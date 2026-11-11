#!/usr/bin/env bash
# Installs the network-card watchdog on homelabai and fixes the network config that still names the
# card enp3s0. Run once, on homelabai:  sudo bash ~/model-gateway/ops/nic-watchdog/install.sh
# Safe to run again. Backups of what it replaces go to /root/nic-watchdog-backup-<time>/.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "run it with sudo"; exit 1; }

MAC=$(. ./nic-watchdog.conf; echo "$NIC_MAC")
ip -o link | grep -qi "$MAC" || { echo "no network card with address $MAC here - check NIC_MAC in nic-watchdog.conf"; exit 1; }
backup=/root/nic-watchdog-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup"

echo "== the watchdog"
install -m 755 nic-watchdog.sh /usr/local/sbin/nic-watchdog
[ -e /etc/default/nic-watchdog ] || install -m 644 nic-watchdog.conf /etc/default/nic-watchdog
install -m 755 nic-watchdog.sleep /usr/lib/systemd/system-sleep/nic-watchdog
install -m 644 nic-watchdog.service nic-watchdog.timer nic-wol.service /etc/systemd/system/

echo "== Wake-on-LAN by the card's address, instead of wol-enp3s0"
if systemctl list-unit-files wol-enp3s0.service >/dev/null 2>&1 && [ -e /etc/systemd/system/wol-enp3s0.service ]; then
    cp /etc/systemd/system/wol-enp3s0.service "$backup/"
    systemctl disable --now wol-enp3s0.service 2>/dev/null || true
fi
systemctl daemon-reload
systemctl enable --now nic-wol.service nic-watchdog.timer

echo "== network config matched by address"
for f in /etc/netplan/*.yaml; do
    [ "$f" = /etc/netplan/01-lan.yaml ] && continue
    if grep -q "enp3s0" "$f"; then
        echo "   moving $f (names enp3s0) to $backup/"
        mv "$f" "$backup/"
    fi
done
install -m 600 01-lan.yaml /etc/netplan/01-lan.yaml
netplan generate
# Same address as now, so an SSH session survives the reconfiguration
netplan apply

echo
nic-watchdog status
echo "Backups: $backup"
echo "Logs:    journalctl -t nic-watchdog"
