"""Runs a project's commands in a throwaway container, never on this server itself.

A coding task runs code nobody has reviewed yet - the project's build, its tests, and commands the
model chose - so all of it goes into a container that sees only the task's worktree (at /work), has
no network unless the project needs one to fetch dependencies, and is capped in memory, CPU and
processes. It runs as the gateway's own user, so what it writes stays owned by that user and not by
root. The container is removed when the command ends, or when it runs out of time.

Two things keep it from reaching past the container:

- The worktree's .git - a one-line file naming the real git directory, which lives in the project's
  clone outside the container - is mounted read-only over itself. Writable, a command could point it
  at a git directory of its own whose config names a program (core.fsmonitor, a hook), and the next
  `git add` the gateway ran on the host would run that program as the gateway's user - who can use
  Docker, which makes that root. workspace.git also checks the pointer before every command.
- A network, when a project needs one for its dependencies, is never Docker's default bridge, from
  which every service on this machine and the LAN answers. It is the network named by
  coding.sandbox.network (default gateway-sandbox), made by ops/sandbox-network/setup.sh with
  firewall rules that let it reach the internet and nothing private. A task that needs a network
  fails, saying so, until that network exists.
"""
import asyncio
import logging
import os
import uuid

from app import settings

log = logging.getLogger("coding.sandbox")

MAX_OUTPUT = 12_000
NETWORK_LABEL = "gateway.egress"     # set to "filtered" by ops/sandbox-network/setup.sh


def network_name():
    return settings.get("coding.sandbox.network", "CODING_SANDBOX_NETWORK") or "gateway-sandbox"


async def network_ready():
    """None when the filtered network is there; otherwise what is wrong, for the task's log."""
    name = network_name()
    process = await asyncio.create_subprocess_exec(
        "docker", "network", "inspect", "--format", f'{{{{index .Labels "{NETWORK_LABEL}"}}}}', name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await process.communicate()
    if process.returncode != 0:
        return (f"this project needs a network, and the sandbox network {name} does not exist. "
                "An admin sets it up once on the server with ops/sandbox-network/setup.sh, which "
                "blocks it from this machine and the LAN.")
    if out.decode().strip() != "filtered":
        return (f"the sandbox network {name} is not marked as filtered ({NETWORK_LABEL}=filtered), so "
                "it may reach this machine and the LAN; it is not used. See ops/sandbox-network/setup.sh.")
    return None


def _tail(text, limit=MAX_OUTPUT):
    """The end of a long output, where the error usually is, with how much was left out."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters cut from the start ...]\n" + text[-limit:]


async def run(worktree, image, command, network=False, timeout=900, memory="6g", cpus="6"):
    """Runs `command` with sh in `image`, in the worktree. Returns (exit_code, output).

    exit_code is None when it was stopped for running out of time.
    """
    if network:
        problem = await network_ready()
        if problem:
            return 1, problem
    root = os.path.realpath(worktree)
    pointer = os.path.join(root, ".git")
    if os.path.islink(pointer) or not os.path.exists(pointer):
        return 1, "the task's .git is missing or replaced; refusing to run anything in it"
    name = f"gateway-task-{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "--rm", "--name", name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", network_name() if network else "none",
            "--memory", memory, "--cpus", cpus, "--pids-limit", "1024",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
            "-e", "HOME=/tmp", "-e", "CI=true",
            "-v", f"{root}:/work",
            # Over the writable mount: the git pointer cannot be changed, moved or deleted
            "-v", f"{pointer}:/work/.git:ro",
            "-w", "/work",
            image, "sh", "-c", command]
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await (await asyncio.create_subprocess_exec("docker", "rm", "-f", name,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)).wait()
        process.kill()
        return None, f"stopped after {timeout} seconds without finishing"
    return process.returncode, _tail(out.decode(errors="replace"))


async def image_present(image):
    process = await asyncio.create_subprocess_exec("docker", "image", "inspect", image,
                                                   stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.DEVNULL)
    return await process.wait() == 0


async def pull(image, timeout=1200):
    process = await asyncio.create_subprocess_exec("docker", "pull", "--quiet", image,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    if process.returncode != 0:
        raise RuntimeError(f"could not pull {image}: {out.decode(errors='replace')[-300:]}")
