# Setup guide: sleep, wake and deploy

This guide sets up everything from the start: homelabai sleeps when idle, wakes when someone uses
the gateway, and Jenkins builds and deploys it. Follow the parts in order. Each step ends with a
check, so you know it worked before you move on.

New here? Start with [Prerequisites](#prerequisites).
Already set up and just need to do something? Go to [Everyday tasks](#everyday-tasks).
Something failed? Go to [If something goes wrong](#if-something-goes-wrong).

---

## Before you start

### The machines

Every step starts with a coloured label that says where to do it.

| Label | Machine | Address | Log in as | The prompt looks like |
|---|---|---|---|---|
| 🟦 **homelabai** | The GPU server. Runs the gateway. Goes to sleep. | `192.168.50.146` | `mainul35` | `mainul35@homelabai:~$` |
| 🟩 **proxy-vm** | The always-on VM. Runs Jenkins, Harbor, the tunnel and the wake front. | `192.168.50.81` | `proxt` | `proxt@proxy-vm:~$` |
| ⬜ **Your PC** | Your Windows computer, in PowerShell. | — | — | `PS C:\Users\...>` |
| 🟨 **Browser** | Web pages: Cloudflare, Harbor, Jenkins. | — | — | — |

### Prerequisites

Tick these off before Part 1. Each has a quick way to check it. This guide does not cover installing
any of them.

**🟦 homelabai**

- [ ] Ubuntu, and you can use `sudo`.
- [ ] The gateway already runs from `~/model-gateway`, with its `.venv`, its database container and
      `config/config.properties` filled in.
      Check: `curl -s localhost:9000/health` prints `{"status":"ok"}`.
- [ ] `git`, `curl`, `ufw` and `ethtool` are installed.
      Check: `which git curl ufw ethtool` prints four paths.
- [ ] Wake-on-LAN is switched on in the BIOS ("Power On By PCI-E" on your ASRock board).
- [ ] It has a fixed address, `192.168.50.146`, for example a DHCP reservation on the router.
      Check: `ip -4 addr show enp3s0` shows `192.168.50.146`.

**🟩 proxy-vm**

- [ ] It is on the home network itself, not behind NAT. Its address starts with `192.168.50.`.
      Check: `ip -4 addr | grep 192.168.50` shows `192.168.50.81`.
- [ ] Docker with the compose plugin, plus `git`, `curl` and `watch`.
      Check: `docker compose version && which git curl watch`.
- [ ] A Cloudflare tunnel already runs here, the one that serves Jenkins and Harbor.
- [ ] Harbor runs at `https://harbor.mainul35.dev`, and on proxy-vm itself at `localhost:5000`.
- [ ] Jenkins runs at `https://jenkins.mainul35.dev`, set up like the VSD jobs need:
  - it runs in a Docker container, with proxy-vm's Docker socket mounted and the `docker` CLI inside
  - its home `/var/jenkins_home` is a Docker volume
  - these plugins are installed: Pipeline, Git, Credentials Binding, SSH Credentials,
    Active Choices, Timestamper

  Check: the VSD build and deploy jobs work. If they do, this is in place.

**⬜ Your PC**

- [ ] Windows PowerShell with `ssh` and `scp`. Windows 10 and 11 have them built in.
      Check: `scp` prints its usage text.
- [ ] You can log in to proxy-vm as `proxt` with a password or a key.

**🟨 Accounts**

- [ ] Cloudflare, with the `mainul35.dev` domain and Zero Trust (Networks → Tunnels).
- [ ] Harbor admin.
- [ ] Jenkins admin.
- [ ] The repository `https://github.com/mainul35/ai-gateway`. It is public, so no GitHub login is needed.

**Keep these at hand**

- [ ] An API key the gateway accepts, or its master key. You need it for the final test in Part 9.
- [ ] About an hour, with physical access to homelabai in case it does not wake up.

### Three habits that prevent most mistakes

1. **Check the prompt before you paste.** A command meant for proxy-vm does nothing useful on homelabai.
2. **Replace every `<placeholder>` before you press Enter.** Text inside `< >` is never typed as is.
3. **Do the check at the end of each step.** If it does not match, stop and look in
   [If something goes wrong](#if-something-goes-wrong).

### How the pieces fit

```
Internet ──► Cloudflare ──► proxy-vm: wake front :9100 ──► homelabai: gateway :9000
                                       │
                                       └─ homelabai asleep? Send a wake-up packet, wait, then pass the request on.

homelabai: idle timer ── nothing happening for 5 minutes ──► suspend
Jenkins on proxy-vm: build-ai-gateway ──► Harbor ──► deploy-ai-gateway ──► homelabai + proxy-vm
```

---

## Part 1 — homelabai

### 1.1 Get the code

🟦 **homelabai**

Download the repository into `~/ai-gateway-src`.

```bash
git clone -b develop https://github.com/mainul35/ai-gateway ~/ai-gateway-src
```

If `~/ai-gateway-src` already exists, update it instead:

```bash
cd ~/ai-gateway-src && git fetch && git checkout develop && git reset --hard origin/develop
```

✅ **Check:** `ls ~/ai-gateway-src/ops/idle-shutdown` lists four files.

### 1.2 Deploy the gateway once by hand

🟦 **homelabai**

This copies the code into `~/model-gateway` and restarts the gateway. Your settings in
`config/config.properties` are never touched. Later, Jenkins does this for you.

```bash
cd ~/ai-gateway-src && DEPLOY_HOST=local bin/deploy.sh
```

✅ **Check:** this prints `{"busy":false, ...}`.

```bash
curl -s localhost:9000/internal/activity
```

### 1.3 Let proxy-vm reach the gateway

🟦 **homelabai**

Open port 9000, for proxy-vm only.

```bash
sudo ufw allow from 192.168.50.81 to any port 9000 proto tcp
```

✅ **Check:** `sudo ufw status | grep 9000` shows the rule with `192.168.50.81`.

### 1.4 Allow the gateway to suspend the machine

🟦 **homelabai**

This lets the "sleep now" button work. It allows exactly one command and nothing else.

```bash
echo 'mainul35 ALL=(root) NOPASSWD: /usr/bin/systemctl suspend' | sudo tee /etc/sudoers.d/gateway-suspend
sudo chmod 440 /etc/sudoers.d/gateway-suspend
```

✅ **Check:** `sudo -n -l /usr/bin/systemctl suspend` prints `/usr/bin/systemctl suspend`.

### 1.5 Make sure it can sleep and wake properly

🟦 **homelabai**

Run these three checks.

```bash
cat /sys/power/mem_sleep
sudo ethtool enp3s0 | grep Wake-on
cat /sys/class/net/enp3s0/device/power/wakeup
```

✅ **Check:**

| Command | You want to see | Why |
|---|---|---|
| `mem_sleep` | `[deep]` in brackets | Real sleep, 3–8 W. Without it, sleep still uses 20–40 W. |
| `Wake-on` | a line `Wake-on: g` | The network card listens for the wake-up packet. |
| `wakeup` | `enabled` | The network card is allowed to wake the machine. |

### 1.6 Install the idle timer

🟦 **homelabai**

The idle timer checks every minute. After 5 quiet minutes it suspends the machine.
It starts in **test mode**: it only writes "would suspend" in the log.

```bash
cd ~/ai-gateway-src/ops/idle-shutdown
sudo install -m 755 idle-shutdown.sh /usr/local/sbin/idle-shutdown
sudo install -m 644 idle-shutdown.conf /etc/default/idle-shutdown
sudo install -m 644 idle-shutdown.service idle-shutdown.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now idle-shutdown.timer
```

✅ **Check:** `systemctl list-timers idle-shutdown.timer` shows the timer, with the next run less
than a minute away.

Leave test mode on for now. You switch it off in [step 9.2](#92-switch-off-test-mode).

---

## Part 2 — proxy-vm: the wake front

The wake front is a small program on proxy-vm. It passes every request to the gateway. If homelabai
is asleep, it wakes it first.

### 2.1 Get the code

🟩 **proxy-vm**

```bash
git clone -b develop https://github.com/mainul35/ai-gateway ~/ai-gateway-src
```

If it already exists: `cd ~/ai-gateway-src && git fetch && git checkout develop && git reset --hard origin/develop`

✅ **Check:** `ls ~/ai-gateway-src/wake` lists `wake_front.py`.

### 2.2 Write the settings file

🟦 **homelabai** — first, look up the gateway's session secret:

```bash
grep '^gateway.session.secret' ~/model-gateway/config/config.properties
```

Copy the value after the `=`. If it is empty, leave the setting empty below.

🟩 **proxy-vm** — then create the settings file:

```bash
cd ~/ai-gateway-src/wake
cp wake.env.example wake.env
nano wake.env
```

Set these lines, then save with `Ctrl+O`, `Enter`, `Ctrl+X`:

```
WAKE_UPSTREAM=http://192.168.50.146:9000
WAKE_MAC=a8:a1:59:9d:f4:ae
WAKE_BROADCAST=192.168.50.255
WAKE_SESSION_SECRET=<the value you copied>
```

✅ **Check:** `grep -E '^WAKE_(UPSTREAM|MAC|BROADCAST)' wake.env` shows the three lines above.

### 2.3 Start the wake front

🟩 **proxy-vm**

Run this inside the `wake` folder, **not** the repository's main folder. The main folder's compose
file is an old app that clashes with Harbor on port 5000.

```bash
cd ~/ai-gateway-src/wake && docker compose up -d --build
```

✅ **Check:** both commands print `{"status":"ok"}` or `{"up":true, ...}`.

```bash
curl -s -m 3 http://192.168.50.146:9000/health; echo
curl -s localhost:9100/__wake/status; echo
```

If the first one prints nothing, homelabai's firewall is blocking proxy-vm. Do [step 1.3](#13-let-proxy-vm-reach-the-gateway).

### 2.4 Test sleeping and waking

🟦 **homelabai** — put it to sleep. Your SSH window freezes. That is expected.

```bash
sudo systemctl suspend
```

🟩 **proxy-vm** — wait 20 seconds, then wake it:

```bash
curl -s -X POST localhost:9100/__wake/now; echo
watch -n 2 'curl -s localhost:9100/__wake/status'
```

✅ **Check:** within about 30 seconds, `"waking":true` changes to `"up":true`. Press `Ctrl+C` to
leave `watch`.

If it never wakes, press homelabai's power button. Then see
[If something goes wrong](#if-something-goes-wrong).

---

## Part 3 — Cloudflare: send the public address to the wake front

Right now `ai-gateway.mainul35.dev` points at homelabai. When homelabai sleeps, visitors get
error 1033. You now point it at the wake front on proxy-vm instead.

The site is unreachable between steps 3.2 and 3.3, so do them one right after the other.

### 3.1 Find where it points today

🟨 **Browser** — Cloudflare dashboard → **Networks → Tunnels**

Open each tunnel's **Public Hostname** tab. Find `ai-gateway.mainul35.dev`. Note which tunnel it is
in. This is the **old tunnel**.

✅ **Check:** you know the old tunnel's name, and you know proxy-vm's tunnel (it serves
Jenkins and Harbor).

### 3.2 Delete the old DNS record

🟨 **Browser** — Cloudflare dashboard → **mainul35.dev → DNS → Records**

Find the **CNAME** named `ai-gateway`. Click **Edit → Delete**.

✅ **Check:** `ai-gateway` is no longer in the DNS list.

### 3.3 Add the address to proxy-vm's tunnel

🟨 **Browser** — **Networks → Tunnels → proxy-vm's tunnel → Public Hostname → Add a public hostname**

| Field | Value |
|---|---|
| Subdomain | `ai-gateway` |
| Domain | `mainul35.dev` |
| Type | `HTTP` |
| URL | `192.168.50.81:9100` |

Click **Save**.

✅ **Check:** the DNS list from 3.2 shows `ai-gateway` again.

### 3.4 Remove the address from the old tunnel

🟨 **Browser** — **Networks → Tunnels → old tunnel → Public Hostname**

Delete `ai-gateway.mainul35.dev` there, if it is still listed.

### 3.5 Test it

⬜ **Your PC** (or any machine)

```bash
curl -s https://ai-gateway.mainul35.dev/__wake/status
```

✅ **Check:** it prints `{"up":true, ...}`. Then open `https://ai-gateway.mainul35.dev` in the
browser and log in as usual.

---

## Part 4 — Harbor: a place to keep the builds

### 4.1 Create the project

🟨 **Browser** — `https://harbor.mainul35.dev`, logged in as admin

**Projects → + New Project**. Name: `ai-gateway`. Leave **Public** unticked. Click **OK**.

✅ **Check:** the project page shows **Access Level: Private**.

### 4.2 Create the robot account

🟨 **Browser** — **Projects → ai-gateway → Robot Accounts → + New Robot Account**

Create it **inside the project**, not under Administration.

| Field | Value |
|---|---|
| Name | `jenkins-ai-gateway` |
| Expiration | Never |
| Repository | Pull, Push |
| Artifact | List, Read, Delete |
| Tag | List |

Click **Create**. Copy the **secret** now. Harbor shows it only once.

✅ **Check:** the list shows `robot$ai-gateway+jenkins-ai-gateway`, **6 permissions**, **Never Expires**.

---

## Part 5 — An SSH key for Jenkins

Jenkins logs in to homelabai with this key to deploy. homelabai does not accept passwords, so
`ssh-copy-id` does not work. These steps work around that.

### 5.1 Make the key

🟩 **proxy-vm**

```bash
ssh-keygen -t ed25519 -N '' -C jenkins-deploy -f ~/jenkins-deploy
```

✅ **Check:** `ls ~/jenkins-deploy*` shows `jenkins-deploy` and `jenkins-deploy.pub`.

### 5.2 Let homelabai accept the key

🟦 **homelabai**

This fetches the public key from proxy-vm and adds it. It asks for `proxt`'s password once. The
`from=` part means the key only works when it comes from proxy-vm.

```bash
echo "from=\"192.168.50.81\" $(ssh proxt@192.168.50.81 cat jenkins-deploy.pub)" >> ~/.ssh/authorized_keys
tail -1 ~/.ssh/authorized_keys
```

✅ **Check:** the last line starts with `from="192.168.50.81" ssh-ed25519 AAAA` and ends with
`jenkins-deploy`.

### 5.3 Test the key

🟩 **proxy-vm**

```bash
ssh -i ~/jenkins-deploy -o BatchMode=yes mainul35@192.168.50.146 true && echo ok
```

✅ **Check:** it prints `ok`.

### 5.4 Show the private key

🟩 **proxy-vm**

```bash
cat ~/jenkins-deploy
```

Copy all of it, including the first line (it says `BEGIN OPENSSH PRIVATE KEY`) and the last
line (it says `END OPENSSH PRIVATE KEY`).
You paste it in step 6.2. Then you delete these files in step 6.4.

---

## Part 6 — Jenkins credentials

All three are added the same way:

🟨 **Browser** — Jenkins → **Manage Jenkins → Credentials → System → Global credentials (unrestricted) → + Add Credentials**

Type each **ID** exactly as written. The pipelines look them up by ID.

### 6.1 Harbor robot

| Field | Value |
|---|---|
| Kind | Username with password |
| Username | `robot$ai-gateway+jenkins-ai-gateway` |
| Password | the secret from step 4.2 |
| ID | `harbor-robot-ai-gateway` |

Lost the secret? In Harbor, tick the robot → **Action → Refresh Secret**, and copy the new one.

### 6.2 SSH key

| Field | Value |
|---|---|
| Kind | SSH Username with private key |
| Username | `mainul35` |
| Private Key | Enter directly → paste the key from step 5.4 |
| ID | `homelabai-ssh` |

### 6.3 Wake front settings

⬜ **Your PC** — copy the settings file from proxy-vm to your Downloads folder:

```powershell
scp proxt@192.168.50.81:ai-gateway-src/wake/wake.env $HOME\Downloads\wake.env
```

🟨 **Browser** — add the credential:

| Field | Value |
|---|---|
| Kind | Secret file |
| File | `Downloads\wake.env` |
| ID | `gateway-wake-env` |

⬜ **Your PC** — delete the copy:

```powershell
Remove-Item $HOME\Downloads\wake.env
```

### 6.4 Delete the key files

🟩 **proxy-vm**

Jenkins has the key now. Remove the copies.

```bash
rm ~/jenkins-deploy ~/jenkins-deploy.pub
```

✅ **Check for Part 6:** the credentials list shows `harbor-robot-ai-gateway`, `homelabai-ssh` and
`gateway-wake-env`.

---

## Part 7 — Jenkins jobs

There are two jobs, both started by hand. Nothing on GitHub starts them.

- **build-ai-gateway** checks the code, then puts a build in Harbor.
- **deploy-ai-gateway** takes a build from Harbor and puts it on homelabai and proxy-vm.

### 7.1 Create the build job

🟨 **Browser** — Jenkins → **New Item**

Name: `build-ai-gateway`. Type: **Pipeline**. Click **OK**. Fill in the **Pipeline** section:

| Field | Value |
|---|---|
| Definition | Pipeline script from SCM |
| SCM | Git |
| Repository URL | `https://github.com/mainul35/ai-gateway.git` |
| Credentials | `- none -` (the repository is public) |
| Branch Specifier | `*/develop` |
| Script Path | `jenkins/build-ai-gateway.Jenkinsfile` |

Leave **Build Triggers** empty. Click **Save**.

### 7.2 Add the branch dropdown

🟨 **Browser** — `build-ai-gateway` → **Configure**

Tick **This project is parameterized** → **Add Parameter → Active Choices Parameter**.

| Field | Value |
|---|---|
| Name | `BRANCH` |
| Choice Type | Single Select |
| Script | Groovy Script, **untick "Use Groovy Sandbox"**, paste the script below |
| Fallback Script | `return ['develop']` |

```groovy
try {
    def branches = ['git', 'ls-remote', '--heads', 'https://github.com/mainul35/ai-gateway.git']
        .execute().text.readLines()
        .collect { it.replaceAll(/^.*refs\/heads\//, '') }
        .findAll { it }
        .sort()
    return branches.contains('develop') ? ['develop'] + (branches - 'develop') : (branches ?: ['develop'])
} catch (ignored) {
    return ['develop']
}
```

Click **Save**.

✅ **Check:** **Build with Parameters** shows a `BRANCH` list starting with `develop`. If the list is
empty, go to **Manage Jenkins → In-process Script Approval**, click **Approve**, and reload.

### 7.3 Create the deploy job

🟨 **Browser** — Jenkins → **New Item**

Name: `deploy-ai-gateway`. Type: **Pipeline**. Fill in the **Pipeline** section like step 7.1, with
one difference:

| Field | Value |
|---|---|
| Script Path | `jenkins/deploy-ai-gateway.Jenkinsfile` |

No parameters. Leave **Build Triggers** empty. Click **Save**.

---

## Part 8 — The first release through Jenkins

### 8.1 Build

🟨 **Browser** — `build-ai-gateway` → **Build with Parameters** → BRANCH `develop` → **Build**

The first run takes a few minutes longer, because it downloads a Python image.

✅ **Check:** the Console Output ends with
`Pushed <tag> (gateway-release + gateway-wake-front) -- pick tag '<tag>' in deploy-ai-gateway`.
Note the tag, a 7-character code.

### 8.2 Deploy

🟨 **Browser** — `deploy-ai-gateway` → **Build Now**

The build pauses at **Select image tag**. This is a question, not a hang. Open the build → **Paused
for Input**. Pick the tag from 8.1. Click **Proceed**.

It then wakes homelabai, deploys the gateway, replaces the wake front you started in step 2.3, and
tests both.

✅ **Check:** the build finishes with **SUCCESS**. Then confirm on the machine:

🟩 **proxy-vm**

```bash
docker inspect gateway-wake-front --format '{{.Config.Image}}'
curl -s localhost:9100/__wake/status; echo
```

The first line ends with `:<your tag>`. The second shows `"up":true`.

From now on, Jenkins owns the wake front. Do not start it by hand from `~/ai-gateway-src/wake` again.

---

## Part 9 — Final check

### 9.1 A request while homelabai sleeps

🟦 **homelabai**

```bash
sudo systemctl suspend
```

⬜ **Your PC** — wait 20 seconds, then send a chat request through the public address. Use an API
key the gateway accepts.

```bash
curl -N https://ai-gateway.mainul35.dev/v1/chat/completions -H "Authorization: Bearer <your API key>" -H "Content-Type: application/json" -d "{\"model\":\"qwen3.8-27b-tuned\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
```

✅ **Check:** you see `: waking homelabai` lines, then the answer. No 1033, no 502.

### 9.2 Switch off test mode

🟦 **homelabai**

Watch the idle timer for a while. Do not type in this window, or it counts as activity.

```bash
journalctl -t idle-shutdown -f
```

When you see `idle for 5 min` and then `DRY_RUN: would suspend now`, it is working. Press `Ctrl+C`
and switch test mode off:

```bash
sudo sed -i 's/^DRY_RUN=1/DRY_RUN=0/' /etc/default/idle-shutdown
```

✅ **Check:** about 5 minutes after the last activity, homelabai goes to sleep on its own.

**Setup is complete.**

---

## Everyday tasks

| I want to… | Where | Do this |
|---|---|---|
| **Release new code** | 🟨 Jenkins | `build-ai-gateway` → Build with Parameters → `develop`. Then `deploy-ai-gateway` → Build Now → pick the new tag. |
| **Go back to an earlier version** | 🟨 Jenkins | `deploy-ai-gateway` → Build Now → pick an older tag. |
| **Change how long it waits before sleeping** | 🟦 homelabai | `sudo nano /etc/default/idle-shutdown` → change `IDLE_MINUTES=5`. It applies within a minute. No restart needed. |
| **Keep it awake for a while** | 🟦 homelabai | `sudo touch /run/stay-awake`. Remove it with `sudo rm /run/stay-awake`. A reboot also removes it. |
| **Put it to sleep now** | 🟦 homelabai | `curl -X POST localhost:9000/admin/power/suspend -H "Authorization: Bearer <admin key>"` |
| **Wake it now** | 🟩 proxy-vm | `curl -s -X POST localhost:9100/__wake/now` |
| **See if it is awake** | 🟩 proxy-vm | `curl -s localhost:9100/__wake/status` |
| **See why it is or is not sleeping** | 🟦 homelabai | `journalctl -t idle-shutdown -n 20 --no-pager` |
| **See the wake front's log** | 🟩 proxy-vm | `docker logs --tail 30 gateway-wake-front` |
| **Change a wake front setting** | 🟨 Jenkins | Edit your `wake.env`, update the `gateway-wake-env` credential with it, then run `deploy-ai-gateway` with the current tag. |
| **Update the idle timer after a deploy** | 🟦 homelabai | Only when the deploy log says the script differs: `sudo install -m 755 ~/model-gateway/ops/idle-shutdown/idle-shutdown.sh /usr/local/sbin/idle-shutdown` |

**About the idle timeout.** The real wait is a little longer than the number you set: up to one
extra minute until the next check, plus a 30-second warning. Never edit the defaults inside
`idle-shutdown.sh`. Always use `/etc/default/idle-shutdown`, which deploys never overwrite. More
settings: [sleep-and-wake.md → Settings reference](sleep-and-wake.md#settings-reference).

---

## If something goes wrong

Each of these happened during the first setup.

| You see | Where | Why | Fix |
|---|---|---|---|
| Cloudflare error **1033** | Browser | The address still points at homelabai's tunnel. | Do [Part 3](#part-3--cloudflare-send-the-public-address-to-the-wake-front). |
| **"A DNS record with this name already exists"** | Cloudflare | The old CNAME is still there. | Delete it first ([step 3.2](#32-delete-the-old-dns-record)), then save again. |
| `"up":false` while homelabai answers ping | proxy-vm | homelabai's firewall blocks port 9000. | [Step 1.3](#13-let-proxy-vm-reach-the-gateway). |
| `"waking":true` for 5 minutes, homelabai does not wake | proxy-vm | The wake-up packet is not reaching it. | On homelabai, check [step 1.5](#15-make-sure-it-can-sleep-and-wake-properly). Try your Windows wake script: if that works, the problem is on proxy-vm's side. |
| `Permission denied (publickey)` | proxy-vm | The key line in homelabai's `authorized_keys` is missing or is placeholder text. | Redo [step 5.2](#52-let-homelabai-accept-the-key) and check `tail -1`. |
| `cat: .../jenkins-deploy.pub: No such file` | homelabai | The key is on proxy-vm, not homelabai. | Use the command in step 5.2, which fetches it from proxy-vm. |
| Never sleeps, log says `process unattended-upgr running` | homelabai | An old settings file listed a process Ubuntu always runs. | `sudo sed -i 's/ unattended-upgr / /' /etc/default/idle-shutdown` |
| Log says `DRY_RUN: would suspend now`, nothing happens | homelabai | Test mode is still on. | [Step 9.2](#92-switch-off-test-mode). |
| Log says `busy: recent input on pts/0` | homelabai | You typed in an SSH window. | Close it, or wait 5 minutes. |
| Log says `busy: network ... KB/s` all the time | homelabai | Other services move data constantly. | Raise `NET_KBPS_MAX` in `/etc/default/idle-shutdown`. |
| `404 page not found` from `localhost:9000` | proxy-vm | On proxy-vm, port 9000 is Portainer. | Use `http://192.168.50.146:9000`. |
| `port is already allocated` for 5000 | proxy-vm | You ran compose in the main folder. Port 5000 is Harbor. | Run `docker compose down` there, then use the `wake` folder (step 2.3). |
| `git pull` says *not a git repository* in `~/model-gateway` | homelabai | That folder is a deploy target, not a clone. | Work in `~/ai-gateway-src`. |
| Jenkins: `ci/checks.sh: No such file or directory` | Jenkins | The job reads an old branch. | Set the Branch Specifier to `*/develop` (step 7.1). |
| Jenkins: **Check registry credentials** fails | Jenkins | The robot secret is wrong or was refreshed. | Update `harbor-robot-ai-gateway` (step 6.1). |
| Jenkins: **Wake homelabai** times out | Jenkins | homelabai did not wake. | On proxy-vm, run `curl -s localhost:9100/__wake/status` and `docker logs gateway-wake-front`. |
| Sleep API answers `503` about sudo | homelabai | The sudo rule is missing. | [Step 1.4](#14-allow-the-gateway-to-suspend-the-machine). |

How it all works, and every setting: [sleep-and-wake.md](sleep-and-wake.md).
What the Jenkins jobs do in detail: [deployment.md](deployment.md).
