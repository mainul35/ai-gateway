#!/usr/bin/env bash
# idle-shutdown — suspend (or power off) homelabai once it has been idle for IDLE_MINUTES.
# Run every 5 min by idle-shutdown.timer. Logs: journalctl -t idle-shutdown -f
#
# "Busy" = any of: stay-awake file present, an SSH/tty session with recent
# keyboard input, the gateway reports work (a request, coding task or job, or a
# request in the last GATEWAY_RECENT_SECONDS), Ollama has a model loaded, a named
# process is running, CPU load, GPU utilisation or network traffic above
# threshold. Every busy check resets the idle clock, and so does waking from sleep.
set -uo pipefail

CONF=/etc/default/idle-shutdown
[[ -r $CONF ]] && . "$CONF"

: "${IDLE_MINUTES:=30}"
: "${DRY_RUN:=1}"
: "${ACTION:=suspend}"             # suspend (wakes in seconds) or poweroff
: "${CHECK_GATEWAY:=1}"
: "${GATEWAY_URL:=http://127.0.0.1:9000}"
: "${GATEWAY_RECENT_SECONDS:=300}" # a request this recent counts as busy (= the timer's interval)
: "${CHECK_INTERVAL_SECONDS:=300}" # how often the timer runs; a much longer gap means we slept"
: "${GPU_UTIL_MAX:=10}"            # % — highest GPU over a 5 s sample
: "${LOAD_MAX:=2.0}"               # 1-min load average
: "${NET_KBPS_MAX:=500}"           # KB/s rx+tx on the default interface
: "${SESSION_IDLE_MAX_MIN:=20}"    # a tty with no keystrokes for this long doesn't count
: "${CHECK_OLLAMA:=1}"
: "${OLLAMA_URL:=http://127.0.0.1:11434}"
: "${BUSY_PROCESSES:=apt apt-get dpkg unattended-upgr rsync}"
: "${INHIBIT_FILE:=/run/stay-awake}"

STATE_DIR=/run/idle-shutdown        # tmpfs: cleared on reboot = free boot grace period
LAST_BUSY=$STATE_DIR/last_busy
LAST_CHECK=$STATE_DIR/last_check
mkdir -p "$STATE_DIR"

log() { logger -t idle-shutdown -- "$*"; }
now=$(date +%s)
busy_reason=""

