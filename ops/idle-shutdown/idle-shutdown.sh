#!/usr/bin/env bash
# idle-shutdown — suspend (or power off) homelabai once it has been idle for IDLE_MINUTES.
# Run every minute by idle-shutdown.timer. Logs: journalctl -t idle-shutdown -f
#
# "Busy" = any of: stay-awake file present, a deploy in progress, a job registered with
# bin/keep-awake whose process is still alive, an SSH/tty session with recent keyboard input, the
# gateway reports work (a request, coding task or job, or a request in the last
# GATEWAY_RECENT_SECONDS), a process doing real work, CPU load, GPU utilisation or network traffic
# above threshold. Every busy check resets the idle clock, and so does waking from sleep.
#
# "A process doing real work" is measured, not guessed from names alone: every check notes each
# process's CPU time and disk I/O, and the next check compares. Busy is
#   - any process using PROC_CPU_MIN % of a core or moving PROC_IO_MB_MIN MB a minute: a model
#     generating, a download writing to disk, a build, a pipeline, a script started with nohup;
#   - a command running in a terminal session (ssh, tmux) using any CPU (PROC_CPU_TTY_MIN), even
#     with nobody typing - an idle shell, an editor or `top` left open is not work;
#   - a download or package tool (DOWNLOAD_PROCESSES, DOWNLOAD_COMMANDS) running for 30 s or more,
#     however slowly - a stalled download still means somebody wants it finished.
# Background services that are never idle (PROC_IGNORE: microk8s, its network, container
# runtimes) are left out, and so is this check's own work. Each busy line names the process.
#
# A model sitting loaded is not work: Ollama keeps them for ever (OLLAMA_KEEP_ALIVE=-1), so counting
# one kept the machine up for nothing. A model answering shows as CPU, GPU and a gateway request.
# Every model is unloaded just before suspending (POST /internal/unload), so none sleeps on the GPU.
#
# Keeping it awake for a job of your own: `bin/keep-awake <command>` (or `keep-awake --pid <pid>`
# for one already running). It holds exactly as long as that process lives - unlike refreshing the
# deploy lock from a loop, which kept the machine up 9.5 hours after a download ended (2026-10-21).
set -uo pipefail

CONF=${IDLE_SHUTDOWN_CONF:-/etc/default/idle-shutdown}
[[ -r $CONF ]] && . "$CONF"

: "${IDLE_MINUTES:=5}"
: "${DRY_RUN:=1}"
: "${ACTION:=suspend}"             # suspend (wakes in seconds) or poweroff
: "${CHECK_GATEWAY:=1}"
: "${GATEWAY_URL:=http://127.0.0.1:9000}"
: "${GATEWAY_RECENT_SECONDS:=90}"  # a request this recent counts as busy (a bit over the interval)
: "${CHECK_INTERVAL_SECONDS:=60}"  # how often the timer runs; a much longer gap means we slept
: "${GPU_UTIL_MAX:=10}"            # % — highest GPU over a 5 s sample
: "${LOAD_MAX:=2.0}"               # 1-min load average
: "${NET_KBPS_MAX:=500}"           # KB/s rx+tx on the default interface
: "${SESSION_IDLE_MAX_MIN:=5}"     # a tty with no keystrokes for this long doesn't count
: "${CHECK_OLLAMA:=0}"             # 1: a loaded Ollama model counts as busy (the old rule)
: "${OLLAMA_URL:=http://127.0.0.1:11434}"
: "${BUSY_PROCESSES:=apt apt-get dpkg rsync}"
: "${INHIBIT_FILE:=/run/stay-awake}"
: "${DEPLOY_LOCK:=/tmp/gateway-deploying}" # bin/deploy.sh holds this while it works
: "${WARN_SECONDS:=30}"            # notice before suspending; a busy signal in it cancels
: "${KEEP_AWAKE_DIR:=/tmp/keep-awake}"     # bin/keep-awake: one file per job, holding its pid
: "${PROC_CPU_MIN:=10}"            # % of one core over the last interval: a process at work
: "${PROC_CPU_TTY_MIN:=1}"         # ...for a command run in a terminal session
: "${PROC_IO_MB_MIN:=20}"          # MB read+written a minute: a download, a copy, a build
# Never idle, never work: microk8s and its network, container runtimes, the kernel's own helpers
: "${PROC_IGNORE:=kubelite|k8s-dqlite|calico|flanneld|containerd|dockerd|snapd|systemd-journald|multipathd}"
# A terminal process that is not work by itself: shells, multiplexers, viewers, editors
: "${TTY_IGNORE:=^-?(ba|z|fi|da|k|c|tc)?sh$|^(tmux|screen|sudo|su|login|sshd|less|more|man|vi|vim|nvim|nano|top|htop|btop|watch|tail|journalctl)$}"
# Downloads and installs, by program name and by what a program is asked to do; 30 s or older
: "${DOWNLOAD_PROCESSES:=apt apt-get dpkg rsync curl wget aria2c scp sftp git git-lfs hf huggingface-cli pip pip3 uv snap}"
: "${DOWNLOAD_COMMANDS:=ollama (pull|create|push)|docker (build|pull|push|load)|docker[- ]compose .*(build|pull)|docker buildx}"
: "${STATE_DIR:=/run/idle-shutdown}"   # tmpfs: cleared on reboot = free boot grace period
: "${LOG_TAG:=idle-shutdown}"

