#!/usr/bin/env bash
# nic-watchdog - brings homelabai's network card back when it dies, so the machine never sits on,
# awake and unreachable. Logs: journalctl -t nic-watchdog
#
# Why: the onboard Intel I211 (igb) sometimes drops off the PCIe bus when the machine wakes from
# suspend ("igb ... enp4s0: PCIe link lost"). Nothing brought it back: on 2026-11-11 homelabai
# woke at 03:23, lost the card, and stayed offline for 7 hours - still suspending and waking - until
# it was restarted by hand. A restart brings the card back; on 2026-11-01 it took a power cut.
#
#   nic-watchdog resume   after every wake (system-sleep hook): waits for the link, and if it does
#                         not come, resets the card on the PCI bus, then reboots as a last resort
#   nic-watchdog watch    every minute (nic-watchdog.timer): the same, for a card that dies later
#   nic-watchdog status   what it sees, changing nothing
#
# "Dead" is the card itself: gone from the system, or no carrier on its port. A router or internet
# outage leaves the carrier up, so it never reboots the machine for one. Reboots are rate-limited
# (MAX_REBOOTS in REBOOT_WINDOW_MIN), so a cable pulled out cannot make it reboot in a loop.
#
# Settings: /etc/default/nic-watchdog (see nic-watchdog.conf). DRY_RUN=1 only says what it would do.
set -uo pipefail

CONF=${NIC_WATCHDOG_CONF:-/etc/default/nic-watchdog}
[ -r "$CONF" ] && . "$CONF"
NIC_MAC=${NIC_MAC:-a8:a1:59:9d:f4:ae}          # the card, by its hardware address: names change
NIC_PCI=${NIC_PCI:-0000:04:00.0}              # where it sits, for when it has dropped off the bus
LINK_WAIT_SEC=${LINK_WAIT_SEC:-40}            # a link takes a few seconds to negotiate after a wake
DEAD_CHECKS_BEFORE_RESET=${DEAD_CHECKS_BEFORE_RESET:-2}    # watch: minutes dead before a reset
DEAD_CHECKS_BEFORE_REBOOT=${DEAD_CHECKS_BEFORE_REBOOT:-5}  # watch: minutes dead before a reboot
MAX_REBOOTS=${MAX_REBOOTS:-3}
REBOOT_WINDOW_MIN=${REBOOT_WINDOW_MIN:-360}
STATE_DIR=${STATE_DIR:-/var/lib/nic-watchdog}
DRY_RUN=${DRY_RUN:-0}

log() { logger -t nic-watchdog -- "$*"; echo "nic-watchdog: $*"; }

# The interface that has the card's address now, or nothing
nic_iface() {
    local dev
    for dev in /sys/class/net/*; do
        [ "$(cat "$dev/address" 2>/dev/null)" = "$NIC_MAC" ] && { basename "$dev"; return 0; }
    done
    return 1
}

# Alive: the card is there and its port has a link
alive() {
    local iface
    iface=$(nic_iface) || return 1
    [ "$(cat "/sys/class/net/$iface/carrier" 2>/dev/null)" = "1" ]
}

wait_alive() {
    local waited=0
    while [ "$waited" -lt "$1" ]; do
        alive && return 0
        sleep 2
        waited=$((waited + 2))
    done
    alive
}

run() {
    if [ "$DRY_RUN" = "1" ]; then log "(dry run) would: $*"; else "$@"; fi
}

# Takes the card off the PCI bus and finds it again: the driver starts from scratch, as at boot
reset_card() {
    local pci=$NIC_PCI iface
    iface=$(nic_iface) && pci=$(basename "$(readlink -f "/sys/class/net/$iface/device")")
    log "resetting the network card ($pci): removing it from the PCI bus and scanning again"
    [ -e "/sys/bus/pci/devices/$pci/remove" ] && run sh -c "echo 1 > /sys/bus/pci/devices/$pci/remove"
    sleep 3
    run sh -c "echo 1 > /sys/bus/pci/rescan"
    sleep 5
    iface=$(nic_iface) || { log "the card did not come back after the reset"; return 1; }
    # Wake-on-LAN is set on the card itself; a card found again starts with it off
    command -v ethtool >/dev/null && run ethtool -s "$iface" wol g
    command -v networkctl >/dev/null && run networkctl reconfigure "$iface"
    return 0
}

reboots_recently() {
    mkdir -p "$STATE_DIR"
    touch "$STATE_DIR/reboots"
    local since=$(( $(date +%s) - REBOOT_WINDOW_MIN * 60 ))
    awk -v since="$since" '$1 >= since' "$STATE_DIR/reboots" | wc -l
}

reboot_machine() {
    local done_lately
    done_lately=$(reboots_recently)
    if [ "$done_lately" -ge "$MAX_REBOOTS" ]; then
        log "the card is still dead, but $done_lately reboots in the last $REBOOT_WINDOW_MIN min already: not rebooting again"
        return 1
    fi
    log "the card is still dead after a reset: rebooting ($((done_lately + 1)) of at most $MAX_REBOOTS in $REBOOT_WINDOW_MIN min)"
    [ "$DRY_RUN" = "1" ] || date +%s >> "$STATE_DIR/reboots"
    run systemctl reboot
}

recover() {
    reset_card
    if wait_alive "$LINK_WAIT_SEC"; then
        log "the card is back after the reset ($(nic_iface))"
        return 0
    fi
    reboot_machine
}

# One at a time: the minute check must not reset the card while the after-wake one is waiting on it
if [ "${1:-watch}" = "resume" ] || [ "${1:-watch}" = "watch" ]; then
    exec 9>/run/nic-watchdog.lock
    flock -n 9 || exit 0
fi

case "${1:-watch}" in
    resume)
        if wait_alive "$LINK_WAIT_SEC"; then
            exit 0
        fi
        log "no network link ${LINK_WAIT_SEC}s after waking from sleep"
        recover
        ;;
    watch)
        mkdir -p "$STATE_DIR"
        if alive; then
            rm -f "$STATE_DIR/dead"
            exit 0
        fi
        echo x >> "$STATE_DIR/dead"
        dead=$(wc -l < "$STATE_DIR/dead")
        log "no network link (check $dead in a row)"
        if [ "$dead" -ge "$DEAD_CHECKS_BEFORE_REBOOT" ]; then
            rm -f "$STATE_DIR/dead"
            reboot_machine
        elif [ "$dead" -eq "$DEAD_CHECKS_BEFORE_RESET" ]; then
            reset_card
        fi
        ;;
    wol)
        # At boot (nic-wol.service): Wake-on-LAN on, on whatever the card is called now
        iface=$(nic_iface) || { log "no network card with address $NIC_MAC: Wake-on-LAN not set"; exit 1; }
        run ethtool -s "$iface" wol g
        log "Wake-on-LAN on for $iface ($(ethtool "$iface" 2>/dev/null | awk -F': ' '/^[[:space:]]*Wake-on:/ {print $2}'))"
        ;;
    status)
        iface=$(nic_iface) || iface="(missing)"
        echo "card $NIC_MAC: interface $iface, carrier $(cat "/sys/class/net/$iface/carrier" 2>/dev/null || echo -),"\
             "alive: $(alive && echo yes || echo no), reboots by this in the last $REBOOT_WINDOW_MIN min: $( [ -r "$STATE_DIR/reboots" ] && reboots_recently || echo 0)"
        ;;
    *)
        echo "usage: nic-watchdog resume|watch|wol|status" >&2
        exit 2
        ;;
esac
