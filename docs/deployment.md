# Deployment pipeline

Merging to `develop` deploys. Nobody has to wake homelabai, SSH anywhere or run a script.

```
push / merge to develop
        │
        ▼
GitHub Actions CI  (.github/workflows/ci.yml, GitHub's own runners)
  compile · control characters · secret scan · shell syntax · wake front tests
        │  green
        ▼
deployer on proxy-vm  (ops/deployer, every 2 min, pulls from GitHub)
  1. new commit on develop?            no  → nothing to do
  2. CI passed?                        no  → skip that commit for good; pending → check again next run
  3. homelabai asleep?                 yes → wake it, wait for the gateway  (bin/wake-homelabai.sh)
  4. deploy the gateway to homelabai   bin/deploy.sh over ssh: compile check, back up the running
                                       code, sync, install changed requirements, restart, health
                                       check, automatic rollback, public URL check
  5. deploy the wake front here        bin/deploy-wake.sh: rebuild, restart only if it changed,
                                       health check, automatic rollback
  6. remember the commit as deployed
```

## Why it pulls

The obvious design is a GitHub Actions self-hosted runner on proxy-vm. The repository is public, and
a self-hosted runner on a public repository runs workflows from other people's pull requests,
on your LAN. Pulling avoids that entirely: proxy-vm asks GitHub for new commits, nothing from the
internet can start anything here, and the CI that runs other people's code runs on GitHub's machines,
which can't reach the LAN. The cost is up to two minutes between a green CI and the deploy starting.

## One-time setup (on proxy-vm, as `proxt`)

```bash
# 1. An ssh key homelabai accepts, so deploys need no password
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519      # skip if ~/.ssh/id_ed25519 exists
ssh-copy-id mainul35@192.168.50.146                    # asks for mainul35's password once
ssh -o BatchMode=yes mainul35@192.168.50.146 true && echo ok

# 2. The pipeline's own clone (separate from ~/ai-gateway-src, which stays yours to play in)
git clone -b develop https://github.com/mainul35/ai-gateway ~/gateway-deployer/src

# 3. Settings, and the timer
sudo cp ~/gateway-deployer/src/ops/deployer/gateway-deployer.conf /etc/default/gateway-deployer
sudo cp ~/gateway-deployer/src/ops/deployer/gateway-deployer.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gateway-deployer.timer

# 4. The first deploy, watched
sudo systemctl start gateway-deployer
journalctl -u gateway-deployer -f
```

The service runs as `proxt` (`User=` in `gateway-deployer.service`); change it there if the account
differs. That account needs the ssh key from step 1 and permission to run `docker`.

The wake front's settings stay in `~/gateway-wake/wake.env`, outside every clone. The first pipeline
deploy copies them there from `~/ai-gateway-src/wake/wake.env` if you set it up by hand earlier, and
replaces the container you started by hand.

On GitHub, protect `develop` (Settings → Branches) so only you can push to it: whatever lands there
runs on both machines. While there, Settings → Actions → General → "Require approval for all
outside collaborators" keeps strangers' pull requests from using your Actions minutes.

## Day to day

| To | Do |
|---|---|
| Deploy | merge to `develop`. Within ~2 min of CI going green it is live |
| Watch a deploy | `journalctl -u gateway-deployer -f` on proxy-vm |
| See what is deployed | `cat ~/gateway-deployer/state/deployed` on proxy-vm |
| Deploy the branch head again (e.g. after fixing something on a server) | `FORCE=1 ~/gateway-deployer/src/ops/deployer/gateway-deployer.sh` (also skips the CI check) |
| Deploy without waiting for CI | `REQUIRE_CI=0` in `/etc/default/gateway-deployer` |
| Pause deploys | `sudo systemctl stop gateway-deployer.timer` (`start` to resume) |
| Roll back | `git revert` the commit and push; the pipeline deploys the revert. On the server, the last 5 deployed versions are also kept in `~/model-gateway/.deploy-backup/` |
| Deploy by hand, no pipeline | `DEPLOY_HOST=mainul35@192.168.50.146 bin/deploy.sh` and `bin/deploy-wake.sh` from any clone on proxy-vm, or `DEPLOY_HOST=local bin/deploy.sh` on homelabai |

A failed deploy has already been rolled back by the script that failed; the log says which step and
why. A commit whose CI failed is skipped and never retried; the next commit is considered normally.

## What the pipeline does not touch

These need root or hold secrets, so they stay manual (all in [sleep-and-wake.md](sleep-and-wake.md),
"Setting it up"):

- `config/config.properties` on homelabai (secrets, SSO) and `~/gateway-wake/wake.env` on proxy-vm
- the idle timer installed in `/usr/local/sbin`, and `/etc/default/idle-shutdown`. When a deploy ships
  a newer `idle-shutdown.sh`, the deploy log prints the one `sudo install` line to run
- sudo, UFW and Cloudflare tunnel settings

## Deploys and sleep

A deploy starts by waking homelabai if needed, and while `bin/deploy.sh` runs, `/tmp/gateway-deploying`
exists on the server, which the idle timer treats as work, so the machine
never suspends halfway through a deploy. If a deploy dies, the lock stops counting after 30 minutes.
After the deploy, the machine goes back to sleep on its own once it has been idle for
`IDLE_MINUTES`.
