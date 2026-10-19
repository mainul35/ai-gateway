#!/usr/bin/env bash
# The coding sandbox's network: the internet, for fetching a project's dependencies, and nothing else.
#
#   sudo ops/sandbox-network/setup.sh install   # once: rules now and at every boot, then the network
#   sudo ops/sandbox-network/setup.sh status    # what is in place
#   sudo ops/sandbox-network/setup.sh remove    # undo all of it
#
# A coding task runs code nobody has reviewed. On Docker's default bridge it can reach every service
# on this machine (Ollama, ComfyUI, the database's neighbours, anything bound to 0.0.0.0) and, through
# it, the LAN - Jenkins, Harbor, the router. Here, traffic from the sandbox's bridge is dropped:
#   - to any private, loopback, link-local, CGNAT or multicast address (DOCKER-USER chain), and
#   - to this machine itself, on any of its addresses (INPUT chain).
# Names still resolve: Docker's resolver answers from inside the container's own namespace.
#
# The gateway uses the network only while it carries the label gateway.egress=filtered, which this
# script sets after the rules are in place (app/coding/sandbox.py). Root is needed for the firewall.
set -euo pipefail

NAME=${SANDBOX_NETWORK:-gateway-sandbox}
SUBNET=${SANDBOX_SUBNET:-172.31.250.0/24}
BRIDGE=${SANDBOX_BRIDGE:-br-gwsandbox}
BLOCKED="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4"
SELF=/usr/local/sbin/gateway-sandbox-net
UNIT=/etc/systemd/system/gateway-sandbox-net.service

[ "$(id -u)" = 0 ] || { echo "needs root: sudo $0 ${1:-install}" >&2; exit 1; }

rule() {    # adds the rule once, at the top of its chain
    local table_cmd=$1 chain=$2; shift 2
    "$table_cmd" -C "$chain" "$@" 2>/dev/null || "$table_cmd" -I "$chain" "$@"
}

unrule() {
    local table_cmd=$1 chain=$2; shift 2
    while "$table_cmd" -C "$chain" "$@" 2>/dev/null; do "$table_cmd" -D "$chain" "$@"; done
}

apply() {
    iptables -nL DOCKER-USER >/dev/null 2>&1 || iptables -N DOCKER-USER
    for net in $BLOCKED; do
        rule iptables DOCKER-USER -i "$BRIDGE" -d "$net" -j DROP
    done
    rule iptables INPUT -i "$BRIDGE" -j DROP
    rule ip6tables INPUT -i "$BRIDGE" -j DROP 2>/dev/null || true
    rule ip6tables FORWARD -i "$BRIDGE" -j DROP 2>/dev/null || true
}

network() {
    if docker network inspect "$NAME" >/dev/null 2>&1; then
        local label
        label=$(docker network inspect --format '{{index .Labels "gateway.egress"}}' "$NAME")
        local bridge
        bridge=$(docker network inspect --format '{{index .Options "com.docker.network.bridge.name"}}' "$NAME")
        if [ "$label" != filtered ] || [ "$bridge" != "$BRIDGE" ]; then
            echo "network $NAME exists but was not made by this script (label '$label', bridge '$bridge');" >&2
            echo "remove it (docker network rm $NAME) and run install again" >&2
            exit 1
        fi
        return
    fi
    docker network create --driver bridge --subnet "$SUBNET" \
        -o "com.docker.network.bridge.name=$BRIDGE" \
        --label gateway.egress=filtered "$NAME" >/dev/null
}

case "${1:-install}" in
    install)
        install -m 755 "$0" "$SELF"
        cat > "$UNIT" <<EOF
[Unit]
Description=Firewall for the AI gateway's coding sandbox network
After=docker.service network-online.target
Wants=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SELF apply

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable --now gateway-sandbox-net.service
        network
        echo "sandbox network $NAME ($SUBNET on $BRIDGE): internet only; rules re-applied at every boot"
        ;;
    apply)
        apply
        ;;
    status)
        docker network inspect --format '{{.Name}} {{index .Labels "gateway.egress"}} {{(index .IPAM.Config 0).Subnet}}' "$NAME" 2>/dev/null \
            || echo "no network $NAME"
        iptables -S DOCKER-USER 2>/dev/null | grep -- "-i $BRIDGE" || echo "no DOCKER-USER rules for $BRIDGE"
        iptables -S INPUT | grep -- "-i $BRIDGE" || echo "no INPUT rule for $BRIDGE"
        systemctl is-enabled gateway-sandbox-net.service 2>/dev/null || true
        ;;
    remove)
        docker network rm "$NAME" 2>/dev/null || true
        for net in $BLOCKED; do unrule iptables DOCKER-USER -i "$BRIDGE" -d "$net" -j DROP; done
        unrule iptables INPUT -i "$BRIDGE" -j DROP
        unrule ip6tables INPUT -i "$BRIDGE" -j DROP 2>/dev/null || true
        unrule ip6tables FORWARD -i "$BRIDGE" -j DROP 2>/dev/null || true
        systemctl disable --now gateway-sandbox-net.service 2>/dev/null || true
        rm -f "$UNIT" "$SELF"
        systemctl daemon-reload
        echo "removed; tasks that need a network will fail until it is installed again"
        ;;
    *)
        echo "usage: sudo $0 install|status|remove" >&2; exit 2 ;;
esac