LAST_BUSY=$STATE_DIR/last_busy
LAST_CHECK=$STATE_DIR/last_check
mkdir -p "$STATE_DIR"

log() { logger -t "$LOG_TAG" -- "$*"; }
now=$(date +%s)
busy_reason=""

# Which processes did real work since the last check: prints the reason, or nothing. Keeps its own
# per-process figures in $STATE_DIR/procs.json, so it runs on every check, busy or not.
working_processes() {
  PROC_CPU_MIN=$PROC_CPU_MIN PROC_CPU_TTY_MIN=$PROC_CPU_TTY_MIN PROC_IO_MB_MIN=$PROC_IO_MB_MIN \
  PROC_IGNORE=$PROC_IGNORE TTY_IGNORE=$TTY_IGNORE DOWNLOAD_PROCESSES=$DOWNLOAD_PROCESSES \
  DOWNLOAD_COMMANDS=$DOWNLOAD_COMMANDS BUSY_PROCESSES=$BUSY_PROCESSES \
  python3 - "$STATE_DIR/procs.json" "$$" <<'PY' 2>/dev/null
import json, os, re, sys, time

state_path, me = sys.argv[1], int(sys.argv[2])
env = os.environ
hz = os.sysconf("SC_CLK_TCK")
uptime = float(open("/proc/uptime").read().split()[0])
ignore = re.compile(env["PROC_IGNORE"]) if env.get("PROC_IGNORE") else None
tty_ignore = re.compile(env["TTY_IGNORE"]) if env.get("TTY_IGNORE") else None
downloads = set(env.get("DOWNLOAD_PROCESSES", "").split()) | set(env.get("BUSY_PROCESSES", "").split())
download_commands = re.compile(env["DOWNLOAD_COMMANDS"]) if env.get("DOWNLOAD_COMMANDS") else None

procs = {}
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        if not argv:
            continue                                         # a kernel thread
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        io = 0
        try:
            with open(f"/proc/{pid}/io") as f:
                counters = dict(line.split(": ") for line in f.read().splitlines())
            io = int(counters.get("read_bytes", 0)) + int(counters.get("write_bytes", 0))
        except (OSError, ValueError):
            pass
        procs[pid] = {"ppid": int(fields[1]), "tty": int(fields[4]), "start": int(fields[19]),
                      "cpu": int(fields[11]) + int(fields[12]), "io": io,
                      "name": os.path.basename(argv[0]), "cmd": " ".join(argv)[:160]}
    except (OSError, IndexError, ValueError):
        continue

# This check's own work (curl, nvidia-smi, this python) is not anybody's job
mine = {str(me)}
changed = True
while changed:
    changed = False
    for pid, p in procs.items():
        if pid not in mine and str(p["ppid"]) in mine:
            mine.add(pid)
            changed = True

try:
    with open(state_path) as f:
        before = json.load(f)
except (OSError, ValueError):
    before = {}
elapsed = max(1.0, time.time() - before.get("at", 0)) if before else None
with open(state_path + ".new", "w") as f:
    json.dump({"at": time.time(), "procs": {pid: [p["start"], p["cpu"], p["io"]] for pid, p in procs.items()}}, f)
os.replace(state_path + ".new", state_path)

reasons = []
for pid, p in procs.items():
    if pid in mine or (ignore and ignore.search(p["cmd"])):
        continue
    age = uptime - p["start"] / hz
    if age >= 30 and (p["name"] in downloads or (download_commands and download_commands.search(p["cmd"]))):
        reasons.append(f"{p['name']} running {int(age // 60)} min ({p['cmd'][:70]})")
        continue
    old = (before.get("procs") or {}).get(pid) if elapsed else None
    if not old or old[0] != p["start"]:
        continue                                             # new since the last check: next time
    cpu = (p["cpu"] - old[1]) / hz / elapsed * 100
    mb = (p["io"] - old[2]) / 1e6 / elapsed * 60
    in_terminal = p["tty"] != 0 and not (tty_ignore and tty_ignore.search(p["name"]))
    if in_terminal and cpu >= float(env["PROC_CPU_TTY_MIN"]):
        reasons.append(f"{p['name']} running in a terminal ({cpu:.0f}% cpu: {p['cmd'][:70]})")
    elif cpu >= float(env["PROC_CPU_MIN"]) or mb >= float(env["PROC_IO_MB_MIN"]):
        reasons.append(f"{p['name']} working ({cpu:.0f}% cpu, {mb:.0f} MB/min: {p['cmd'][:70]})")
if reasons:
    print("; ".join(reasons[:3]) + (f" and {len(reasons) - 3} more" if len(reasons) > 3 else ""))
PY
}

