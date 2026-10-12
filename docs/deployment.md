# Deployment pipeline

The gateway is built and deployed by Jenkins (`jenkins.mainul35.dev`, on proxy-vm), from the
`Jenkinsfile` at the root of this repository. Merging to `develop` deploys; nobody has to wake
homelabai, SSH anywhere or run a script.

```
push / merge
     │  GitHub webhook (or a periodic scan)
     ▼
Jenkins on proxy-vm
  Checks                 every branch and pull request    ci/checks.sh: compile, control characters,
                                                           secret scan, shell syntax, wake front tests
  ── develop only, after the checks pass ──
  Wake homelabai         through the wake front, waits for the gateway   bin/wake-homelabai.sh
  Deploy gateway         over ssh to homelabai                            bin/deploy.sh
                         compile check, back up the running code, sync, install changed
                         requirements, restart, health check, automatic rollback, public URL check
  Deploy wake front      on proxy-vm                                       bin/deploy-wake.sh
                         rebuild, restart only if it changed, health check, automatic rollback
```

Jenkins runs on proxy-vm, which never sleeps and sits on the same LAN as homelabai, so it can wake it,
reach its gateway and SSH into it directly. The checks are a script (`ci/checks.sh`), so they run the
same way on your machine.

## One-time setup

### 1. The agent's tools

The pipeline runs on `agent any`: on a single-node Jenkins, the built-in node on proxy-vm. That node
needs:

| Tool | For | Check |
|---|---|---|
| `bash`, `git`, `curl`, `tar` | everything | usually present |
| `python3` with `venv` | the checks, and the compile check in `deploy.sh` | `python3 -m venv /tmp/x && echo ok` |
| `ssh` client | deploying to homelabai | `ssh -V` |
| `docker` CLI with the compose plugin, allowed to use proxy-vm's Docker | deploying the wake front | `docker compose version && docker ps` |

If Jenkins itself runs in a container, mount the host's `/var/run/docker.sock` and install the docker
CLI and compose plugin in the image (or use an agent that has them). Waking still works from inside a
container, because the pipeline wakes homelabai by calling the wake front at
`http://192.168.50.81:9100`, not with its own broadcast. Its requests to homelabai leave proxy-vm
from `192.168.50.81`, which homelabai's firewall already allows on port 9000.

Run these checks in a throwaway Jenkins job with an `sh` step, so they see what Jenkins sees (its
user, its PATH, its container if it has one).

### 2. Credentials (Manage Jenkins → Credentials → System → Global)

| ID | Kind | Contents |
|---|---|---|
| `homelabai-ssh` | SSH Username with private key | username `mainul35`, and a private key homelabai accepts |
| `gateway-wake-env` | Secret file | the wake front's settings: `wake/wake.env.example`, filled in (your current `~/ai-gateway-src/wake/wake.env` on proxy-vm) |

For the SSH key, a key just for deploys:

```bash
# anywhere
ssh-keygen -t ed25519 -N '' -C jenkins-deploy -f jenkins-deploy
ssh-copy-id -i jenkins-deploy.pub mainul35@192.168.50.146
# paste the contents of jenkins-deploy (the private key) into the credential, then delete both files
```

The pipeline accepts homelabai's host key on first contact and checks it on every deploy after that.

### 3. The job

New Item → **Multibranch Pipeline**, named e.g. `ai-gateway`:

- Branch Sources → GitHub, repository `https://github.com/mainul35/ai-gateway` (public, so no
  credentials needed to read it)
- Behaviours: discover branches; discover pull requests from origin; discover pull requests from
  forks with **Trust: From users with Admin or Write permission**. The repository is public, and this
  keeps a stranger's pull request from changing the `Jenkinsfile` that runs on proxy-vm (theirs runs
  with your `Jenkinsfile`, and only the checks, never the deploy stages)
- Build Configuration: by Jenkinsfile, path `Jenkinsfile`
- Scan Repository Triggers: periodically, every 5 minutes, as a fallback for missed webhooks

Then on GitHub, Settings → Webhooks → Add webhook: payload URL
`https://jenkins.mainul35.dev/github-webhook/`, content type `application/json`, events "push" and
"pull requests". If `jenkins.mainul35.dev` is behind Cloudflare Access, add a bypass for the
`/github-webhook/` path, or the webhook never gets through (the periodic scan still works).

A plain Pipeline job pointed at `develop` works too; the deploy stages recognise `develop` either way.

### 4. First run

Run the `develop` branch job (Build Now) and watch the stages. The first wake-front deploy replaces the
container you started by hand from `~/ai-gateway-src/wake`; from then on it runs from the job's
workspace with the settings in `gateway-wake-env`. Check afterwards:

```bash
curl -s localhost:9100/__wake/status          # on proxy-vm: {"up": true, ...}
docker ps --filter name=gateway-wake-front    # running, from compose project gateway-wake
```

On GitHub, protect `develop` (Settings → Branches) so only you can push to it: whatever lands there
runs on both machines.

## Day to day

| To | Do |
|---|---|
| Deploy | merge to `develop` |
| Watch a deploy | the `develop` build in Jenkins (stage view, console output) |
| Deploy again without a new commit | Build Now on the `develop` branch job |
| Pause deploys | disable the job (or the `develop` branch job) |
| Roll back | `git revert` the commit and push; the revert deploys like anything else. On the server, the last 5 deployed versions are also kept in `~/model-gateway/.deploy-backup/` |
| Change wake front settings | update the `gateway-wake-env` credential, then Build Now on `develop`. Docker Compose sees the changed settings and recreates the container |
| Deploy by hand, no Jenkins | `DEPLOY_HOST=local bin/deploy.sh` on homelabai, or `DEPLOY_HOST=mainul35@192.168.50.146 bin/deploy.sh` and `WAKE_ENV_FILE=… bin/deploy-wake.sh` from a clone on proxy-vm |
| Run the checks locally | `ci/checks.sh` |

A failed deploy step has already rolled itself back; the console output says which step and why. If
the checks fail, nothing is deployed.

## What the pipeline does not touch

These need root or hold secrets, so they stay manual (all in [sleep-and-wake.md](sleep-and-wake.md),
"Setting it up"):

- `config/config.properties` on homelabai (secrets, SSO)
- the idle timer installed in `/usr/local/sbin`, and `/etc/default/idle-shutdown`. When a deploy ships a
  newer `idle-shutdown.sh`, the console output prints the one `sudo install` line to run
- sudo, UFW and Cloudflare tunnel settings

## Deploys and sleep

The pipeline wakes homelabai before deploying. While `bin/deploy.sh` runs, `/tmp/gateway-deploying`
exists on the server, which the idle timer treats as work, so the machine never suspends halfway
through a deploy; if a deploy dies, the lock stops counting after 30 minutes. Afterwards the machine
goes back to sleep on its own once it has been idle for `IDLE_MINUTES`.

Pull request and feature-branch builds only run the checks, on proxy-vm; they never wake homelabai.
