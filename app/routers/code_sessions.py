"""The playground's Code mode: the coding sessions on the person's own computers.

Every coding tool the launcher connects - Claude Code, OpenCode, Qwen Code, Codex, Hermes, OpenClaw -
sends its model requests here, and each request carries its conversation so far. So the sessions are
known from that traffic (observe), nothing to install: which tool (the launcher's X-AI-Gateway-Client
header, else the tool's own User-Agent), which session (the tool's own id - Claude Code's
X-Claude-Code-Session-Id, OpenCode's x-session-id, Codex's session_id - else the conversation's first
question), and its folder, title, model and latest messages. Requests that come from no coding tool -
the playground, an API script - are not sessions.

Talking to a session from the playground needs its computer's sessions agent (app/static/claude-sessions.py,
started by `ai-gateway sessions`), and for now Claude Code: the agent also reports the sessions Claude
Code keeps for the launcher (~/.claude-gateway/projects). Nothing connects in to the computer: the agent
asks here for work (a long poll), and a message sent to a session from the playground is handed to it
that way. It runs `claude --resume <session> -p` in the session's folder and sends each event back,
which the playground shows as it comes.

Only the person's own sessions, from computers that report with their key. A message makes Claude Code
act on that computer as if it were typed there: file edits are allowed, and commands only where the
agent was started with --allow-commands and the person asks for them.

The jobs live in memory: a restart of the gateway ends the ones in flight, and the agent picks up the
next message as before.
"""
import asyncio
import hashlib
import itertools
import json
import logging
import re
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal, authenticate
from app.db import get_session, session_factory
from app.models import CCDSessions, utcnow

log = logging.getLogger("gateway")

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


# The coding tools the launcher connects, by what their requests say they are
TOOLS = {"claude-code": "Claude Code", "claude-desktop": "Claude desktop", "opencode": "OpenCode", "qwen-code": "Qwen Code", "codex": "Codex",
         "hermes": "Hermes", "openclaw": "OpenClaw"}
# The desktop app's Code tab is Claude Code too, and says so in its User-Agent: looked for first
_AGENTS_BY_NAME = (("claude-desktop", "claude-desktop"), ("claude-cli", "claude-code"), ("opencode", "opencode"), ("qwencode", "qwen-code"),
                   ("qwen-code", "qwen-code"), ("codex", "codex"), ("hermes", "hermes"), ("openclaw", "openclaw"))
# Sessions a computer's agent can send a message to: Claude Code's, from a terminal or the desktop app
SENDABLE = ("claude-code", "claude-desktop")
RUNNING_SECONDS = 90         # a session that asked for a reply this recently is working


def _computer(name):
    """A computer's name as both the launcher's header and its agent give it: case aside."""
    return (name or "").strip().lower()


def _inbox(user_id, computer):
    return _waiting.setdefault((user_id, _computer(computer)), asyncio.Queue())


def _agent(user_id, computer):
    """The computer's sessions agent, when it has asked for work or reported lately; else None."""
    agent = _agents.get((user_id, _computer(computer)))
    return agent if agent and time.monotonic() - agent.get("seen", 0) < ONLINE_SECONDS else None


def _json(row, transcript=False):
    agent = _agent(row.user_id, row.pc_name)
    client = row.client or "claude-code"
    data = {"session_id": row.session_id, "title": row.title, "workspace_path": row.workspace_path,
            "pc_name": row.pc_name, "model": row.model, "branch_name": row.branch_name,
            "client": client, "tool": TOOLS.get(client, client),
            "status": "running" if row.last_active and (utcnow() - row.last_active).total_seconds() < RUNNING_SECONDS
            else "idle",
            "last_active": row.last_active.isoformat() if row.last_active else None,
            # Its computer's agent is there: what makes a session one to send messages to (Claude Code's)
            "sendable": client in SENDABLE, "online": agent is not None,
            "can_send": agent is not None and client in SENDABLE,
            "allow_commands": bool(agent and agent.get("allow_commands"))}
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
    title: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=500)
    branch_name: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=100)
    status: str = Field(default="idle", pattern="^(idle|running)$")
    last_active: datetime | None = None
    # Its latest messages; left out when they have not changed since the last report
    transcript: list[Said] | None = Field(default=None, max_length=KEPT_MESSAGES)
    # "claude-desktop": a session of the desktop app's Code tab, only its id and folder - the gateway
    # knows the rest from its requests, and lists it only once it has seen them
    app: str | None = Field(default=None, pattern="^(claude-code|claude-desktop)$")


