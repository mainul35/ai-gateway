"""Tools the gateway answers itself: what it is running, on what machine, and whether it is healthy.

Every other tool the agent has comes from an MCP server somebody else wrote. These are the gateway
looking at itself - its models, its llama.cpp engines, the GPU and memory of the machine it runs on,
the services it leans on - so a model asked "why is the 80B slow" or "is ComfyUI up" can find out
rather than guess. They read and never change anything.

They describe the server's inside - addresses, ports, what is loaded - so they are offered to
managers and admins only, decided from who is asking before the model sees a tool list, the same way
the MCP servers are.

To the agent loop they look like one more tool server, named "gateway", so its tools are
gateway__status, gateway__models and gateway__services, and - for what the gateway and its owner's
projects are rather than how they are doing - gateway__knowledge_sources, gateway__knowledge_search
and gateway__read_file over the index in app/knowledge.py.
"""
import asyncio
import json
import logging
import socket
import time
from datetime import timedelta

import httpx
from sqlalchemy import func, select, text

from app import backends, knowledge, settings, usage
from app.db import session_factory
from app.engine.profiles import load_profiles
from app.engine.supervisor import supervisor
from app.models import ApiKey, CodingTask, CodingTaskEvent, Project, UsageRecord, User, utcnow
from app.tools import mcp_client
from utils.ollama_client import ollama_host
from utils.system_info import measure_hardware

log = logging.getLogger("tools.self")

SERVER = "gateway"
PROBE_SECONDS = 3
STARTED_AT = time.time()

GB = 1024 ** 3


class Builtin:
    """Stands where an MCP server would in the agent's tool map, so the loop knows to call here."""
    name = SERVER


BUILTIN = Builtin()


def is_available_to(principal):
    """Managers and admins, while the feature is on. Server internals are not for every user."""
    return bool(settings.get_bool("tools.self.enabled", "TOOLS_SELF_ENABLED", True)
                and principal is not None and principal.is_manager)


def _gb(value):
    return round((value or 0) / GB, 1)


async def _get_json(client, url):
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


# --- status ------------------------------------------------------------------------------------


def _measured():
    """GPU and memory as they are now, in GB, saying so when only the configured sizes are known."""
    info = measure_hardware()
    return {
        "gpus": [{"name": g["name"], "vram_total_gb": _gb(g["vram_total"]), "vram_free_gb": _gb(g["vram_free"])}
                 if info["gpu_source"] == "detected" else
                 {"name": g["name"], "vram_total_gb": _gb(g["vram_total"]),
                  "note": "configured size, not measured: no GPU is visible to the gateway"}
                 for g in info["gpu_info"]],
        "ram": {"total_gb": _gb(info["total_ram"]), "available_gb": _gb(info["available_ram"]),
                "swap_used_gb": _gb(info["swap_used"])},
        "cpu": {"cores": info["cpu_count"], "busy_percent": info["cpu_percent"]},
    }


async def status():
    """The machine and what is loaded on it, right now."""
    measured = await asyncio.to_thread(_measured)   # psutil and nvidia-smi block
    loaded, ollama_problem = [], None
    try:
        async with httpx.AsyncClient(timeout=PROBE_SECONDS) as client:
            for model in (await _get_json(client, f"{ollama_host()}/api/ps")).get("models") or []:
                loaded.append({"model": model.get("name"), "vram_gb": _gb(model.get("size_vram")),
                               "total_gb": _gb(model.get("size")), "expires_at": model.get("expires_at")})
    except (httpx.HTTPError, ValueError) as e:
        ollama_problem = f"{e.__class__.__name__}: {e}"

    since = utcnow() - timedelta(days=1)
    async with session_factory()() as db:
        users = (await db.execute(select(func.count(User.id)))).scalar() or 0
        keys = (await db.execute(select(func.count(ApiKey.id)).where(ApiKey.is_active.is_(True)))).scalar() or 0
        requests = (await db.execute(select(func.count(UsageRecord.id))
                                     .where(UsageRecord.created_at >= since))).scalar() or 0
        tokens = (await db.execute(select(func.coalesce(func.sum(UsageRecord.total_tokens), 0))
                                   .where(UsageRecord.created_at >= since))).scalar() or 0

    engine = supervisor.status()
    return {
        "host": socket.gethostname(),
        "gateway_uptime_minutes": int((time.time() - STARTED_AT) / 60),
        **measured,
        "ollama_loaded": loaded if not ollama_problem else f"could not ask Ollama: {ollama_problem}",
        "llamacpp_engines": {
            "profiles": engine["profiles"],
            "running": [{k: r[k] for k in ("name", "port", "uptime_seconds", "idle_seconds", "in_flight", "ttl_seconds")}
                        for r in engine["running"]],
        },
        "last_24h": {"requests": requests, "tokens": int(tokens)},
        "accounts": {"users": users, "active_api_keys": keys},
    }


