"""The playground's Code mode: the Claude Code sessions on the person's own computers.

Each computer that should show its sessions runs the sessions agent (app/static/claude-sessions.py,
started by `ai-gateway sessions`). It reads the sessions Claude Code keeps for the launcher
(~/.claude-gateway/projects) and reports them here every half minute: their titles, folders, branches
and latest messages. Nothing connects in to the computer: the agent asks here for work (a long poll),
and a message sent to a session from the playground is handed to it that way. It runs
`claude --resume <session> -p` in the session's folder and sends each event back, which the playground
shows as it comes.

Only the person's own sessions, from computers that report with their key. A message makes Claude Code
act on that computer as if it were typed there: file edits are allowed, and commands only where the
agent was started with --allow-commands and the person asks for them.

The jobs live in memory: a restart of the gateway ends the ones in flight, and the agent picks up the
next message as before.
"""
import asyncio
import itertools
import json
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, authenticate
from app.db import get_session
from app.models import CCDSessions, utcnow

router = APIRouter(prefix="/chat/sessions", tags=["code sessions"])

ONLINE_SECONDS = 90          # a computer that reported this recently is there to talk to
POLL_SECONDS = 25            # an agent's long poll: under the 100 s Cloudflare allows a silent request
PICKUP_SECONDS = 40          # a message nobody picked up in this long: the computer is not there
RUN_SECONDS = 1800           # one message's whole run
KEPT_MESSAGES = 60           # of a session's transcript

_waiting: dict[tuple[int, str], asyncio.Queue] = {}      # (user, computer) -> messages for its agent
_jobs: dict[int, dict] = {}                               # job -> its owner, events and state
_agents: dict[tuple[int, str], dict] = {}                 # (user, computer) -> what its agent allows
_ids = itertools.count(1)


def _user(principal):
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Sessions belong to a user; the master key has none")
    return principal.user


def _inbox(user_id, computer):
    return _waiting.setdefault((user_id, computer), asyncio.Queue())


def _online(row):
    return bool(row.seen_at and (utcnow() - row.seen_at).total_seconds() < ONLINE_SECONDS)


def _json(row, transcript=False):
    agent = _agents.get((row.user_id, row.pc_name)) or {}
    data = {"session_id": row.session_id, "title": row.title, "workspace_path": row.workspace_path,
            "pc_name": row.pc_name, "status": row.status, "model": row.model, "branch_name": row.branch_name,
            "last_active": row.last_active.isoformat() if row.last_active else None,
            "online": _online(row), "allow_commands": bool(agent.get("allow_commands"))}
    if transcript:
        try:
            data["transcript"] = json.loads(row.transcript or "[]")
        except ValueError:
            data["transcript"] = []
    return data


# --- what the agent reports and asks for --------------------------------------------------------

class Said(BaseModel):
    role: str = Field(pattern="^(user|assistant|tool)$")
    text: str = Field(max_length=20000)
    at: str | None = Field(default=None, max_length=40)


class Reported(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")
    title: str = Field(default="Untitled", max_length=200)
    workspace_path: str | None = Field(default=None, max_length=500)
    branch_name: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=100)
    status: str = Field(default="idle", pattern="^(idle|running)$")
    last_active: datetime | None = None
    # Its latest messages; left out when they have not changed since the last report
    transcript: list[Said] | None = Field(default=None, max_length=KEPT_MESSAGES)


class Report(BaseModel):
    pc_name: str = Field(min_length=1, max_length=100)
    sessions: list[Reported] = Field(default_factory=list, max_length=200)
    allow_commands: bool = False


@router.post("/register")
async def register(payload: Report, principal: Principal = Depends(authenticate),
                   session: AsyncSession = Depends(get_session)):
    """A computer's sessions as its agent sees them now: added, brought up to date, and the ones it no
    longer reports taken out. A session id another person's computer reported is never touched."""
    user = _user(principal)
    now = utcnow()
    _agents[(user.id, payload.pc_name)] = {"allow_commands": payload.allow_commands, "seen": time.monotonic()}
    reported = {s.session_id: s for s in payload.sessions}
    existing = {row.session_id: row for row in (await session.execute(
        select(CCDSessions).where(CCDSessions.session_id.in_(list(reported))))).scalars()} if reported else {}
    for sid, s in reported.items():
        row = existing.get(sid)
        if row is not None and row.user_id != user.id:
            continue
        if row is None:
            row = CCDSessions(session_id=sid, user_id=user.id, pc_name=payload.pc_name)
            session.add(row)
        row.pc_name, row.title, row.workspace_path = payload.pc_name, s.title or "Untitled", s.workspace_path
        row.branch_name, row.model, row.status = s.branch_name, s.model, s.status
        row.last_active, row.seen_at = s.last_active or now, now
        if s.transcript is not None:
            row.transcript = json.dumps([m.model_dump() for m in s.transcript])
    # What this computer no longer has (deleted, or older than it reports) goes from the list
    gone = delete(CCDSessions).where(CCDSessions.user_id == user.id, CCDSessions.pc_name == payload.pc_name)
    if reported:
        gone = gone.where(CCDSessions.session_id.not_in(list(reported)))
    await session.execute(gone)
    await session.commit()
    return {"registered": len(reported)}