check() {
  # Measured on every check, before anything returns, so each one compares with the minute before
  local working; working=$(working_processes)

  # 1. Manual override: sudo touch /run/stay-awake
  if [[ -e $INHIBIT_FILE ]]; then busy_reason="inhibit file $INHIBIT_FILE"; return; fi
  # A deploy in progress. Only a fresh one counts, so a deploy that died cannot keep it up forever.
  if [[ -n $(find "$DEPLOY_LOCK" -maxdepth 0 -mmin -30 2>/dev/null) ]]; then
    busy_reason="deploy in progress"; return
  fi
  # Jobs registered with bin/keep-awake: busy exactly while their process lives; a finished or
  # crashed job's file is cleared here, so none can hold the machine up after it
  mkdir -p "$KEEP_AWAKE_DIR" 2>/dev/null && chmod 1777 "$KEEP_AWAKE_DIR" 2>/dev/null
  local held pid
  for held in "$KEEP_AWAKE_DIR"/*; do
    [[ -f $held ]] || continue
    pid=$(head -c 20 "$held" | tr -dc 0-9)
    if [[ -n $pid && -r /proc/$pid/stat ]] && [[ $(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null) != Z ]]; then
      busy_reason="kept awake by $(basename "$held") (pid $pid)"; return
    fi
    rm -f "$held"
  done

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

  # 4. Ollama has a model in memory - only with CHECK_OLLAMA=1; see the top
  if (( CHECK_OLLAMA )) && curl -fsS --max-time 3 "$OLLAMA_URL/api/ps" 2>/dev/null | grep -q '"name"'; then
    busy_reason="ollama model loaded"; return
  fi

  # 5. Processes doing real work, downloads and installs, commands run in a terminal (measured above;
  #    BUSY_PROCESSES are counted there with the downloads)
  if [[ -n $working ]]; then busy_reason="$working"; return; fi

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

# Every model off the GPU before sleeping: the gateway stops its llama.cpp models and has Ollama drop
# its own; if the gateway does not answer, Ollama is asked directly. Never a reason not to sleep.
unload_models() {
  local result
  if result=$(curl -fsS --max-time 90 -X POST "$GATEWAY_URL/internal/unload" 2>/dev/null); then
    log "unloaded before suspending: $result"
    return
  fi
  log "gateway did not answer the unload; asking Ollama directly"
  local name
  for name in $(curl -fsS --max-time 5 "$OLLAMA_URL/api/ps" 2>/dev/null | python3 -c '
import json, sys
for m in json.load(sys.stdin).get("models") or []:
    print(m.get("name") or m.get("model"))' 2>/dev/null); do
    curl -fsS --max-time 30 "$OLLAMA_URL/api/generate" -d "{\"model\": \"$name\", \"keep_alive\": 0}" >/dev/null 2>&1       && log "unloaded $name"
  done
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
  log "idle threshold reached — suspending in ${WARN_SECONDS}s"
  wall "homelabai idle ${IDLE_MINUTES}+ min: suspending in ${WARN_SECONDS}s. Cancel: sudo touch $INHIBIT_FILE" 2>/dev/null
  sleep "$WARN_SECONDS"
  busy_reason=""; now=$(date +%s); check
  if [[ -n $busy_reason ]]; then
    echo "$now" > "$LAST_BUSY"
    log "suspend cancelled: $busy_reason"
    exit 0
  fi
  echo "$now" > "$LAST_BUSY"     # so the first check after waking starts a fresh idle period
  unload_models
  systemctl suspend
fi
