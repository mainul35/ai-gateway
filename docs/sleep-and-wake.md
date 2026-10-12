# Sleep and wake

homelabai (RTX 3090, 5950X) draws 80-130 W doing nothing. It now suspends itself after a few idle
minutes and wakes when someone actually uses the gateway, so a long task that finishes at 3 a.m. no
longer leaves it running until morning.

- [What runs where](#what-runs-where)
- [How a request is handled](#how-a-request-is-handled)
- [When it goes to sleep](#when-it-goes-to-sleep)
- [Changing the idle timeout](#changing-the-idle-timeout)
- [Doing it by hand](#doing-it-by-hand)
- [Setting it up](#setting-it-up)
- [Settings reference](#settings-reference)
- [Troubleshooting](#troubleshooting)
- [Power and cost](#power-and-cost)
- [Files](#files)

## What runs where

```
                      Cloudflare Tunnel  (ai-gateway.mainul35.dev)
                              │
                              ▼
 proxy-vm 192.168.50.81 ─ never sleeps ────────────────────────────────────
   cloudflared ──► wake front :9100  (wake/)
                     │  awake:  passes every request straight through
                     │  asleep: Wake-on-LAN packet, then answers the request
                     ▼
 homelabai 192.168.50.146 ─ sleeps when idle ──────────────────────────────
   gateway :9000 (app/)  ─  PostgreSQL :5433  ─  Ollama / llama.cpp / ComfyUI
   idle timer (ops/idle-shutdown): suspends after IDLE_MINUTES with nothing to do
```

**The gateway and its database stay on homelabai.** Only the wake front, a ~400-line proxy with no
state worth keeping, runs on proxy-vm. Nothing moves: the gateway needs the GPU machine (it starts
llama.cpp engines, talks to ComfyUI on localhost, runs photo tools and coding tasks there), and its
database only matters while the gateway is running, so they sleep and wake together.

What does move is **where the tunnel points**. A tunnel connector on homelabai goes to sleep with it,
and Cloudflare then answers every request with error 1033. The connector for
`ai-gateway.mainul35.dev` belongs on proxy-vm, pointing at the wake front ([setup step 3](#3-cloudflare-tunnel)).

## How a request is handled

The wake front checks whether the gateway answers before doing anything else.

**Awake:** the request is passed through untouched, streaming included. The only cost is one hop on
the LAN.

**Asleep:** if the request may wake the machine (below), the wake front sends a Wake-on-LAN packet,
resends it every 15 s, and polls the gateway's `/health` every 2 s. From suspend the gateway answers
in about 5-15 s; from power-off, 1-2 minutes. Meanwhile:

| Request | What the caller sees |
|---|---|
| Streamed completion (`"stream": true` on `/v1/chat/completions` or `/v1/completions`) | SSE headers at once, `: waking homelabai` comment lines every 10 s, then the real answer. Clients ignore comment lines, and the steady bytes keep Cloudflare from cutting the request off at 100 s. Waits up to 300 s |
| Any other API call or click in an open page | Held until the gateway answers, then answered normally. After 90 s (Cloudflare allows 100 s of silence) it gets `503` with `Retry-After: 30`; the OpenAI SDKs retry that on their own |
| A page opened in the browser | A "waking the server" page that reloads itself once the gateway is up |

So from suspend, a request is simply a few seconds slower than usual.

### Who may wake it

`ai-gateway.mainul35.dev` is public, and scanners hit it all day. If any request could wake the
machine it would never sleep, so only these can:

- an API key the gateway has already accepted (the wake front remembers them while it runs), or the
  master key (`WAKE_MASTER_KEY_SHA256`)
- a login cookie with a valid signature (checked with the gateway's own session secret)
- a request that passed Cloudflare Access (the dashboard and admin pages are behind it)
- an unknown key that looks like a gateway key (`sk-` + 43 characters): at most once every 10 min.
  If the gateway then rejects it, it never wakes the machine again

Two kinds of request never wake it, even with a valid key:

- `/health`, so monitors and the deploy script's checks don't
- what a web page repeats on a timer: the dashboard's stats (`/admin/system`, `/admin/usage`,
  `/v1/models` from a browser, every 15 s), the task list and task event feed, and the remote-folder
  long-poll. Otherwise a tab left open anywhere would wake the machine every few minutes, forever.
  The same requests are also left out of the idle check, so an open tab does not keep it awake either

A page opened without a valid cookie gets a "Wake it up" button instead of waking it on its own
(rate-limited to once per 2 min). The gateway still authenticates every request itself; the wake
front only decides who may switch the machine on.

## When it goes to sleep

`ops/idle-shutdown/idle-shutdown.sh` runs every minute (`idle-shutdown.timer`). Each run asks: is
anything happening? If not for `IDLE_MINUTES` in a row, it warns logged-in users, waits
`WARN_SECONDS` (anything busy in that window cancels), and runs `systemctl suspend`.

Busy means any one of:

| Signal | Why |
|---|---|
| `/run/stay-awake` exists | your manual "not now" |
| a deploy is running (`/tmp/gateway-deploying`, written by `bin/deploy.sh`, honoured for 30 min) | a deploy is never cut off halfway |
| someone typed in an SSH or console session in the last `SESSION_IDLE_MAX_MIN` | you are working on it. A session left open but untouched does not count |
| the gateway reports work (`GET /internal/activity`, loopback only) | a request being answered or streamed, a request in the last 90 s, a coding task running or queued, a deploy being watched, a playground job, a llama.cpp request. This catches what GPU numbers miss, like a coding task between two model calls |
| a process in `BUSY_PROCESSES` is running | package installs (`apt`, `dpkg`), `rsync` |
| 1-minute load average above `LOAD_MAX` | something CPU-heavy outside the gateway |
| GPU above `GPU_UTIL_MAX` % (peak over 5 s) | inference or image generation not started through the gateway |
| network above `NET_KBPS_MAX` KB/s | a download or transfer |

A coding task **waiting for your approval** is not running, so it lets the machine sleep; approving it
later wakes it. Time spent asleep is not counted as idle time: after waking, the clock starts again.

Every decision is logged: `journalctl -t idle-shutdown -f`

```
busy: gateway: coding task 12 running
idle for 3 min (threshold 5)
idle threshold reached — suspending in 30s
busy: just woke from sleep
```

## Changing the idle timeout

The threshold is a setting, not code. On homelabai:

```bash
sudo nano /etc/default/idle-shutdown        # change IDLE_MINUTES=5 to what you want
journalctl -t idle-shutdown -f              # the next line shows the new threshold
```

It takes effect on the next check, within a minute. Nothing needs restarting, and deploys never
overwrite `/etc/default/idle-shutdown`, so your value stays.

Things that go with it:

- **Below about 3 minutes** the checks themselves get coarse: the timer runs once a minute, so the
  real delay is `IDLE_MINUTES` plus up to one minute plus `WARN_SECONDS`.
- **Checking more often** means editing the timer too: `OnUnitActiveSec=` in
  `/etc/systemd/system/idle-shutdown.timer`, then `sudo systemctl daemon-reload`, and set
  `CHECK_INTERVAL_SECONDS` in `/etc/default/idle-shutdown` to the same interval (it is how the script
  recognises that it has just woken up). Keep `GATEWAY_RECENT_SECONDS` a little above the interval,
  so a quick request between two checks is never missed.
- **Never sleeping for a while** (an all-night job the signals might miss): `sudo touch /run/stay-awake`,
  and `sudo rm /run/stay-awake` when it is done. A reboot clears it.
- **Powering off instead of suspending:** `ACTION=poweroff`. About 2 W less while off, but every wake
  is a full boot.
- **Switching it off:** `DRY_RUN=1` keeps logging what it would do without doing it;
  `sudo systemctl disable --now idle-shutdown.timer` stops it entirely.

The defaults written in the script itself (`: "${IDLE_MINUTES:=5}"` near the top of
`idle-shutdown.sh`) only apply when `/etc/default/idle-shutdown` does not set a value; change the
config file, not the script.

## Doing it by hand

**Sleep now** (admin key, or the master key):

```bash
curl -X POST http://192.168.50.146:9000/admin/power/suspend -H "Authorization: Bearer <key>"
# {"suspending_in_seconds": 5, ...}
```

It answers `409` with the reasons if something is running; add
`-H 'Content-Type: application/json' -d '{"force": true}'` to suspend anyway. `DELETE` on the same URL
cancels one that has not happened yet. Port 9000 is open only to proxy-vm, so run this from homelabai
(`localhost:9000`), from proxy-vm, or from a logged-in browser through the public URL (`/admin` is
behind Cloudflare Access). On proxy-vm, note that `localhost:9000` is Portainer, not the gateway.

**Wake now:**

```bash
curl -s -X POST localhost:9100/__wake/now        # on proxy-vm
curl -s localhost:9100/__wake/status             # {"up": true, "waking": false, ...}
```

or `bin/wake-homelabai.sh` from a clone on proxy-vm (waits until the gateway answers), the
PowerShell magic-packet script on Windows, or the power button.

## Setting it up

From nothing, in this order; each step can be checked before the next. With the Jenkins pipeline
([deployment.md](deployment.md)) set up, steps 1 and 2's code updates happen on their own after
this; the one-time parts below stay manual because they need root.

### 1. homelabai

```bash
# Gateway with the activity endpoint and suspend API (or let the pipeline do it)
git clone -b develop https://github.com/mainul35/ai-gateway ~/ai-gateway-src
cd ~/ai-gateway-src && DEPLOY_HOST=local bin/deploy.sh
curl -s localhost:9000/internal/activity                     # {"busy": false, ...}

# Let proxy-vm reach the gateway
sudo ufw allow from 192.168.50.81 to any port 9000 proto tcp

# Let the gateway suspend the machine (the suspend API), and nothing else
echo 'mainul35 ALL=(root) NOPASSWD: /usr/bin/systemctl suspend' | sudo tee /etc/sudoers.d/gateway-suspend
sudo chmod 440 /etc/sudoers.d/gateway-suspend

# Suspend must really work: [deep] means suspend-to-RAM (s2idle draws 20-40 W)
cat /sys/power/mem_sleep
cat /sys/class/net/enp3s0/device/power/wakeup                # enabled
sudo ethtool enp3s0 | grep Wake-on                           # Wake-on: g
sudo systemctl suspend                                       # then wake it (step 2 gives you a way)

# The idle timer
cd ~/model-gateway/ops/idle-shutdown
sudo install -m 755 idle-shutdown.sh /usr/local/sbin/idle-shutdown
sudo install -m 644 idle-shutdown.conf /etc/default/idle-shutdown   # first time only: it holds your settings
sudo install -m 644 idle-shutdown.service idle-shutdown.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now idle-shutdown.timer
journalctl -t idle-shutdown -f        # starts with DRY_RUN=1: "would suspend"; then set DRY_RUN=0
```

When a later deploy brings a newer `idle-shutdown.sh`, the deploy output says so and prints the
`sudo install` line to run. Install the `.conf` again only on purpose: it would replace your settings.

### 2. proxy-vm

```bash
git clone -b develop https://github.com/mainul35/ai-gateway ~/ai-gateway-src
mkdir -p ~/gateway-wake && cp ~/ai-gateway-src/wake/wake.env.example ~/gateway-wake/wake.env
nano ~/gateway-wake/wake.env
#   WAKE_UPSTREAM=http://192.168.50.146:9000
#   WAKE_SESSION_SECRET=<gateway.session.secret from homelabai's config/config.properties>
~/ai-gateway-src/bin/deploy-wake.sh              # builds, starts, health-checks, rolls back on failure
curl -s localhost:9100/__wake/status             # {"up": true, ...}
```

Test the whole loop before touching the tunnel: suspend homelabai, then
`curl -s -X POST localhost:9100/__wake/now` and watch `__wake/status` turn `"up": true`.

proxy-vm must sit on the LAN bridge (its address is `192.168.50.x`), not behind NAT, or the
Wake-on-LAN broadcast never leaves Proxmox. The container uses host networking for the same reason.

### 3. Cloudflare tunnel

All connectors of one tunnel serve all of its hostnames, so a connector left on homelabai would
still get some of the requests while it sleeps (1033). Either:

- **A separate tunnel on proxy-vm (cleaner).** Create it in the Cloudflare dashboard (Zero Trust →
  Networks → Tunnels), install its connector on proxy-vm with the `cloudflared service install <token>`
  command shown there, and give it the public hostname `ai-gateway.mainul35.dev` →
  `http://localhost:9100`. Then delete that hostname from the old tunnel. Anything else homelabai
  serves keeps its own tunnel.
- **Move the existing tunnel.** Install its connector on proxy-vm, change the hostname's service to
  `http://localhost:9100`, and uninstall the connector from homelabai
  (`sudo cloudflared service uninstall`).

Check: `curl -s https://ai-gateway.mainul35.dev/__wake/status`. Undo: point the hostname back at the
gateway. The Cloudflare Access applications (`scripts/cloudflare_access.py`) are per hostname and
path, so they keep working unchanged.

## Settings reference

### homelabai: `/etc/default/idle-shutdown`

| Setting | Value shipped | Meaning |
|---|---|---|
| `IDLE_MINUTES` | 5 | minutes with nothing going on before it sleeps |
| `DRY_RUN` | 1 | 1: only log "would suspend". Set 0 once the log looks right |
| `ACTION` | suspend | `suspend` or `poweroff` |
| `WARN_SECONDS` | 30 | warning before sleeping; activity during it cancels |
| `CHECK_INTERVAL_SECONDS` | 60 | must match the timer's interval |
| `CHECK_GATEWAY` / `GATEWAY_URL` | 1 / `http://127.0.0.1:9000` | ask the gateway whether it is working |
| `GATEWAY_RECENT_SECONDS` | 90 | a gateway request this recent counts as busy |
| `SESSION_IDLE_MAX_MIN` | 5 | typing in SSH this recent counts as busy |
| `GPU_UTIL_MAX` | 10 | % GPU (peak over 5 s) |
| `LOAD_MAX` | 2.0 | 1-minute load average |
| `NET_KBPS_MAX` | 500 | KB/s in + out on the default interface |
| `CHECK_OLLAMA` | 0 | Ollama keeps models loaded permanently here, so a loaded model says nothing |
| `BUSY_PROCESSES` | `apt apt-get dpkg rsync` | exact process names that count as busy |
| `INHIBIT_FILE` | `/run/stay-awake` | exists: never sleep |
| `DEPLOY_LOCK` | `/tmp/gateway-deploying` | written by deploys; honoured while under 30 min old |

### homelabai: `config/config.properties` (gateway)

| Setting | Default | Meaning |
|---|---|---|
| `power.suspend.command` | `sudo -n /usr/bin/systemctl suspend` | what `POST /admin/power/suspend` runs; needs a matching sudo rule |

### proxy-vm: `~/gateway-wake/wake.env`

| Setting | Default | Meaning |
|---|---|---|
| `WAKE_UPSTREAM` | required | the gateway on the LAN: `http://192.168.50.146:9000` |
| `WAKE_MAC` | required | homelabai's NIC: `a8:a1:59:9d:f4:ae` |
| `WAKE_BROADCAST` | 255.255.255.255 | the LAN broadcast: `192.168.50.255` |
| `WAKE_SESSION_SECRET` | empty | the gateway's `gateway.session.secret`, to check login cookies properly |
| `WAKE_MASTER_KEY_SHA256` | empty | `printf %s "$KEY" \| sha256sum`, so the master key always wakes it |
| `WAKE_HOLD_SECONDS` | 90 | how long a non-streamed request waits; keep under Cloudflare's 100 |
| `WAKE_STREAM_HOLD_SECONDS` | 300 | how long a streamed completion waits |
| `WAKE_BOOT_TIMEOUT` | 300 | stop trying to wake after this |
| `WAKE_RESEND_SECONDS` | 15 | resend the magic packet this often while waking |
| `WAKE_UNVERIFIED_COOLDOWN` | 600 | an unconfirmed key may wake it once per this many seconds |
| `WAKE_BUTTON_COOLDOWN` | 120 | the "Wake it up" button, for anyone without a valid cookie |

After editing: `cd ~/gateway-wake && docker compose -p gateway-wake up -d --force-recreate`.

## Troubleshooting

Each of these happened during setup.

| Symptom | Cause | Fix |
|---|---|---|
| Public URL shows Cloudflare error 1033 while the machine sleeps | the tunnel connector is on homelabai | [setup step 3](#3-cloudflare-tunnel) |
| `__wake/status` stays `"up": false` though homelabai answers ping | homelabai's firewall blocks port 9000 from proxy-vm | `sudo ufw allow from 192.168.50.81 to any port 9000 proto tcp` on homelabai |
| It never sleeps; the log says `busy: process unattended-upgr running` | an old config listed `unattended-upgr`, which matches Ubuntu's always-running `unattended-upgrade-shutdown` | remove it from `BUSY_PROCESSES` in `/etc/default/idle-shutdown` |
| The log says `DRY_RUN: would suspend now` but nothing happens | still in test mode | `DRY_RUN=0` in `/etc/default/idle-shutdown` |
| The log says `busy: recent input on pts/0` | you are typing in an SSH session (watching the log counts) | close the session, or wait `SESSION_IDLE_MAX_MIN` |
| It never sleeps; `busy: network ... KB/s` | k3s or another service moves data constantly | raise `NET_KBPS_MAX` |
| Wake times out; homelabai does not answer ping | the packet does not wake it from suspend | on homelabai: `ethtool enp3s0` shows `Wake-on: g`; `/sys/class/net/enp3s0/device/power/wakeup` is `enabled`; BIOS "Power On By PCI-E" on. Try the Windows script to tell proxy-vm problems from homelabai ones |
| Wake times out from proxy-vm only | proxy-vm is NAT'd, not bridged | give it a bridged NIC (`vmbr0`) with a `192.168.50.x` address |
| `curl localhost:9000/admin/...` on proxy-vm says `404 page not found` | port 9000 on proxy-vm is Portainer | use `http://192.168.50.146:9000` |
| `docker compose up` in the repo root fails on port 5000 | the root `docker-compose.yml` is the old model checker; 5000 is Harbor | the wake front is deployed with `bin/deploy-wake.sh` (or from `wake/`) |
| `git pull` in `~/model-gateway` fails: not a git repository | by design: it is the deploy target, not a clone | deploy from a clone: `DEPLOY_HOST=local bin/deploy.sh` |
| `POST /admin/power/suspend` answers 503 about sudo | the sudo rule is missing | the `sudoers.d/gateway-suspend` lines in step 1 |

## Power and cost

At the wall, typical for this hardware (measure with a plug-in meter for real numbers; the 1000 W PSU
rating is the maximum it can deliver, not what it draws):

| State | Draw | Per month if held 24/7, at ~36 yen/kWh |
|---|---|---|
| On, idle | 80-130 W | 2,100-3,400 yen |
| Suspended (S3, `[deep]`) | 3-8 W | 80-210 yen |
| Off, Wake-on-LAN armed | 1-3 W | 30-80 yen |

## Files

| Path | Runs on | What |
|---|---|---|
| `app/activity.py` | homelabai | what counts as gateway activity; `GET /internal/activity` |
| `app/power.py`, `POST /admin/power/suspend` in `app/routers/admin.py` | homelabai | sleep on request |
| `ops/idle-shutdown/` | homelabai | the idle timer: script, config, systemd units |
| `wake/` | proxy-vm | the wake front, its tests, Docker and systemd files |
| `bin/deploy.sh` | anywhere | deploys the gateway (holds the deploy lock while it runs) |
| `bin/deploy-wake.sh` | proxy-vm | deploys the wake front |
| `bin/wake-homelabai.sh` | proxy-vm | wakes homelabai and waits for the gateway |
| `Jenkinsfile`, `ci/checks.sh` | Jenkins on proxy-vm | the deployment pipeline ([deployment.md](deployment.md)) |
