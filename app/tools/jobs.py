"""Work that outlives the browser tab that asked for it.

A playground job - an answer being written, a picture being drawn, a photograph being cleaned up -
used to be the HTTP response itself: the work was the stream, so closing the tab or walking over to
the dashboard cancelled it. A minute of a model's time would disappear because someone looked away.

Here the work is a task of its own that keeps every event it has produced, and the response is only
a reader of that buffer. A reader that goes away changes nothing, and a reader that arrives late -
the same person coming back to the conversation - is handed everything from the beginning and then
follows along live.

Jobs live in memory and are forgotten when the gateway restarts. That is on purpose: what matters is
written to the database as the job finishes, and a half-finished answer is not worth keeping.
"""
import asyncio
import contextvars
import itertools
import logging
import time

log = logging.getLogger("jobs")

# Long enough to walk to another page and come back, short enough that nothing piles up
KEEP_FINISHED_SECONDS = 300

_jobs = {}
_numbers = itertools.count(1)


class Job:
    def __init__(self, user_id, conversation_id, kind, label):
        self.id = next(_numbers)
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.kind = kind            # "chat", "image" or "photo"
        self.label = label          # what was asked, to name it while it runs
        self.events = []
        self.done = False
        self.started_at = time.time()
        self.finished_at = None
        self.task = None
        self._more = asyncio.Event()

    def append(self, event):
        self.events.append(event)
        self._more.set()

    def finish(self):
        self.done = True
        self.finished_at = time.monotonic()
        self._more.set()

    def json(self):
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "conversation_id": self.conversation_id, "started_at": self.started_at,
                "seconds": round(time.time() - self.started_at, 1)}


def _forget_old():
    for job_id, job in list(_jobs.items()):
        if job.done and job.finished_at and time.monotonic() - job.finished_at > KEEP_FINISHED_SECONDS:
            del _jobs[job_id]


def start(user_id, conversation_id, kind, label, source, on_finish=None):
    """Runs `source` - an async generator of SSE events - as a task, and returns the job holding it."""
    _forget_old()
    job = Job(user_id, conversation_id, kind, label)
    _jobs[job.id] = job

    async def run():
        ending = "finished"
        try:
            async for event in source:
                job.append(event)
        except asyncio.CancelledError:
            # Worth its own line: a cancelled job is the failure this whole module exists to prevent,
            # and it leaves no other trace
            ending = "cancelled"
            raise
        except Exception:
            ending = "failed"
            # The reader may already be gone, so this is the only place it will be noticed
            log.exception("job %s (%s) failed", job.id, kind)
        finally:
            log.info("job %s (%s) %s after %d events", job.id, kind, ending, len(job.events))
            job.finish()
            if on_finish is not None:
                try:
                    await on_finish(job)
                except Exception:
                    log.exception("job %s finished but could not be saved", job.id)

    # In a context of its own, which is the whole point. A task created the ordinary way inherits the
    # request's context, and that context carries the cancel scope the web server closes when the
    # browser goes away - so the "detached" job would be cancelled by the very disconnection it exists
    # to survive. An empty context belongs to no request and is cancelled by nobody.
    job.task = asyncio.get_running_loop().create_task(run(), context=contextvars.Context())
    return job


async def follow(job, start_at=0):
    """Every event from `start_at`, then each new one until the job ends."""
    index = start_at
    while True:
        while index < len(job.events):
            yield job.events[index]
            index += 1
        if job.done:
            return
        job._more.clear()
        # Checked again after clearing: an event appended in between would otherwise wait for the next
        if index < len(job.events) or job.done:
            continue
        await job._more.wait()


def running_for(user_id, conversation_id):
    """The job still working on this conversation, if there is one."""
    _forget_old()
    for job in sorted(_jobs.values(), key=lambda j: j.id, reverse=True):
        if not job.done and job.user_id == user_id and job.conversation_id == conversation_id:
            return job
    return None


def get(user_id, job_id):
    job = _jobs.get(job_id)
    return job if job is not None and job.user_id == user_id else None
