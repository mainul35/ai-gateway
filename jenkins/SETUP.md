# ai-gateway — Jenkins & Harbor setup (step by step)

End-to-end setup for the `build-ai-gateway` and `deploy-ai-gateway` jobs on `proxy-vm`'s Jenkins
(`jenkins.mainul35.dev`). Same pattern as `vsd-auth-ops`: build pushes to Harbor, deploy picks a tag.
Nothing is triggered by GitHub; you run both jobs by hand. Do the sections in order.

---

## 0. What the two jobs do

| Job | Jenkinsfile | Does |
|---|---|---|
| `build-ai-gateway` | `jenkins/build-ai-gateway.Jenkinsfile` | checks out a chosen **branch** → runs `ci/checks.sh` → builds two images → pushes both to Harbor as `<short commit>` and `latest`. Never deploys |
| `deploy-ai-gateway` | `jenkins/deploy-ai-gateway.Jenkinsfile` | asks which **tag** → wakes homelabai → deploys the gateway to homelabai → deploys the wake front on proxy-vm → smoke test → prunes old tags. Never builds |

The two images, both in Harbor project `ai-gateway`, always under the same tag:

| Image | Holds | Deployed to |
|---|---|---|
| `gateway-release` | the gateway's code for that commit (`jenkins/release.Dockerfile`, from `git archive`) | **homelabai**: the deploy job copies the code out and runs that commit's `bin/deploy.sh` over ssh (backup, sync, requirements, restart, health check, automatic rollback). The gateway itself keeps running from its venv, as before: it needs the GPU machine's llama.cpp, ComfyUI, models and project folders, so it is not containerised |
| `gateway-wake-front` | the wake front (`wake/`) | **proxy-vm**: `docker compose`, project `gateway-wake`, host networking |

Where settings live:

| Kind | Example | Where |
|---|---|---|
| Pipeline constants | `REGISTRY`, `GATEWAY_SSH`, `WAKE_FRONT_URL` | the `environment { }` block of each Jenkinsfile |
| Wake front settings | `WAKE_UPSTREAM`, `WAKE_MAC`, `WAKE_SESSION_SECRET` | **one file**, the Jenkins secret file `gateway-wake-env` (Step 2c) |
| Gateway settings and secrets | `gateway.master.key`, SSO, database | `~/model-gateway/config/config.properties` on homelabai. Never touched by a deploy |
| Per-run choices | which branch to build, which tag to deploy | job parameters (the `BRANCH` dropdown, the tag prompt) |

---

## 1. Harbor (admin account)

1. Log into `https://harbor.mainul35.dev` as `admin`.
2. **Projects → New Project** → Name `ai-gateway`, Access Level **Private** → OK.
3. Create the robot **inside the project** (not *Administration → Robot Accounts*, which makes a
   system robot without repository scopes): **Projects → `ai-gateway` → Robot Accounts tab →
   + New Robot Account**.
   - Name: type only `jenkins-ai-gateway`. Harbor shows the full login username
     `robot$ai-gateway+jenkins-ai-gateway` after Create; that full string is the Jenkins username.
   - Expiration: never (or your policy).
   - Permissions:

     | Resource | Check |
     |---|---|
     | Repository | **Pull**, **Push** |
     | Artifact | **List**, **Read**, **Delete** |
     | Tag | **List** |

     Pull/Push move the images; Artifact List/Read + Tag List feed the deploy job's tag prompt;
     Artifact Delete lets it prune old tags.
   - Create → **copy the secret now** (shown once).

---

## 2. Jenkins credentials

All in **Manage Jenkins → Credentials → System → Global credentials (unrestricted) → + Add
Credentials**. Type the **ID** exactly as written: the Jenkinsfiles look credentials up by ID.

### 2a. Harbor robot — `harbor-robot-ai-gateway`
- **Kind**: Username with password
- **Username**: `robot$ai-gateway+jenkins-ai-gateway` (as Harbor showed it)
- **Password**: the robot secret from Step 1
- **ID**: `harbor-robot-ai-gateway`

