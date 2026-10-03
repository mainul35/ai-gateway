"""Runs a project's commands in a throwaway container, never on this server itself.

A coding task runs code nobody has reviewed yet - the project's build, its tests, and commands the
model chose - so all of it goes into a container that sees only the task's worktree (at /work), has
no network unless the project needs one to fetch dependencies, and is capped in memory, CPU and
processes. It runs as the gateway's own user, so what it writes stays owned by that user and not by
root. The container is removed when the command ends, or when it runs out of time.
"""
import asyncio
import logging
import os
import uuid

log = logging.getLogger("coding.sandbox")

MAX_OUTPUT = 12_000


def _tail(text, limit=MAX_OUTPUT):
    """The end of a long output, where the error usually is, with how much was left out."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters cut from the start ...]\n" + text[-limit:]


async def run(worktree, image, command, network=False, timeout=900, memory="6g", cpus="6"):
    """Runs `command` with sh in `image`, in the worktree. Returns (exit_code, output).

    exit_code is None when it was stopped for running out of time.
    """
    name = f"gateway-task-{uuid.uuid4().hex[:12]}"
    args = ["docker", "run", "--rm", "--name", name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", "bridge" if network else "none",
            "--memory", memory, "--cpus", cpus, "--pids-limit", "1024",
            "--security-opt", "no-new-privileges",
            "-e", "HOME=/tmp", "-e", "CI=true",
            "-v", f"{os.path.realpath(worktree)}:/work", "-w", "/work",
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