# --- models ------------------------------------------------------------------------------------


async def models(name_contains=""):
    """Every model the gateway serves, with what it can do and how it has behaved this week."""
    available = await backends.available_models()
    async with session_factory()() as db:
        health = await usage.by_model(db, utcnow() - timedelta(days=7))
    profiles = load_profiles()
    wanted = (name_contains or "").lower()
    rows = []
    for name, backend in sorted(available.items()):
        if wanted and wanted not in name.lower():
            continue
        seen = health.get(name) or {}
        row = {"name": name, "served_by": backend.kind,
               "capabilities": sorted(backend.capabilities)}
        if backend.size_bytes:
            row["size_gb"] = _gb(backend.size_bytes)
        if backend.context_length:
            row["context_length"] = backend.context_length
        if name in profiles:
            profile = profiles[name]
            row["llamacpp_profile"] = {"ctx_size": profile.ctx_size, "ttl_seconds": profile.ttl_seconds,
                                       "exclusive": profile.exclusive}
        if seen:
            row["last_7_days"] = {k: seen[k] for k in ("requests", "failures", "avg_latency_ms", "tokens", "last_used")}
        rows.append(row)
    return {"count": len(rows), "models": rows}


# --- services ----------------------------------------------------------------------------------


def _rpc_servers():
    """Remote llama.cpp RPC servers the engine profiles use, from their --rpc arguments."""
    found = {}
    for name, profile in load_profiles().items():
        args = list(profile.extra_args)
        for index, arg in enumerate(args):
            if arg == "--rpc" and index + 1 < len(args):
                for address in str(args[index + 1]).split(","):
                    found.setdefault(address.strip(), []).append(name)
    return found


async def _tcp_open(address):
    host, _, port = address.rpartition(":")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), PROBE_SECONDS)
        writer.close()
        return True, None
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        return False, f"{e.__class__.__name__}: {e}"