### 2b. SSH key for homelabai — `homelabai-ssh`
A key used only for deploys. On proxy-vm:

```bash
ssh-keygen -t ed25519 -N '' -C jenkins-deploy -f ~/jenkins-deploy
ssh-copy-id -i ~/jenkins-deploy.pub mainul35@192.168.50.146     # asks mainul35's password once
ssh -i ~/jenkins-deploy -o BatchMode=yes mainul35@192.168.50.146 true && echo ok
cat ~/jenkins-deploy                                             # copy this into the credential
```

- **Kind**: SSH Username with private key
- **Username**: `mainul35`
- **Private Key**: Enter directly → paste the contents of `~/jenkins-deploy`
- **ID**: `homelabai-ssh`

Then `rm ~/jenkins-deploy ~/jenkins-deploy.pub`. The first deploy accepts homelabai's host key and
every later one checks it.

### 2c. Wake front settings — `gateway-wake-env`
Your current settings are already in a file: `~/ai-gateway-src/wake/wake.env` on proxy-vm (the
template is `wake/wake.env.example`). Copy it to the machine with your browser.

- **Kind**: Secret file
- **File**: that `wake.env`
- **ID**: `gateway-wake-env`

To change a setting later, edit the file and use **Update** on the credential to upload it again,
then run `deploy-ai-gateway`.

### Verify
The global credentials list shows `harbor-robot-ai-gateway`, `homelabai-ssh` and `gateway-wake-env`.

---

## 3. What the Jenkins container needs

Nothing new, if it is set up the way the VSD jobs need it:

- the host's Docker socket mounted, with the `docker` CLI and compose plugin inside (every
  `docker` command runs on proxy-vm's own daemon, which is why `localhost:5000` reaches Harbor)
- Jenkins home as a Docker **volume** and the container's hostname left as its ID: the Checks stage
  runs `python:3.12` with `--volumes-from` the Jenkins container, exactly like the enterprise
  build's Maven stage, because the Jenkins image has no Python
- `git`, `curl`, `tar` and an `ssh` client in the Jenkins image (the official image has them)

Network: from inside the Jenkins container, `http://192.168.50.146:9000` (the gateway) and
`http://192.168.50.81:9100` (the wake front) must answer. Its traffic leaves proxy-vm from
`192.168.50.81`, which homelabai's firewall already allows on 9000. The deploy job wakes homelabai
by calling the wake front, because a broadcast from the Jenkins container never reaches the LAN.

---

## 4. Create the BUILD job (`build-ai-gateway`)

1. **New Item** → name `build-ai-gateway` → **Pipeline** (not Freestyle, not Multibranch) → OK.
2. *(Optional)* Description: `Build ai-gateway from a chosen branch and push to Harbor`.
3. **Pipeline** section:
   - **Definition**: Pipeline script from SCM
   - **SCM**: Git, **Repository URL** `https://github.com/mainul35/ai-gateway.git`, **Credentials**
     `- none -` (public repository)
   - **Branch Specifier**: `*/develop`. This is where the *Jenkinsfile* is read from, not the
     branch that gets built. Until pull request #1 is merged it only exists on
     `*/feature/sleep-when-idle`; use that, and switch to `*/develop` after the merge.
   - **Script Path**: `jenkins/build-ai-gateway.Jenkinsfile`
   - leave *Lightweight checkout* ticked
4. **Build Triggers**: none. You build on demand.
5. **Save**, then add the `BRANCH` dropdown (4a).

### 4a. `BRANCH` dropdown — Active Choices

The Jenkinsfile has no `parameters { }` block on purpose (it would overwrite this one every run).

1. `build-ai-gateway` → **Configure** → tick **This project is parameterized** → **Add Parameter →
   Active Choices Parameter**.
2. **Name**: `BRANCH`. **Choice Type**: Single Select.
3. **Script** → Groovy Script, untick **Use Groovy Sandbox**, and paste:

   ```groovy
   try {
       def branches = ['git', 'ls-remote', '--heads', 'https://github.com/mainul35/ai-gateway.git']
           .execute().text.readLines()
           .collect { it.replaceAll(/^.*refs\/heads\//, '') }
           .findAll { it }
           .sort()
       // develop first: it is what gets deployed
       return branches.contains('develop') ? ['develop'] + (branches - 'develop') : (branches ?: ['develop'])
   } catch (ignored) {
       return ['develop']
   }
   ```