@router.get("/agent/next")
async def next_message(pc: str = Query(min_length=1, max_length=100), principal: Principal = Depends(authenticate)):
    """The agent asks for work: a message for one of its sessions, or nothing after a while (204)."""
    user = _user(principal)
    agent = _agents.setdefault((user.id, pc), {"allow_commands": False})
    agent["seen"] = time.monotonic()
    try:
        job_id = await asyncio.wait_for(_inbox(user.id, pc).get(), timeout=POLL_SECONDS)
    except asyncio.TimeoutError:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job = _jobs.get(job_id)
    if job is None or job["done"]:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    job["picked"].set()
    return {"job_id": job_id, "session_id": job["session_id"], "text": job["text"],
            "workspace_path": job["workspace_path"], "model": job["model"], "permission": job["permission"]}


class Events(BaseModel):
    job_id: int
    events: list[dict] = Field(default_factory=list, max_length=500)
    done: bool = False
    error: str | None = Field(default=None, max_length=2000)


@router.post("/agent/events")
async def job_events(payload: Events, principal: Principal = Depends(authenticate)):
    """What a run has done so far, from the agent running it."""
    user = _user(principal)
    job = _jobs.get(payload.job_id)
    if job is None or job["user_id"] != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such job")
    for event in payload.events:
        await job["events"].put(event)
    if payload.error:
        await job["events"].put({"kind": "error", "text": payload.error})
    if payload.done:
        job["done"] = True
        await job["events"].put(None)
    return {"ok": True}


# --- what the playground asks -------------------------------------------------------------------

@router.get("")
async def list_sessions(principal: Principal = Depends(authenticate), session: AsyncSession = Depends(get_session)):
    """The person's sessions, the latest first, each saying whether its computer is there now."""
    user = _user(principal)
    since = utcnow() - timedelta(days=30)
    rows = (await session.execute(select(CCDSessions).where(
        CCDSessions.user_id == user.id, CCDSessions.last_active >= since)
        .order_by(CCDSessions.last_active.desc()).limit(200))).scalars()
    return [_json(row) for row in rows]


async def _session_row(session, user, session_id):
    row = (await session.execute(select(CCDSessions).where(
        CCDSessions.user_id == user.id, CCDSessions.session_id == session_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")
    return row


@router.get("/{session_id}")
async def session_detail(session_id: str, principal: Principal = Depends(authenticate),
                         session: AsyncSession = Depends(get_session)):
    """One session and its latest messages, as its computer last reported them."""
    return _json(await _session_row(session, _user(principal), session_id), transcript=True)


class SessionMessage(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    # "edits": Claude Code may edit files but not run commands; "commands": both, where the computer's
    # agent was started with --allow-commands
    permission: str = Field(default="edits", pattern="^(edits|commands)$")


@router.post("/{session_id}/message")
async def send_message(session_id: str, payload: SessionMessage, principal: Principal = Depends(authenticate),
                       session: AsyncSession = Depends(get_session)):
    """A message for a session, run on its computer; its events streamed back as they come."""
    user = _user(principal)
    row = await _session_row(session, user, session_id)
    if not _online(row):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{row.pc_name} is not reporting its sessions now: start `ai-gateway sessions` there")
    agent = _agents.get((user.id, row.pc_name)) or {}
    if payload.permission == "commands" and not agent.get("allow_commands"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{row.pc_name} does not let commands be run from here: "
                                                       "its agent was started without --allow-commands")
    if any(j["session_id"] == session_id and not j["done"] for j in _jobs.values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "This session is already answering a message")
    job_id = next(_ids)
    job = {"user_id": user.id, "session_id": session_id, "text": payload.text, "workspace_path": row.workspace_path,
           "model": row.model, "permission": payload.permission, "events": asyncio.Queue(), "done": False,
           "picked": asyncio.Event(), "started": time.monotonic()}
    _jobs[job_id] = job
    await _inbox(user.id, row.pc_name).put(job_id)
    computer = row.pc_name

    async def stream():
        def line(event):
            return f"data: {json.dumps({'session': event})}\n\n".encode()
        try:
            yield line({"kind": "status", "text": f"Waiting for {computer} to pick it up"})
            try:
                await asyncio.wait_for(job["picked"].wait(), timeout=PICKUP_SECONDS)
            except asyncio.TimeoutError:
                job["done"] = True
                yield line({"kind": "error", "text": f"{computer} did not pick the message up: is its agent running?"})
                return
            yield line({"kind": "status", "text": f"Running on {computer}"})
            while True:
                left = RUN_SECONDS - (time.monotonic() - job["started"])
                if left <= 0:
                    yield line({"kind": "error", "text": "It ran for longer than half an hour; stopped waiting"})
                    return
                try:
                    event = await asyncio.wait_for(job["events"].get(), timeout=min(15, left))
                except asyncio.TimeoutError:
                    yield b": still running\n\n"           # keeps Cloudflare from closing a quiet stream
                    continue
                if event is None:
                    yield line({"kind": "done"})
                    return
                yield line(event)
        finally:
            job["done"] = True
            _jobs.pop(job_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