async def services():
    """Whether each thing the gateway depends on answers, and how quickly."""
    async def http(name, url, detail=None):
        began = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=PROBE_SECONDS, follow_redirects=True) as client:
                response = await client.get(url)
            row = {"service": name, "url": url, "ok": response.status_code < 400,
                   "status": response.status_code, "ms": int((time.monotonic() - began) * 1000)}
            if detail and response.status_code < 400:
                try:
                    row.update(detail(response.json()))
                except ValueError:
                    pass
            return row
        except httpx.HTTPError as e:
            return {"service": name, "url": url, "ok": False, "error": f"{e.__class__.__name__}: {e}"}

    async def database():
        began = time.monotonic()
        try:
            async with session_factory()() as db:
                version = (await db.execute(text("select version()"))).scalar() or ""
            return {"service": "database", "ok": True, "ms": int((time.monotonic() - began) * 1000),
                    "version": version.split(",")[0]}
        except Exception as e:
            return {"service": "database", "ok": False, "error": f"{e.__class__.__name__}: {e}"}

    async def rpc(address, used_by):
        ok, problem = await _tcp_open(address)
        row = {"service": "llama.cpp RPC server", "address": address, "used_by": used_by, "ok": ok}
        if problem:
            row["error"] = problem
        return row

    probes = [http("ollama", f"{ollama_host()}/api/version", lambda d: {"version": d.get("version")}),
              database()]
    if settings.comfyui_url():
        probes.append(http("comfyui", f"{settings.comfyui_url()}/queue",
                           lambda d: {"running": len(d.get("queue_running") or []),
                                      "pending": len(d.get("queue_pending") or [])}))
    if settings.searxng_url():
        probes.append(http("searxng", f"{settings.searxng_url()}/healthz"))
    if settings.public_base_url():
        probes.append(http("public url", f"{settings.public_base_url()}/health"))
    probes += [rpc(address, used_by) for address, used_by in _rpc_servers().items()]
    rows = list(await asyncio.gather(*probes))

    for server in mcp_client.servers():
        if server.enabled:
            problem = server.trouble()
            rows.append({"service": f"mcp tool server {server.name}", "transport": server.transport,
                         "ok": problem is None, **({"error": problem} if problem else {})})
    return {"services": rows, "all_ok": all(row["ok"] for row in rows)}


# --- knowledge ---------------------------------------------------------------------------------


async def knowledge_sources():
    sources = await knowledge.list_sources()
    return {"sources": sources, "indexing_now": knowledge.progress["running"],
            "note": "Search them with knowledge_search; a source name or pattern narrows it."}


async def knowledge_search(args):
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("give a query: a question or the words to look for")
    hits = await knowledge.search(query, str(args.get("source") or "").strip() or None,
                                  int(args.get("limit") or 8))
    if not hits:
        return {"results": [], "note": "Nothing indexed matches. knowledge_sources shows what is indexed."}
    return {"results": [{"source": h["source"], "path": h["path"],
                         "lines": f'{h["start_line"]}-{h["end_line"]}', "content": h["content"]} for h in hits],
            "note": "Passages, best first. read_file shows more of a file around a passage."}


async def read_file(args):
    return await knowledge.read_file(str(args.get("source") or ""), str(args.get("path") or ""),
                                     args.get("start_line") or 1, args.get("end_line"))


# --- the tool list -----------------------------------------------------------------------------

