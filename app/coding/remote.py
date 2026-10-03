"""A relay to a folder on somebody's own computer: the gateway thinks, their machine does.

A local project is a folder this server cannot see. Its owner's browser (with the folder opened on
the Tasks page) or the helper program (tools/gateway-helper.py, running on their computer) connects
here and asks for work; the task runner hands it one small operation at a time - list, read, write,
search, run - and waits for the answer.

The operations are deliberately simple. Everything that needs judgement stays on this server: an
edit is worked out here with the same engine the server's own projects use, from the file's text,
and only the finished text is sent back to be written. That keeps the two clients small and keeps
them from disagreeing with each other about what an edit means.

Only the person who added a project can serve it: their computer, their folder.
"""
import asyncio
import itertools
import time

CONNECTED_FOR = 45          # seconds since the last poll that still counts as connected
CALL_SECONDS = 120          # an operation that takes longer than this has failed
RUN_SECONDS = 1800          # a command can take longer: a build, a test suite, a person deciding

_queues: dict[int, asyncio.Queue] = {}
_waiting: dict[int, tuple[int, asyncio.Future]] = {}
_seen: dict[int, dict] = {}
_ids = itertools.count(1)


class NotConnected(Exception):
    pass


class ClientError(Exception):
    """The client could not do it; the message is its reason."""


def _queue(project_id):
    return _queues.setdefault(project_id, asyncio.Queue())


def seen(project_id, client, can_run):
    _seen[project_id] = {"at": time.monotonic(), "client": client, "can_run": bool(can_run)}


def connection(project_id):
    """{client, can_run} of the client serving the project now, or None."""
    entry = _seen.get(project_id)
    if entry and time.monotonic() - entry["at"] < CONNECTED_FOR:
        return entry
    return None


async def call(project_id, op, **args):
    """Asks the project's client to do one operation and returns its result."""
    if connection(project_id) is None:
        raise NotConnected("the folder is not connected: open the Tasks page with the folder connected, "
                           "or start the helper on that computer")
    request_id = next(_ids)
    future = asyncio.get_running_loop().create_future()
    _waiting[request_id] = (project_id, future)
    await _queue(project_id).put({"id": request_id, "op": op, "args": args})
    try:
        return await asyncio.wait_for(future, RUN_SECONDS if op == "run" else CALL_SECONDS)
    except asyncio.TimeoutError:
        raise ClientError(f"the computer did not answer a {op} within the time allowed")
    finally:
        _waiting.pop(request_id, None)


async def next_request(project_id, client, can_run, wait=25):
    """Long-poll from a client: the next operation for it, or None after `wait` seconds."""
    seen(project_id, client, can_run)
    try:
        return await asyncio.wait_for(_queue(project_id).get(), wait)
    except asyncio.TimeoutError:
        return None
    finally:
        seen(project_id, client, can_run)


def answer(project_id, request_id, ok, result=None, error=None):
    """A client's answer. Ignored when nothing is waiting for it (it took too long, or is not theirs)."""
    waiting = _waiting.get(request_id)
    if not waiting or waiting[0] != project_id or waiting[1].done():
        return False
    if ok:
        waiting[1].set_result(result)
    else:
        waiting[1].set_exception(ClientError(str(error or "the computer could not do it")[:2000]))
    return True