4. **Fallback Script**: `return ['develop']`.
5. **Save**. If Jenkins lists the script under **Manage Jenkins → In-process Script Approval**,
   approve it.
6. **Build with Parameters** → `BRANCH` lists the repository's branches → pick one → **Build**.
   The console ends with `Pushed <sha> (gateway-release + gateway-wake-front) -- pick tag '<sha>'
   in deploy-ai-gateway`.

---

## 5. Create the DEPLOY job (`deploy-ai-gateway`)

1. **New Item** → name `deploy-ai-gateway` → **Pipeline** → OK.
2. **Pipeline** section: same as §4 step 3, with **Script Path**
   `jenkins/deploy-ai-gateway.Jenkinsfile`.
3. **Build Triggers**: none. Never auto-deploy.
4. **Save**. No parameters to add.

### Run a deploy

1. **Build Now**. It pauses at *Select image tag* (amber in Stage View, or *Paused for Input* in the
   build's menu). That is the prompt, not a hang.
2. The dropdown lists the last 15 tags, newest first, e.g.
   `a1b2c3d  (2026-10-12 10:00:01 UTC, develop) [latest]`. Pick one → **Proceed**. (**Abort**
   changes nothing.)
3. It then runs on its own:
   - **Pull release** — pulls both images for that tag, copies the gateway's code out
   - **Wake homelabai** — asks the wake front to wake it, waits until the gateway answers
   - **Deploy gateway** — that commit's `bin/deploy.sh` over ssh; it rolls itself back if the
     gateway does not come up healthy. While it runs, homelabai will not go to sleep
   - **Deploy wake front** — `docker compose up` with the tag's image and `gateway-wake-env`
   - **Smoke test** — the gateway answers `/health`, and the wake front reports it up
   - **Prune old Harbor tags** — keeps the newest 20 tags of each image; failures here never fail
     the deploy

**Rollback:** run `deploy-ai-gateway` again and pick the previous tag.

---

## 6. First end-to-end run

1. **Build**: `build-ai-gateway` → BRANCH `feature/sleep-when-idle` (or `develop` once merged) →
   note the tag.
2. **Deploy**: `deploy-ai-gateway` → pick that tag → Proceed. This first deploy replaces the wake
   front container you started by hand; from now on Jenkins owns it.
3. **Verify**, on proxy-vm:
   ```bash
   docker inspect gateway-wake-front --format '{{.Config.Image}}'
   # localhost:5000/ai-gateway/gateway-wake-front:<tag>
   curl -s localhost:9100/__wake/status          # {"up":true,...}
   ```
   and on homelabai: `curl -s localhost:9000/internal/activity` answers.

---

## 7. Gotchas

- **Compose project `gateway-wake`.** The deploy job pins it (`COMPOSE_PROJECT_NAME`); `bin/deploy-wake.sh`
  uses the same name. Do not run `docker compose up` for the wake front by hand without
  `-p gateway-wake`, or you get a second project fighting over the container name.
- **The repo-root `docker-compose.yml` is the old model checker,** not the gateway or the wake
  front. Port 5000 on proxy-vm is Harbor.
- **The idle timer is not deployed by Jenkins.** It needs root. When a deploy brings a newer
  `ops/idle-shutdown/idle-shutdown.sh`, the *Deploy gateway* console prints the `sudo install` line
  to run on homelabai.
- **Robot secret expiry.** If *Check registry credentials* fails, the robot expired or its secret
  was refreshed: update `harbor-robot-ai-gateway`.
- **A deploy that cannot wake homelabai** stops at *Wake homelabai* after 5 minutes, before touching
  anything. Check `curl -s localhost:9100/__wake/status` on proxy-vm and the wake front's logs
  (`docker logs gateway-wake-front`).
