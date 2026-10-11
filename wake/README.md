# Wake front

homelabai sleeps when nobody is using it (`ops/idle-shutdown`). This small proxy runs on proxy-vm,
which never sleeps, and stands between Cloudflare Tunnel and the gateway:

```
Cloudflare Tunnel ──► wake front (proxy-vm :9100) ──► gateway (homelabai :9000)
                         │
                         └─ asleep? Wake-on-LAN packet to homelabai
```

While homelabai is awake every request passes straight through, streamed answers included. While it
is asleep, a request someone meant to make wakes it:

| Request | While waking |
|---|---|
| Streamed completion (`"stream": true`) | SSE headers at once, `: waking` comments every 10 s, then the real stream. No Cloudflare 524 however long it takes |
| Other API call | held up to `WAKE_HOLD_SECONDS` (90), then answered; `503` + `Retry-After: 30` if still waking |
| Page opened in a browser | a "waking up" page that reloads itself when the gateway answers |
| A tab polling in the background | `503`, wakes nothing |
| `/health` | `503`, wakes nothing (so `bin/deploy.sh` and monitors never wake it) |

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
cd wake
cp wake.env.example wake.env       # set WAKE_UPSTREAM to homelabai's LAN address
docker compose up -d --build       # host networking: WoL broadcasts must reach the LAN
curl -s localhost:9100/__wake/status
```

Or without Docker, see `gateway-wake-front.service`.

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
