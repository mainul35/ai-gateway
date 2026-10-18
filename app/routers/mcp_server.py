"""The gateway as an MCP server, so a person's coding agents can work across all of their projects.

Claude Code, OpenCode and Qwen Code connect here (the launcher sets it up: bin/ai-gateway.sh) with
the person's own gateway key, and the header X-Workspace names the project the session is in. They
get the person's indexed repositories (app/knowledge.py, pgvector; connected on the dashboard) to
list, search and read, and project notes: what earlier sessions learned about how the projects fit
together, shared by every session of that person in every workspace, and kept true by the agents.

Pull, not push: nothing is added to the agent's prompt beyond a short note of what is there. It asks
when it needs another project, so prompts stay small and the prompt cache that makes long sessions
fast keeps working.

MCP's Streamable HTTP transport, the stateless subset: one JSON-RPC message (or a batch) per POST, a
JSON answer, no server-sent stream. Every person sees their own projects and notes only; admins and
managers also see the gateway's configured sources.
"""
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_, select

from app import knowledge
from app.auth import Principal, authenticate
from app.db import session_factory
from app.models import ProjectNote, utcnow

log = logging.getLogger("mcp_server")

router = APIRouter(tags=["mcp"])

PROTOCOL = "2025-06-18"
MAX_NOTES = 300              # per person
NOTE_CHARS = 600


class ToolError(Exception):
    """Said back to the agent as a failed tool call, which it can read and act on."""


TOOLS = [
    {"name": "list_projects",
     "description": ("Your indexed projects: the GitHub repositories connected on the gateway's dashboard, "
                     "with what each is and when it was last indexed. Start here to see what else you can "
                     "read besides the workspace you are in."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_projects",
     "description": ("Searches the code and documents of your other projects, by meaning and by exact words: "
                     "how a project does something, where a function, endpoint or setting is defined, what "
                     "another project expects from this one. Returns passages with project, file and lines. "
                     "Indexed from what is pushed to GitHub, so very recent unpushed work elsewhere is not "
                     "in it."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "A question, or the names and words to look for."},
         "project": {"type": "string", "description": "Optional: one project's name, e.g. owner/repo or repo."},
         "limit": {"type": "integer", "description": "How many passages, 1-20 (default 8)."}},
         "required": ["query"]}},
    {"name": "read_project_file",
     "description": "Reads lines of a file in one of your projects, when a search passage is not enough.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "path": {"type": "string"},
         "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
         "required": ["project", "path"]}},
    {"name": "refresh_project",
     "description": ("Re-indexes one of your projects (or all of them) from GitHub now, for changes pushed "
                     "since the last run. Takes from seconds to minutes; search again afterwards."),
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "project_notes",
     "description": ("Notes earlier sessions - in any workspace - wrote about your projects: how they connect, "
                     "what one uses of another, decisions and conventions that span them. Read them before "
                     "working on something that touches another project. Without a project, all of them."),
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "add_project_note",
     "description": ("Records one durable fact about a project that other sessions should know - above all how "
                     "projects connect: 'alishan_pos calls ai-gateway's /v1/messages with model auto'. Only "
                     "what you verified in the code or the user told you; not today's task, not guesses. "
                     "One fact per note, under 600 characters."),
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string", "description": "The project it is about (owner/repo or repo)."},
         "note": {"type": "string"}}, "required": ["project", "note"]}},
    {"name": "update_project_note",
     "description": ("Rewrites a note that is out of date or wrong - fix it the moment the code or the user shows "
                     "it no longer holds, before carrying on."),
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "note": {"type": "string"}},
                     "required": ["id", "note"]}},
    {"name": "remove_project_note",
     "description": "Removes a note that no longer holds or repeats another.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
]


# --- who is asking, and what they may see ------------------------------------------------------

