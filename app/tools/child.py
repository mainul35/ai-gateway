"""A program the gateway starts for a job - ffmpeg, the narration worker - that cannot outlive it.

Restarted by a deploy, the gateway left a video's ffmpeg running on its own: no one would ever read
what it made, and it went on filling the memory (40 GB and growing, 2026-10-25). And a job that was
stopped - Stop, or carried on later - left its program running to the end. Started here, the
program is killed when the gateway's process goes, and when the work waiting for it is cancelled.
"""
import asyncio
import ctypes
import signal

PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """Runs in the child before the program starts: the kernel sends it SIGKILL when its parent dies."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except (OSError, AttributeError):    # not Linux: nothing to ask for
        pass


async def run(*args, stdin=None, stdout=asyncio.subprocess.DEVNULL):
    """(stdout bytes, stderr bytes, return code) of `args`, with `stdin` written to it."""
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE if stdin is not None else None, stdout=stdout,
        stderr=asyncio.subprocess.PIPE, preexec_fn=_die_with_parent)
    try:
        out, err = await process.communicate(stdin)
    except asyncio.CancelledError:
        process.kill()
        raise
    return out, err, process.returncode