TOOLS = {
    "status": {
        "description": ("This gateway's machine right now: GPU memory used and free, RAM, CPU, which "
                        "Ollama models and llama.cpp engines are loaded, requests and tokens in the last "
                        "24 hours. Use it for questions about load, memory, speed, or what is running."),
        "schema": {"type": "object", "properties": {}},
        "run": lambda args: status(),
    },
    "models": {
        "description": ("Every model this gateway serves: which backend serves it (ollama or llamacpp), "
                        "capabilities (tools, vision, thinking), size, context length, llama.cpp launch "
                        "profile, and requests, failures and latency over the last 7 days."),
        "schema": {"type": "object", "properties": {
            "name_contains": {"type": "string", "description": "Only models whose name contains this."}}},
        "run": lambda args: models(str(args.get("name_contains") or "")),
    },
    "services": {
        "description": ("Health check of everything the gateway depends on: Ollama, the database, "
                        "ComfyUI (image generation), SearXNG (web search), the public URL through "
                        "Cloudflare, remote llama.cpp RPC servers, and the MCP tool servers."),
        "schema": {"type": "object", "properties": {}},
        "run": lambda args: services(),
    },
    "knowledge_sources": {
        "description": ("What the knowledge base holds: this gateway's own code, the owner's other "
                        "projects (their GitHub repositories) and the homelab handbook - with a one-line "
                        "description of each, file counts, and when each was last indexed. Use it to see "
                        "which projects exist before searching."),
        "schema": {"type": "object", "properties": {}},
        "run": lambda args: knowledge_sources(),
    },
    "knowledge_search": {
        "description": ("Searches the code and documents of this gateway, the owner's other projects and "
                        "the homelab handbook (servers, services, ports, deploys, backups), by meaning and "
                        "by exact words. Use it for how something works, where something is defined, "
                        "how a project is built or deployed, or anything about the owner's projects and "
                        "machines. Returns passages with their file and line numbers."),
        "schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "A question, or names and words to find."},
            "source": {"type": "string", "description": ("Optional: only this source, e.g. ai-gateway or "
                                                         "mainul35/homelab-handbook; * works as a wildcard.")},
            "limit": {"type": "integer", "description": "How many passages, 1-20. Default 8."}},
            "required": ["query"]},
        "run": knowledge_search,
    },
    "read_file": {
        "description": ("Reads lines of a file that is in the knowledge base, for when a search passage is "
                        "not enough. Up to 400 lines at a time."),
        "schema": {"type": "object", "properties": {
            "source": {"type": "string", "description": "The source, as knowledge_search returned it."},
            "path": {"type": "string", "description": "The file's path, as knowledge_search returned it."},
            "start_line": {"type": "integer", "description": "First line, 1-based. Default 1."},
            "end_line": {"type": "integer", "description": "Last line. Default start_line + 199."}},
            "required": ["source", "path"]},
        "run": read_file,
    },
    "coding_projects": {
        "description": ("The projects coding tasks can work on: GitHub repositories cloned on the server and "
                        "folders on people's own computers, with whether each folder is connected right now."),
        "schema": {"type": "object", "properties": {}},
        "run": lambda args, context: coding_projects(),
        "context": True,
    },
    "start_coding_task": {
        "description": ("Starts a coding task: the gateway works on a project in the background - explores it, "
                        "makes the change, runs its checks - and ends with a pull request to approve, changes to "
                        "keep or undo in a local folder, or an answer. Use it only when the person asks for a "
                        "change to a project, or for a question about one to be worked out, and say which "
                        "project. Give the task in full, as the person put it. Returns the task's number and a "
                        "link; tell the person the link. It does not wait for the task to finish."),
        "schema": {"type": "object", "properties": {
            "project": {"type": "string", "description": "The project's name, or part of it, from coding_projects."},
            "task": {"type": "string", "description": "What should change, or what should be found out."}},
            "required": ["project", "task"]},
        "run": lambda args, context: start_coding_task(args, context),
        "context": True,
    },
    "coding_task_status": {
        "description": ("Where a coding task is: its status and stage, its last steps, and when it is done its "
                        "answer, summary, pull request or changed files."),
        "schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]},
        "run": lambda args, context: coding_task_status(args),
        "context": True,
    },
}


# --- coding tasks ------------------------------------------------------------------------------

DEFAULT_TASK_MODEL = "qwen3-next-80b"


async def coding_projects():
    from app.coding import remote, workspace
    async with session_factory()() as db:
        projects = (await db.execute(select(Project).order_by(Project.name))).scalars().all()
    rows = []
    for p in projects:
        kind = ("folder on a computer" if p.kind == "local" else
                "GitHub repository" if workspace.github_of(p.clone_url) else "git repository on the server")
        row = {"name": p.name, "kind": kind,
               "checked_with": p.verify_command or None}
        if p.kind == "local":
            row["connected"] = bool(remote.connection(p.id))
        rows.append(row)
    return {"projects": rows, "note": "Projects are added on the Tasks page (/tasks)."}


async def _project_named(db, wanted):
    wanted = (wanted or "").strip().lower()
    projects = (await db.execute(select(Project))).scalars().all()
    exact = [p for p in projects if p.name.lower() == wanted or p.name.lower().split("/")[-1] == wanted]
    found = exact or [p for p in projects if wanted and wanted in p.name.lower()]
    if len(found) != 1:
        names = ", ".join(p.name for p in projects) or "none yet"
        raise ValueError(("No project matches" if not found else f"{len(found)} projects match")
                         + f" {wanted!r}. The projects are: {names}.")
    return found[0]