class Caller:
    def __init__(self, principal, workspace):
        self.principal = principal
        self.user_id = principal.user.id if principal.user else None
        # Admins and managers also read the gateway's configured sources (its own code, the handbook)
        self.include_global = principal.is_manager
        self.workspace = (workspace or "").strip()[:200]

    def need_user(self):
        if self.user_id is None:
            raise ToolError("Project notes belong to a user; this key is the gateway's master key")

    async def sources(self):
        return await knowledge.list_sources(self.user_id, self.include_global) if self.user_id or self.include_global else []

    async def resolve(self, project):
        """A source's stored name from the name a person or agent uses for it."""
        wanted = (project or "").strip().strip("/").lower()
        if not wanted:
            raise ToolError("Name a project; list_projects shows them")
        names = [s["name"] for s in await self.sources()]
        for name in names:
            if knowledge.shown(name).lower() == wanted or name.lower() == wanted:
                return name
        matches = [n for n in names if knowledge.shown(n).lower().endswith("/" + wanted)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ToolError(f"'{project}' is ambiguous: " + ", ".join(knowledge.shown(n) for n in matches))
        raise ToolError(f"No project called '{project}'. list_projects shows yours.")


# --- the tools ---------------------------------------------------------------------------------

async def list_projects(caller, args):
    sources = await caller.sources()
    if not sources:
        return {"projects": [], "note": ("No projects are indexed for you yet. Connect your GitHub on the "
                                         "gateway's dashboard, under Your projects.")}
    return {"workspace": caller.workspace or None,
            "projects": [{"name": knowledge.shown(s["name"]), "description": s["description"] or None,
                          "files": s["files"], "indexed_at": s["indexed_at"],
                          **({"error": s["error"]} if s["error"] else {})} for s in sources]}


async def search_projects(caller, args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("Give a query")
    pattern = None
    if args.get("project"):
        pattern = await caller.resolve(args["project"])
    hits = await knowledge.search(query, pattern, int(args.get("limit") or 8),
                                  owner_id=caller.user_id, include_global=caller.include_global)
    if not hits:
        return {"results": [], "note": "Nothing matches. list_projects shows what is indexed."}
    return {"results": [{"project": knowledge.shown(h["source"]), "path": h["path"],
                         "lines": f'{h["start_line"]}-{h["end_line"]}', "content": h["content"]} for h in hits]}


async def read_project_file(caller, args):
    name = await caller.resolve(args.get("project"))
    try:
        found = await knowledge.read_file(name, str(args.get("path") or ""), args.get("start_line") or 1,
                                          args.get("end_line"), owner_id=caller.user_id,
                                          include_global=caller.include_global)
    except ValueError as e:
        raise ToolError(str(e))
    return {**found, "source": knowledge.shown(found["source"])}


async def refresh_project(caller, args):
    caller.need_user()
    project = args.get("project")
    if project:
        name = await caller.resolve(project)
        if not name.startswith(f"{knowledge.USER_PREFIX}{caller.user_id}/"):
            raise ToolError("Only your own connected projects can be refreshed from here")
        pattern = name
    else:
        pattern = f"{knowledge.USER_PREFIX}{caller.user_id}/*"
    return {"indexing": knowledge.request_reindex(pattern),
            "note": "Search again in a minute; a large project takes longer the first time."}


def _note_json(n):
    return {"id": n.id, "project": n.project, "note": n.content, "updated": n.updated_at.date().isoformat()}


async def project_notes(caller, args):
    caller.need_user()
    async with session_factory()() as session:
        query = select(ProjectNote).where(ProjectNote.user_id == caller.user_id)
        project = str(args.get("project") or "").strip()
        if project:
            # The project's own notes, and those of other projects that mention it
            short = project.split("/")[-1]
            query = query.where(or_(ProjectNote.project.ilike(f"%{short}"), ProjectNote.content.ilike(f"%{short}%")))
        notes = (await session.execute(query.order_by(ProjectNote.project, ProjectNote.id))).scalars().all()
    return {"notes": [_note_json(n) for n in notes]} if notes else {"notes": [], "note": "No notes yet."}


async def add_project_note(caller, args):
    caller.need_user()
    project = str(args.get("project") or "").strip()[:200]
    text = str(args.get("note") or "").strip()
    if not project or not text:
        raise ToolError("Give the project and the note")
    if len(text) > NOTE_CHARS:
        raise ToolError(f"Keep a note under {NOTE_CHARS} characters: one fact per note")
    async with session_factory()() as session:
        mine = (await session.execute(select(ProjectNote).where(ProjectNote.user_id == caller.user_id))).scalars().all()
        if any(n.content.strip().lower() == text.lower() for n in mine):
            raise ToolError("That note is already there")
        if len(mine) >= MAX_NOTES:
            raise ToolError(f"There are {MAX_NOTES} notes, the most kept: update or remove one first")
        note = ProjectNote(user_id=caller.user_id, project=project, content=text)
        session.add(note)
        await session.commit()
        return {"added": _note_json(note)}


async def _own_note(session, caller, note_id):
    note = await session.get(ProjectNote, int(note_id or 0))
    if note is None or note.user_id != caller.user_id:
        raise ToolError(f"No note {note_id}; project_notes lists them")
    return note


async def update_project_note(caller, args):
    caller.need_user()
    text = str(args.get("note") or "").strip()
    if not text or len(text) > NOTE_CHARS:
        raise ToolError(f"Give the note as it is true now, under {NOTE_CHARS} characters")
    async with session_factory()() as session:
        note = await _own_note(session, caller, args.get("id"))
        note.content, note.updated_at = text, utcnow()
        await session.commit()
        return {"updated": _note_json(note)}


async def remove_project_note(caller, args):
    caller.need_user()
    async with session_factory()() as session:
        note = await _own_note(session, caller, args.get("id"))
        await session.delete(note)
        await session.commit()
    return {"removed": int(args.get("id"))}


RUN = {"list_projects": list_projects, "search_projects": search_projects, "read_project_file": read_project_file,
       "refresh_project": refresh_project, "project_notes": project_notes, "add_project_note": add_project_note,
       "update_project_note": update_project_note, "remove_project_note": remove_project_note}


async def instructions(caller):
    """Said once, when an agent connects: what is here, and what is already known about this workspace."""
    text = ("This server gives you the user's other projects. When the task touches another project - an API "
            "this one calls, a library it uses, a service that calls it - search it (search_projects) instead "
            "of guessing, and read project_notes first: earlier sessions in other workspaces may already have "
            "recorded how the projects connect. Record what you verify about how projects connect "
            "(add_project_note), and fix or remove a note the moment the code shows it is wrong.")
    if caller.workspace:
        text += f" This session works in {caller.workspace}."
    if caller.user_id is None:
        return text
    try:
        notes = (await project_notes(caller, {"project": caller.workspace} if caller.workspace else {}))["notes"]
    except Exception:
        notes = []
    if notes:
        text += " Notes about it so far: " + " ".join(f"[{n['id']}] {n['project']}: {n['note']}" for n in notes[:15])
    return text


# --- JSON-RPC ----------------------------------------------------------------------------------

def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


async def _handle(caller, message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "Not a JSON-RPC 2.0 message")
    method, id_ = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if id_ is None:                     # a notification (initialized, cancelled ...): nothing to say
        return None
    if method == "initialize":
        return _result(id_, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "ai-gateway", "version": "1.0"},
                             "instructions": await instructions(caller)})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        run = RUN.get(name)
        if run is None:
            return _error(id_, -32602, f"No tool called {name}")
        try:
            output = await run(caller, args if isinstance(args, dict) else {})
            return _result(id_, {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False, indent=1)}],
                                 "isError": False})
        except ToolError as e:
            return _result(id_, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:
            log.warning("MCP tool %s failed: %s", name, e)
            return _result(id_, {"content": [{"type": "text", "text": f"{name} failed: {e.__class__.__name__}"}],
                                 "isError": True})
    return _error(id_, -32601, f"Method not found: {method}")


@router.post("/v1/mcp")
async def mcp(request: Request, principal: Principal = Depends(authenticate)):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "Not JSON"), status_code=400)
    caller = Caller(principal, request.headers.get("x-workspace"))
    if isinstance(body, list):
        answers = [a for a in [await _handle(caller, m) for m in body] if a is not None]
        return JSONResponse(answers) if answers else Response(status_code=202)
    answer = await _handle(caller, body)
    return JSONResponse(answer) if answer is not None else Response(status_code=202)


@router.get("/v1/mcp")
async def mcp_stream():
    # No server-initiated messages here, so no stream to open: the spec's answer for that is 405
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/v1/mcp")
async def mcp_end():
    return Response(status_code=204)
