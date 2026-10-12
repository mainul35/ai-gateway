# Deployment

The gateway is built and deployed by two Jenkins jobs on `jenkins.mainul35.dev` (proxy-vm), the same
way as the VSD services in `vsd-auth-ops`. Nothing is triggered by GitHub: no GitHub Actions, no
webhooks. You run both jobs by hand.

```
build-ai-gateway   (Build with Parameters → BRANCH)
  checkout BRANCH → ci/checks.sh → build gateway-release + gateway-wake-front
  → push both to Harbor (localhost:5000/ai-gateway/...) as <short commit> and latest

deploy-ai-gateway  (Build Now → pick a tag)
  pull both images for the tag
  → wake homelabai if asleep (through the wake front)
  → deploy the gateway to homelabai: that commit's bin/deploy.sh over ssh
       (backup, sync, requirements, restart, health check, automatic rollback)
  → deploy the wake front on proxy-vm (docker compose, project gateway-wake)
  → smoke test → prune Harbor tags beyond the newest 20
```

Rollback: run `deploy-ai-gateway` again and pick the previous tag.

**Setting it up** (Harbor project and robot, three Jenkins credentials, the two jobs, the `BRANCH`
dropdown): [`jenkins/SETUP.md`](../jenkins/SETUP.md).

## Files

| Path | What |
|---|---|
| `jenkins/build-ai-gateway.Jenkinsfile` | the build job |
| `jenkins/deploy-ai-gateway.Jenkinsfile` | the deploy job |
| `jenkins/release.Dockerfile` | packs one commit's code into the `gateway-release` image |
| `jenkins/SETUP.md` | step-by-step setup |
| `ci/checks.sh` | the checks the build job runs; run it locally too |
| `bin/deploy.sh` | deploys the gateway's code to homelabai (used by the deploy job, and by hand) |
| `bin/wake-homelabai.sh` | wakes homelabai and waits for the gateway |
| `bin/deploy-wake.sh` | builds and deploys the wake front on proxy-vm by hand, without Jenkins |

## Without Jenkins

Everything the jobs do can be done by hand:

```bash
ci/checks.sh                                                   # the checks
DEPLOY_HOST=local bin/deploy.sh                                # on homelabai, from a clone
DEPLOY_HOST=mainul35@192.168.50.146 bin/deploy.sh              # from proxy-vm (wake it first: bin/wake-homelabai.sh)
bin/deploy-wake.sh                                             # wake front, on proxy-vm, settings in ~/gateway-wake/wake.env
```

A wake front deployed by hand and one deployed by Jenkins are the same container (compose project
`gateway-wake`); the next run of either replaces it.

## What deploys never touch

These need root or hold secrets, so they stay manual ([sleep-and-wake.md](sleep-and-wake.md),
"Setting it up"):

- `config/config.properties` on homelabai (secrets, SSO)
- the idle timer in `/usr/local/sbin` and `/etc/default/idle-shutdown`. When a deploy brings a newer
  `idle-shutdown.sh`, the *Deploy gateway* console prints the one `sudo install` line to run
- sudo, UFW and Cloudflare tunnel settings

## Deploys and sleep

The deploy job wakes homelabai before deploying. While `bin/deploy.sh` runs, `/tmp/gateway-deploying`
exists on the server, which the idle timer treats as work, so the machine never suspends halfway
through a deploy; if a deploy dies, the lock stops counting after 30 minutes. Afterwards the machine
goes back to sleep on its own once it has been idle for `IDLE_MINUTES`. The build job never wakes
homelabai: it runs entirely on proxy-vm.