async def start_coding_task(args, context):
    from app.coding import runner as coding_runner
    principal = context.get("principal")
    if principal is None:
        raise PermissionError("Coding tasks can be started only from a signed-in chat")
    available = await backends.available_models()
    model = context.get("model")
    # The chat's own model when it can do the work, otherwise the one that has done it best here
    if not (model in available and "tools" in available[model].capabilities):
        model = DEFAULT_TASK_MODEL if DEFAULT_TASK_MODEL in available else model
    async with session_factory()() as db:
        project = await _project_named(db, args.get("project"))
        task = await coding_runner.create(db, principal, project, str(args.get("task") or ""), model,
                                          context.get("conversation_id"), by="from a playground chat")
    link = f"{settings.public_base_url() or ''}/tasks?task={task.id}"
    note = ""
    if project.kind == "local":
        from app.coding import remote
        if not remote.connection(project.id):
            note = (" The folder is not connected at the moment, so the task will stop at once: open the Tasks "
                    "page on that computer and connect it, or start the helper there, then press Continue.")
    return {"task_id": task.id, "project": project.name, "model": model, "status": "queued", "link": link,
            "note": "It runs in the background; check on it with coding_task_status." + note}


async def coding_task_status(args):
    try:
        task_id = int(args.get("task_id"))
    except (TypeError, ValueError):
        raise ValueError("Give the task's number")
    async with session_factory()() as db:
        task = await db.get(CodingTask, task_id)
        if task is None:
            raise ValueError(f"There is no coding task #{task_id}")
        project = await db.get(Project, task.project_id)
        steps = (await db.execute(select(CodingTaskEvent).where(CodingTaskEvent.task_id == task_id,
                                                                CodingTaskEvent.kind.in_(("stage", "note", "error",
                                                                                          "review", "done", "verify")))
                                  .order_by(CodingTaskEvent.id.desc()).limit(6))).scalars().all()
    out = {"task_id": task.id, "project": project.name if project else None, "status": task.status,
           "stage": task.stage, "last_steps": [f"{e.kind}: {e.text[:300]}" for e in reversed(steps)],
           "link": f"{settings.public_base_url() or ''}/tasks?task={task.id}"}
    if task.error:
        out["error"] = task.error
    if task.summary:
        out["answer" if task.status == "done" and not task.diff else "summary"] = task.summary[:6000]
    if task.pr_url:
        out["pull_request"] = task.pr_url
    if task.diff:
        out["files_changed"] = sorted({line[6:] for line in task.diff.splitlines() if line.startswith("+++ b/")})
    return out


KNOWLEDGE_TOOLS = {"knowledge_sources", "knowledge_search", "read_file"}
CODING_TOOLS = {"coding_projects", "start_coding_task", "coding_task_status"}


def listing():
    """The tools, in the shape the agent loop turns into function definitions."""
    from app.coding import runner as coding_runner
    return [{"server": SERVER, "name": name, "description": tool["description"], "schema": tool["schema"]}
            for name, tool in TOOLS.items()
            if (name not in KNOWLEDGE_TOOLS or knowledge.is_enabled())
            and (name not in CODING_TOOLS or coding_runner.is_enabled())]


async def call(tool, args, context=None):
    """Runs one tool. Returns (text, failed), like mcp_client.call. `context` is who is asking, with
    which model, in which conversation - what the coding tools need to start a task as that person."""
    entry = TOOLS.get(tool)
    if entry is None:
        return f"{SERVER} has no tool called {tool}.", True
    try:
        if entry.get("context"):
            result = await entry["run"](args or {}, context or {})
        else:
            result = await entry["run"](args or {})
    except (ValueError, PermissionError) as e:     # the model asked for something that is not there
        return str(e), True
    except Exception as e:
        log.warning("gateway tool %s failed: %s", tool, e, exc_info=True)
        return f"{e.__class__.__name__}: {e}", True
    return json.dumps(result, ensure_ascii=False, default=str, indent=1), False
