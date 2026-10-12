"""Putting the machine to sleep on request (POST /admin/power/suspend).

The idle timer (ops/idle-shutdown) does this by itself after half an hour; this is for "I'm done,
sleep now". Waking is the wake front's job: the next real request through the tunnel wakes it.

The gateway runs as an ordinary user, so suspending needs one sudo rule on the machine, limited to
exactly this command:

    echo 'mainul35 ALL=(root) NOPASSWD: /usr/bin/systemctl suspend' | sudo tee /etc/sudoers.d/gateway-suspend
    sudo chmod 440 /etc/sudoers.d/gateway-suspend

`power.suspend.command` replaces the command (e.g. "sudo -n /usr/bin/systemctl poweroff", with a
matching sudo rule).
"""
import asyncio
import logging
import shlex

from app import settings

log = logging.getLogger("power")

DEFAULT_COMMAND = "sudo -n /usr/bin/systemctl suspend"

_pending: asyncio.Task | None = None


def command():
    return shlex.split(settings.get("power.suspend.command", "GATEWAY_SUSPEND_COMMAND") or DEFAULT_COMMAND)


async def _run(args, timeout=10):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        return 1, "timed out"
    return process.returncode, (err or out).decode(errors="replace").strip()


async def allowed():
    """(ok, problem): whether the command can run without a password, checked before promising."""
    args = command()
    if args[:2] == ["sudo", "-n"]:
        code, text = await _run(["sudo", "-n", "-l", *args[2:]])
        if code != 0:
            return False, ("the gateway user may not run this without a password; add the sudo rule "
                           "described in app/power.py")
    return True, None


def pending():
    return _pending is not None and not _pending.done()


def schedule(delay_seconds):
    """Suspends after `delay_seconds`, so the HTTP answer reaches the caller first."""
    global _pending

    async def later():
        await asyncio.sleep(delay_seconds)
        log.info("suspending the machine: %s", " ".join(command()))
        code, text = await _run(command())
        if code != 0:
            log.error("suspend failed (%s): %s", code, text)

    _pending = asyncio.create_task(later())


def cancel():
    if pending():
        _pending.cancel()
        return True
    return False