class Report(BaseModel):
    pc_name: str = Field(min_length=1, max_length=100)
    sessions: list[Reported] = Field(default_factory=list, max_length=200)
    allow_commands: bool = False


@router.post("/register")
async def register(payload: Report, principal: Principal = Depends(authenticate),
                   session: AsyncSession = Depends(get_session)):
    """A computer's sessions as its agent sees them now: added or brought up to date. A session id
    another person has is never touched."""
    user = _user(principal)
    now = utcnow()
    _agents[(user.id, _computer(payload.pc_name))] = {"allow_commands": payload.allow_commands, "seen": time.monotonic()}
    reported = {s.session_id: s for s in payload.sessions}
    existing = {row.session_id: row for row in (await session.execute(
        select(CCDSessions).where(CCDSessions.session_id.in_(list(reported))))).scalars()} if reported else {}
    for sid, s in reported.items():
        row = existing.get(sid)
        if row is not None and row.user_id != user.id:
            continue
        if s.app == "claude-desktop":
            # The desktop app keeps sessions that never came here (signed in to Claude itself): only the
            # ones the gateway has seen are this computer's to send to
            if row is not None:
                row.pc_name, row.client, row.seen_at = payload.pc_name, "claude-desktop", now
                row.workspace_path = s.workspace_path or row.workspace_path
            continue
        if row is None:
            row = CCDSessions(session_id=sid, user_id=user.id, pc_name=payload.pc_name)
            session.add(row)
        # What the agent leaves out stays as the gateway knows it from the session's own requests
        row.pc_name, row.title = payload.pc_name, s.title or row.title or "Untitled"
        row.workspace_path, row.branch_name = s.workspace_path or row.workspace_path, s.branch_name or row.branch_name
        row.model, row.status = s.model or row.model, s.status
        row.client = row.client if row.client in SENDABLE else "claude-code"
        row.last_active = max(filter(None, [row.last_active, s.last_active])) if row.last_active else (s.last_active or now)
        row.seen_at = now
        if s.transcript is not None:
            row.transcript = json.dumps([m.model_dump() for m in s.transcript])
    await session.commit()
    return {"registered": len(reported)}


@router.get("/agent/next")
async def next_message(pc: str = Query(min_length=1, max_length=100), principal: Principal = Depends(authenticate)):
    """The agent asks for work: a message for one of its sessions, or nothing after a while (204)."""
    user = _user(principal)
    agent = _agents.setdefault((user.id, _computer(pc)), {"allow_commands": False})
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
    if (row.client or "claude-code") not in SENDABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Messages can be sent to Claude Code sessions only, for now; "
                                                      f"this one is {TOOLS.get(row.client, row.client)}'s")
    agent = _agent(user.id, row.pc_name)
    if agent is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{row.pc_name or 'Its computer'} is not connected: run `ai-gateway sessions` there")
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


# --- the sessions, from the coding tools' own requests ---------------------------------------------

def _tool(headers):
    named = (headers.get("x-ai-gateway-client") or "").strip().lower()
    if named in TOOLS:
        return named
    said = " ".join((headers.get(h) or "").lower() for h in ("user-agent", "originator"))
    return next((tool for mark, tool in _AGENTS_BY_NAME if mark in said), None)


def _words(content):
    """A message's words, from any of the three APIs' shapes; a tool call as one short line."""
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text") and part.get("text"):
            out.append(part["text"])
        elif kind == "tool_use":
            target = part.get("input") or {}
            hint = target.get("file_path") or target.get("command") or target.get("pattern") or ""
            out.append(f"[{part.get('name')}{': ' + str(hint)[:120] if hint else ''}]")
    return "\n".join(out)


def _conversation(body, api):
    """[(role, text)] in order, and the system prompt's text."""
    said, system = [], ""
    if api == "responses":
        system = body.get("instructions") or ""
        items = body.get("input")
        items = [{"role": "user", "content": items}] if isinstance(items, str) else items or []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                said.append(("tool", f"[{item.get('name')}: {str(item.get('arguments') or '')[:120]}]"))
            elif item.get("role") in ("user", "assistant", "system", "developer"):
                role = "system" if item["role"] in ("system", "developer") else item["role"]
                said.append((role, _words(item.get("content"))))
    else:
        system = body.get("system") or ""
        if isinstance(system, list):
            system = _words(system)
        for m in body.get("messages") or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "system":
                system += "\n" + _words(m.get("content"))
                continue
            if role == "tool":
                continue                                    # a tool's output: too much to keep
            if role == "user" and isinstance(m.get("content"), list) and all(
                    isinstance(p, dict) and p.get("type") == "tool_result" for p in m["content"]):
                continue
            text = _words(m.get("content"))
            for call in m.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                text += f"\n[{function.get('name')}: {str(function.get('arguments') or '')[:120]}]"
            if role in ("user", "assistant") and text.strip():
                said.append((role, text.strip()))
    return said, system


