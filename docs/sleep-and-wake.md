# Sleep and wake

homelabai (RTX 3090, 5950X) draws 80-130 W doing nothing. It now suspends itself after a few idle
minutes and wakes when someone actually uses the gateway, so a long task that finishes at 3 a.m. no
longer leaves it running until morning.

- [What runs where](#what-runs-where)
- [How a request is handled](#how-a-request-is-handled)
- [When it goes to sleep](#when-it-goes-to-sleep)
- [Changing the idle timeout](#changing-the-idle-timeout)
- [Doing it by hand](#doing-it-by-hand)
- [Setting it up](setup-guide.md)
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
`ai-gateway.mainul35.dev` belongs on proxy-vm, pointing at the wake front ([setup guide, Part 3](setup-guide.md#part-3--cloudflare-send-the-public-address-to-the-wake-front)).

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

Follow [setup-guide.md](setup-guide.md). It goes step by step, says which machine each step runs on,
and ends every step with a check.

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

See [setup-guide.md → If something goes wrong](setup-guide.md#if-something-goes-wrong).

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
| `jenkins/`, `ci/checks.sh` | Jenkins on proxy-vm | the build and deploy jobs ([deployment.md](deployment.md)) |