check() {
  # 1. Manual override: sudo touch /run/stay-awake
  if [[ -e $INHIBIT_FILE ]]; then busy_reason="inhibit file $INHIBIT_FILE"; return; fi

  # 2. Interactive sessions — only if someone actually typed recently.
  #    (An SSH window left open while you sleep does NOT keep the box up.)
  local tty atime
  while read -r tty; do
    [[ -e /dev/$tty ]] || continue
    atime=$(stat -c %X "/dev/$tty")
    if (( now - atime < SESSION_IDLE_MAX_MIN * 60 )); then
      busy_reason="recent input on $tty"; return
    fi
  done < <(who | awk '{print $2}')

  # 3. The gateway knows about work that leaves the GPU and CPU quiet
  if (( CHECK_GATEWAY )); then
    local verdict
    verdict=$(curl -fsS --max-time 3 "$GATEWAY_URL/internal/activity" 2>/dev/null | python3 -c '
import json, sys
a = json.load(sys.stdin)
recent = a.get("seconds_since_last_request")
if a.get("busy"):
    print("; ".join(a.get("reasons", [])))
elif recent is not None and recent < int(sys.argv[1]):
    print(f"gateway request {recent} s ago")
' "$GATEWAY_RECENT_SECONDS" 2>/dev/null)
    if [[ -n $verdict ]]; then busy_reason="gateway: $verdict"; return; fi
  fi

  # 4. Ollama has a model in memory (unloads after keep_alive, default 5m)
  if (( CHECK_OLLAMA )) && curl -fsS --max-time 3 "$OLLAMA_URL/api/ps" 2>/dev/null | grep -q '"name"'; then
    busy_reason="ollama model loaded"; return
  fi

  # 5. Named processes
  local p
  for p in $BUSY_PROCESSES; do
    if pgrep -x "$p" >/dev/null; then busy_reason="process $p running"; return; fi
  done

  # 6. CPU load
  local load; load=$(cut -d' ' -f1 /proc/loadavg)
  if awk -v l="$load" -v m="$LOAD_MAX" 'BEGIN{exit !(l>m)}'; then
    busy_reason="load $load > $LOAD_MAX"; return
  fi

  # 7. GPU utilisation + network throughput, sampled over 5 s
  local iface rx1=0 tx1=0 rx2=0 tx2=0 g gpu_peak=0 i
  iface=$(ip route show default 2>/dev/null | awk '{print $5; exit}')
  if [[ -n $iface ]]; then
    rx1=$(<"/sys/class/net/$iface/statistics/rx_bytes"); tx1=$(<"/sys/class/net/$iface/statistics/tx_bytes")
  fi
  for i in 1 2 3 4 5; do
    if command -v nvidia-smi >/dev/null; then
      g=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1)
      [[ $g =~ ^[0-9]+$ ]] && (( g > gpu_peak )) && gpu_peak=$g
    fi
    sleep 1
  done
  if (( gpu_peak > GPU_UTIL_MAX )); then busy_reason="gpu ${gpu_peak}%"; return; fi

  if [[ -n $iface ]]; then
    rx2=$(<"/sys/class/net/$iface/statistics/rx_bytes"); tx2=$(<"/sys/class/net/$iface/statistics/tx_bytes")
    local kbps=$(( (rx2 - rx1 + tx2 - tx1) / 5 / 1024 ))
    if (( kbps > NET_KBPS_MAX )); then busy_reason="network ${kbps} KB/s on $iface"; return; fi
  fi
}

check

# Just woke from sleep? The wall clock jumped over the time asleep; that was not idle time.
resumed=0
if [[ -f $LAST_CHECK ]] && (( now - $(<"$LAST_CHECK") > 3 * CHECK_INTERVAL_SECONDS )); then
  resumed=1
fi
echo "$now" > "$LAST_CHECK"

# Busy, first run since boot, or just resumed → reset the idle clock
if [[ -n $busy_reason || ! -f $LAST_BUSY ]] || (( resumed )); then
  echo "$now" > "$LAST_BUSY"
  if (( resumed )); then busy_reason="just woke from sleep${busy_reason:+; $busy_reason}"; fi
  log "busy: ${busy_reason:-first check since boot}"
  exit 0
fi

idle_for=$(( now - $(<"$LAST_BUSY") ))
log "idle for $(( idle_for / 60 )) min (threshold ${IDLE_MINUTES})"

if (( idle_for >= IDLE_MINUTES * 60 )); then
  if (( DRY_RUN )); then
    log "DRY_RUN: would $ACTION now"
    exit 0
  fi
  if [[ $ACTION == poweroff ]]; then
    log "idle threshold reached — powering off in 1 min"
    shutdown -h +1 "homelabai idle ${IDLE_MINUTES}+ min. Cancel: sudo shutdown -c && sudo touch $INHIBIT_FILE"
    exit 0
  fi
  log "idle threshold reached — suspending in 1 min"
  wall "homelabai idle ${IDLE_MINUTES}+ min: suspending in 1 minute. Cancel: sudo touch $INHIBIT_FILE" 2>/dev/null
  sleep 60
  busy_reason=""; now=$(date +%s); check
  if [[ -n $busy_reason ]]; then
    echo "$now" > "$LAST_BUSY"
    log "suspend cancelled: $busy_reason"
    exit 0
  fi
  echo "$now" > "$LAST_BUSY"     # so the first check after waking starts a fresh idle period
  systemctl suspend
fi
