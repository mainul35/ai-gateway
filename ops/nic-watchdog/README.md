# nic-watchdog

Brings homelabai's network card back when it dies, so the machine never sits awake and unreachable.

**Board: ASRock X570 Phantom Gaming 4** (branch `hardware/asrock-x570-phantom-gaming-4`). The values here
are that board's onboard Intel I211: MAC `a8:a1:59:9d:f4:ae`, PCI `0000:04:00.0`. They are in
`nic-watchdog.conf`, `01-lan.yaml` and the wake front's `WAKE_MAC`. For the ASUS ROG Crosshair VIII
Hero, use branch `hardware/asus-rog-crosshair-viii-hero`. On another board this setup cannot find its
card: it takes the card for dead and reboots the machine every few minutes, so it never gets to sleep.

**Why.** The onboard Intel I211 (driver `igb`) sometimes drops off the PCIe bus when homelabai wakes
from suspend: `igb 0000:04:00.0 enp4s0: PCIe link lost`. Nothing brought it back. On 2026-11-11 it
woke at 03:23, lost the card, and stayed offline for 7 hours (still suspending and waking) until it
was restarted by hand. A restart brings the card back.

**What it does**

| When | What |
|---|---|
| After every wake (`/usr/lib/systemd/system-sleep/nic-watchdog`) | Waits up to 40 s for a link; if none, resets the card on the PCI bus; if still none, reboots |
| Every minute (`nic-watchdog.timer`) | No link for 2 checks: resets the card. For 5: reboots |
| At boot (`nic-wol.service`) | Turns Wake-on-LAN on, on whatever the card is called |

It only acts when the card itself has no link, so a router or internet outage never reboots the
machine, and it reboots at most 3 times in 6 hours. The card is found by its hardware address, not
its name: after the first time it dropped off the bus it came back as `enp4s0` instead of `enp3s0`,
and the network config and the Wake-on-LAN service, which both named `enp3s0`, quietly stopped
applying. `01-lan.yaml` replaces that config, matched by address.

**Install** (once, on homelabai; safe to run again):

```bash
sudo bash ~/model-gateway/ops/nic-watchdog/install.sh
```

**Check:** `nic-watchdog status` · logs: `journalctl -t nic-watchdog` · settings: `/etc/default/nic-watchdog`
(`DRY_RUN=1` only logs what it would do).

**What it cannot fix:** a card that only a power cut brings back (2026-11-01). For that, when away
from home, a smart plug on homelabai's power with the BIOS set to power on when power returns
(*Restore on AC power loss: Power On*) lets it be power-cycled from a phone.