_INJECTED = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|task-notification)[^>]*>.*?</\1>", re.S)
_FOLDER = [re.compile(p, re.I) for p in (r"Primary working directory:\s*(\S[^\n]*)", r"<cwd>\s*([^<\n]+)</cwd>",
                                          r"Working directory:\s*(\S[^\n]*)", r"working in the directory:?\s*(\S[^\n]*)",
                                          r"\bcwd:\s*(\S[^\n]*)")]


def _question(text):
    """What a person typed, without what a tool puts into the same message (reminders, set-up)."""
    text = _INJECTED.sub("", text or "").strip()
    if not text or text.startswith("<") or "setting up the context" in text.lower():
        return ""
    return text


def _folder(*texts):
    for text in texts:
        for pattern in _FOLDER:
            found = pattern.search(text or "")
            if found:
                return found.group(1).strip().strip("`'\".,")[:500]
    return None


_recent: dict[tuple[int, str], tuple[float, int]] = {}      # (user, session) -> when last written, messages


def observe(headers, principal, body, api):
    """Notes a coding tool's model request as its session's latest step: off the request's path, never
    failing it. headers: the request's (case-insensitive) headers; api: "messages", "chat" or "responses".
    For the tool's main request, what answered() needs to add the reply to the session; else None."""
    try:
        user = principal.user
        tool = _tool(headers) if user is not None and isinstance(body, dict) else None
        if tool is None:
            return None
        said, system = _conversation(body, api)
        questions = [_question(text) for role, text in said if role == "user"]
        first = next((q for q in questions if q), "")
        session_id = None
        for name in ("x-claude-code-session-id", "x-session-id", "session_id", "session-id", "conversation_id"):
            value = (headers.get(name) or "").strip()
            if re.fullmatch(r"[A-Za-z0-9_.:-]{6,64}", value):
                session_id = value
                break
        folder = (headers.get("x-ai-gateway-workspace") or "").strip()[:500] or _folder(
            system, *[text for role, text in said if role == "user"][:3])
        if session_id is None:
            if not first:
                return None                             # nothing to know the conversation by yet
            digest = hashlib.sha1(f"{user.id}|{tool}|{folder}|{first[:4000]}".encode()).hexdigest()[:24]
            session_id = f"{tool}-{digest}"
        computer = (headers.get("x-ai-gateway-computer") or "").strip()[:100]
        # The tool's own work - choosing a title, summing up - goes with the session too, but only its
        # main loop (the request that offers tools) says what the conversation is
        main = bool(body.get("tools"))
        key = (user.id, session_id)
        reply = (user.id, session_id, api) if main else None
        last, count = _recent.get(key, (0.0, -1))
        if time.monotonic() - last < 5 and (not main or count == len(said)):
            return reply
        _recent[key] = (time.monotonic(), len(said) if main else count)
        update = {"client": tool, "computer": computer, "model": body.get("model")}
        if main:
            # What the person asked and the answers to it: a set-up message and the reply to it are left out
            kept, asked = [], False
            for role, text in said:
                if role == "user":
                    asked = bool(_question(text))
                    if asked:
                        kept.append({"role": "user", "text": _question(text)[:4000]})
                elif role == "assistant" and asked:
                    kept.append({"role": "assistant", "text": text[:4000]})
            kept = kept[-KEPT_MESSAGES:]
            update.update(title=(first.splitlines()[0][:200] if first else None), folder=folder, transcript=kept)
        _later(_note(user.id, session_id, update))
        return reply
    except Exception:                                   # never a reason for the request to fail
        log.exception("could not note a coding session")
        return None


_tasks: set = set()                                     # kept until done: the loop holds tasks weakly


def _later(work):
    task = asyncio.get_running_loop().create_task(work)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def answered(response, reply):
    """The response to a coding tool's main request, unchanged; its words are added to the session
    when it has been sent. reply: what observe() gave, or None (then nothing is done)."""
    if reply is None:
        return response
    try:
        if isinstance(response, StreamingResponse):
            original = response.body_iterator

            async def teed():
                reader = _ReplyReader()
                try:
                    async for chunk in original:
                        reader.feed(chunk)
                        yield chunk
                finally:
                    if reader.status_ok:
                        _later(_add_reply(reply, reader.text()))
            response.body_iterator = teed()
        elif 200 <= response.status_code < 300 and getattr(response, "body", None):
            _later(_add_reply(reply, _reply_of(json.loads(response.body))))
    except Exception:
        log.exception("could not note a coding session's reply")
    return response


