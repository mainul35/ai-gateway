# Wake front

homelabai sleeps when nobody is using it (`ops/idle-shutdown`). This small proxy runs on proxy-vm,
which never sleeps, and stands between Cloudflare Tunnel and the gateway:

```
Cloudflare Tunnel ──► wake front (proxy-vm :9100) ──► gateway (homelabai :9000)
                         │
                         └─ asleep? Wake-on-LAN packet to homelabai
```

While homelabai is awake every request passes straight through, streamed answers included. While it
is asleep, an authorised request wakes it and is answered once the gateway is up:

| Request | While waking |
|---|---|
| Streamed completion (`"stream": true`) | SSE headers at once, `: waking` comments every 10 s, then the real stream. No Cloudflare 524 however long it takes |
| Page opened in a browser | a "waking up" page that reloads itself when the gateway answers |
| Any other request, API call or a click in an open page | held until the gateway answers, then answered (after 90 s: `503` + `Retry-After: 30`) |
| A page's timer polling (dashboard stats, task feeds, remote-folder long-poll) | `503`, wakes nothing |
| An app's model check (`GET /v1/models` with a key, e.g. the Claude desktop app) | the list last served to that same key, wakes nothing; with none kept yet, wakes the machine once to get it |
| `/health` | `503`, wakes nothing (so `bin/deploy.sh` and monitors never wake it) |
| A launcher download (`/downloads/...`) or a static file (`/static/...`) | served from the copy kept the last time the gateway served it (the launchers are also fetched every 30 min while it is up); wakes nothing |

Coming back from suspend takes a few seconds, so most of the time nobody notices. From power-off it
is a full boot, which is what the holding and keep-alives are for.

## Who can wake it

Anything that reaches the public hostname could otherwise keep the machine awake. A request wakes it
only if it carries:

- an API key the gateway has accepted since the wake front started, or the master key
  (`WAKE_MASTER_KEY_SHA256`)
- a login cookie with a valid signature (needs `WAKE_SESSION_SECRET`)
- Cloudflare Access's `Cf-Access-Jwt-Assertion`, i.e. it came through an Access-protected path
- an unknown key shaped like a gateway key (`sk-` + 43 characters): once per
  `WAKE_UNVERIFIED_COOLDOWN` (10 min). If the gateway then answers 401, that key never wakes it again

The asleep page has a "Wake it up" button for when you have no valid cookie yet; it is rate-limited.
The gateway still authenticates every request itself — this only decides who may switch it on.

## Install on proxy-vm

```bash
mkdir -p ~/gateway-wake && cp wake/wake.env.example ~/gateway-wake/wake.env
nano ~/gateway-wake/wake.env       # WAKE_UPSTREAM=http://192.168.50.146:9000, WAKE_SESSION_SECRET=...
bin/deploy-wake.sh                 # builds and starts it from ~/gateway-wake, rolls back on failure
curl -s localhost:9100/__wake/status
```

Host networking is required (WoL broadcasts must reach the LAN). Without Docker, see
`gateway-wake-front.service`. After that, the Jenkins job `deploy-ai-gateway` deploys it
([docs/deployment.md](../docs/deployment.md)); the full guide is [docs/sleep-and-wake.md](../docs/sleep-and-wake.md).

Tests: `pip install -r wake/requirements.txt pytest && pytest wake/`

Then point the tunnel at it instead of at homelabai. In proxy-vm's `cloudflared` config:

```yaml
ingress:
  - hostname: ai-gateway.mainul35.dev
    service: http://localhost:9100
```

If homelabai runs its own `cloudflared` for this hostname, remove the hostname there; a connector on
a sleeping machine is what produced the 1033 errors.

## On homelabai

- Let proxy-vm reach the gateway: `sudo ufw allow from <proxy-vm IP> to any port 9000 proto tcp`
- Wake-on-LAN must work from suspend as well as from power-off:
  `cat /sys/class/net/enp3s0/device/power/wakeup` should say `enabled`, and
  `sudo ethtool enp3s0 | grep Wake-on` should say `g`.
- Install `ops/idle-shutdown` (its README section is in the main README).

## Testing it

```bash
curl -s https://ai-gateway.mainul35.dev/__wake/status          # {"up": true, "waking": false, ...}
sudo systemctl suspend                                            # on homelabai
curl -N https://ai-gateway.mainul35.dev/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b-tuned","stream":true,"messages":[{"role":"user","content":"hi"}]}'
# ": waking homelabai" lines, then the answer
```

Logs: `docker logs -f gateway-wake-front` (one line per wake, with what caused it).