def _reply_of(answer):
    """The words of a whole answer, in any of the three APIs' shapes."""
    if not isinstance(answer, dict):
        return ""
    if isinstance(answer.get("content"), list):                                  # Messages
        return _words(answer["content"])
    if answer.get("choices"):                                                    # chat completions
        message = (answer["choices"][0] or {}).get("message") or {}
        text = message.get("content") or ""
        for call in message.get("tool_calls") or []:
            text += f"\n[{((call or {}).get('function') or {}).get('name')}]"
        return text
    out = []                                                                     # Responses
    for item in answer.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            out.append(_words(item.get("content")))
        elif item.get("type") == "function_call":
            out.append(f"[{item.get('name')}]")
    return "\n".join(x for x in out if x)


class _ReplyReader:
    """The words of a streamed answer, from its events as they pass: text, and each tool it calls."""

    def __init__(self):
        self.parts, self.buffer, self.status_ok = [], "", True

    def feed(self, chunk):
        if len(self.buffer) > 1_000_000:                    # not an event stream after all
            return
        self.buffer += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            if line.startswith("data:"):
                self._event(line[5:].strip())

    def _event(self, payload):
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "error" or "error" in event and not event.get("choices"):
            self.status_ok = False
        elif kind == "content_block_delta" and (event.get("delta") or {}).get("type") == "text_delta":
            self.parts.append(event["delta"].get("text") or "")
        elif kind == "content_block_start" and (event.get("content_block") or {}).get("type") == "tool_use":
            self.parts.append(f"\n[{event['content_block'].get('name')}]\n")
        elif kind == "response.output_text.delta":
            self.parts.append(event.get("delta") or "")
        elif kind == "response.output_item.added" and (event.get("item") or {}).get("type") == "function_call":
            self.parts.append(f"\n[{event['item'].get('name')}]\n")
        elif event.get("choices"):
            delta = (event["choices"][0] or {}).get("delta") or {}
            self.parts.append(delta.get("content") or "")
            for call in delta.get("tool_calls") or []:
                name = ((call or {}).get("function") or {}).get("name")
                if name:
                    self.parts.append(f"\n[{name}]\n")

    def text(self):
        return re.sub(r"\n{2,}", "\n", "".join(self.parts)).strip()


async def _add_reply(reply, text):
    """The model's answer as the session's newest message, until the tool's next request says it all."""
    user_id, session_id, _api = reply
    if not text:
        return
    try:
        async with session_factory()() as db:
            row = (await db.execute(select(CCDSessions).where(CCDSessions.session_id == session_id))).scalar_one_or_none()
            if row is None or row.user_id != user_id:
                return
            said = json.loads(row.transcript or "[]")
            entry = {"role": "assistant", "text": text[:4000]}
            if not said or said[-1] != entry:
                said.append(entry)
            row.transcript = json.dumps(said[-KEPT_MESSAGES:])
            row.last_active = utcnow()
            await db.commit()
    except Exception:
        log.exception("could not note a coding session's reply")


async def _note(user_id, session_id, update):
    try:
        async with session_factory()() as db:
            row = (await db.execute(select(CCDSessions).where(CCDSessions.session_id == session_id))).scalar_one_or_none()
            if row is not None and row.user_id != user_id:
                return
            if row is None:
                row = CCDSessions(session_id=session_id, user_id=user_id, pc_name=update["computer"] or "",
                                  title="Untitled")
                db.add(row)
            # A desktop app's session stays one when a message sent from the playground runs it in Claude Code
            if not (row.client == "claude-desktop" and update["client"] == "claude-code"):
                row.client = update["client"]
            if update["computer"]:
                row.pc_name = update["computer"]
            if update.get("model"):
                row.model = str(update["model"])[:100]
            if update.get("title") and (row.title in (None, "", "Untitled") or row.client != "claude-code"
                                        or not row.seen_at):
                row.title = update["title"]
            if update.get("folder"):
                row.workspace_path = update["folder"]
            if update.get("transcript"):
                row.transcript = json.dumps(update["transcript"])
            row.last_active = utcnow()
            await db.commit()
    except Exception:
        log.exception("could not note a coding session")
