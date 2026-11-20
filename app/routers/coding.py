"""Coding tasks: projects the gateway may change, and the work it does on them.

Managers and admins add projects and start tasks. Only an admin approves a finished task - which
merges it and runs the project's deploy command on this server - and only an admin sets that
command, since it is code this server runs.
"""
import asyncio
import json
import re

import httpx

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github_access, knowledge
from app.auth import Principal, require_admin, require_manager
from app.coding import apps as apps_of_tasks, attachments, detect, projects, remote, runner, workspace
from app.db import get_session
from app.models import ChatFile, CodingTask, CodingTaskEvent, Project
from app.tools import markdown, slides

def _switched_on(request: Request):
    """Every coding endpoint, not only the ones that start work, is closed while coding.enabled is off:
    listing, approving and retrying tasks, and the channel a local helper polls for commands. Only the
    GitHub tokens stay reachable - the knowledge base reads repositories with them too."""
    if not runner.is_enabled() and not request.url.path.startswith("/coding/github"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Coding tasks are switched off (coding.enabled)")


router = APIRouter(prefix="/coding", tags=["coding"], dependencies=[Depends(_switched_on)])

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,200}$")


def _enabled():
    if not runner.is_enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Coding tasks are switched off (coding.enabled)")


def _project_out(project, principal=None, detected=None):
    out = {"id": project.id, "name": project.name, "kind": project.kind or "git", "client": project.client,
           "clone_url": project.clone_url, "base_branch": project.base_branch,
           "verify_command": project.verify_command, "sandbox_image": project.sandbox_image,
           "sandbox_network": project.sandbox_network, "deploy_command": project.deploy_command,
           "notes": project.notes, "mine": bool(principal and principal.user and project.added_by == principal.user.id)}
    if project.kind == "local":
        connected = remote.connection(project.id)
        out["connected"] = connected["client"] if connected else None
        out["can_run"] = bool(connected and connected["can_run"])
    else:
        out["on_github"] = workspace.github_of(project.clone_url) is not None
        out["can_push"] = bool(workspace.token_for(project.clone_url, project.github_token)) or not out["on_github"]
        out["github_token"] = project.github_token
        if project.github_token and out["on_github"]:
            entry = github_access.chosen(workspace.github_of(project.clone_url)[0], project.github_token)
            out["github_token_name"] = (entry["name"] or f"#{entry['id']}") if entry else None
    if detected:
        out["detected"] = detected
    return out


def _task_out(task, project=None, full=False):
    out = {"id": task.id, "project_id": task.project_id, "project": project.name if project else None,
           "description": task.description, "model": task.model, "status": task.status, "stage": task.stage,
           "branch": task.branch, "pr_url": task.pr_url, "error": task.error,
           "created_at": task.created_at.isoformat(), "updated_at": task.updated_at.isoformat(),
           "apps": json.loads(task.apps) if task.apps else [],
           "files": json.loads(task.files) if task.files else [], "thread_id": task.thread_id,
           "kind": task.kind or "code", "sources": json.loads(task.sources) if task.sources else [],
           # A presentation's deck, served from this task (the file itself is its maker's own)
           "result": {"url": f"/coding/tasks/{task.id}/presentation"} if task.result_file_id else None}
    out["can_continue"] = _can_continue(task)
    if full:
        out.update(plan=task.plan, summary=task.summary, diff=task.diff, verify_output=task.verify_output,
                   # The model writes them in Markdown, as in the playground, and they are drawn the same way
                   summary_html=markdown.render(task.summary) if task.summary else None,
                   plan_html=markdown.render(task.plan) if task.plan else None)
    return out


def _can_continue(task):
    """Stopped part-way with its conversation saved, in a stage it can be picked up from."""
    if task.status not in ("failed", "cancelled") or not task.state:
        return False
    try:
        state = json.loads(task.state)
    except ValueError:
        return False
    return bool(state.get("messages")) and state.get("stage") in ("explore", "edit", "ship", "gather")


# --- projects ----------------------------------------------------------------------------------


class ProjectIn(BaseModel):
    """Only the address is needed: the rest is worked out from the project's files, and anything
    given here overrides what was worked out."""
    clone_url: str = Field(min_length=3, max_length=512)
    base_branch: str | None = Field(default=None, max_length=128)
    verify_command: str | None = Field(default=None, max_length=4000)
    sandbox_image: str | None = Field(default=None, max_length=256)
    sandbox_network: bool | None = None
    deploy_command: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)
    github_token: int | None = Field(default=None, ge=0)   # one of its owner's tokens; 0 or none: any that reaches it


class SnapshotIn(BaseModel):
    """What a folder on somebody's computer says about itself, for detection: which of
    detect.KEY_FILES exist, the text of detect.READ_FILES, and whether it holds any Python."""
    present: list[str] = Field(default_factory=list, max_length=100)
    contents: dict[str, str] = Field(default_factory=dict)
    has_python: bool = False


class LocalProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    client: str = Field(pattern="^(browser|helper)$")
    snapshot: SnapshotIn = Field(default_factory=SnapshotIn)


class ProjectPatch(BaseModel):
    base_branch: str | None = Field(default=None, max_length=128)
    verify_command: str | None = Field(default=None, max_length=4000)
    sandbox_image: str | None = Field(default=None, max_length=256)
    sandbox_network: bool | None = None
    deploy_command: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)
    github_token: int | None = Field(default=None, ge=0)   # 0: no longer one chosen


def _owns(principal, project):
    """A project is its owner's - the person who added it - and the admins'. Managers used to be
    able to change any project's commands and start tasks on any of them, including another
    person's folder on their own computer."""
    return principal.is_admin or (principal.user is not None and project.added_by == principal.user.id)


def _require_owner(principal, project):
    if not _owns(principal, project):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who added this project, or an admin, can do that")


def _check_branch(name):
    if not workspace.valid_branch(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name!r} is not a branch name")


@router.get("/projects")
async def list_projects(principal: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    projects = (await session.execute(select(Project).order_by(Project.name))).scalars().all()
    return {"projects": [_project_out(p, principal) for p in projects]}


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def add_project(payload: ProjectIn, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    _enabled()
    try:
        project, kind = await projects.add_git(
            session, principal, payload.clone_url, base_branch=payload.base_branch,
            verify_command=payload.verify_command, sandbox_image=payload.sandbox_image,
            sandbox_network=payload.sandbox_network, deploy_command=payload.deploy_command, notes=payload.notes,
            github_token=payload.github_token)
    except projects.ProjectError as e:
        raise HTTPException(e.status_code, str(e))
    return _project_out(project, principal, detected=kind)


async def _local_name(session, wanted, user_id):
    """A name for a local project: the folder's, made unique if somebody already has one like it."""
    base = re.sub(r"[^\w .-]+", "-", wanted).strip(" .-")[:90] or "project"
    name, n = base, 1
    while True:
        existing = (await session.execute(select(Project).where(Project.name == name))).scalar_one_or_none()
        if existing is None:
            return name, None
        if existing.kind == "local" and existing.added_by == user_id:
            return name, existing          # the same folder, connected again
        n += 1
        name = f"{base} ({n})"


@router.post("/projects/local", status_code=status.HTTP_201_CREATED)
async def add_local_project(payload: LocalProjectIn, principal: Principal = Depends(require_manager),
                            session: AsyncSession = Depends(get_session)):
    """A folder on the caller's own computer, served by their browser or the helper. Adding the same
    folder name again from the same person gives back the project they already have."""
    _enabled()
    if principal.user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Sign in (or use your own API key) to add a folder")
    name, existing = await _local_name(session, payload.name, principal.user.id)
    snapshot = detect.Snapshot([p for p in payload.snapshot.present if p in detect.KEY_FILES],
                               {k: v[:200_000] for k, v in payload.snapshot.contents.items() if k in detect.READ_FILES},
                               payload.snapshot.has_python)
    found = detect.detect(snapshot)
    if existing:
        existing.client = payload.client
        await session.commit()
        return _project_out(existing, principal, detected=found["kind"])
    project = Project(name=name, kind="local", client=payload.client, clone_url="", base_branch="",
                      verify_command=found["local_verify"], sandbox_image=found["image"], sandbox_network=False,
                      added_by=principal.user.id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return _project_out(project, principal, detected=found["kind"])


# --- GitHub access -----------------------------------------------------------------------------


@router.get("/github")
async def github_tokens(_: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    """Owners with a token (masked), and owners that projects or the knowledge base use without one."""
    have = github_access.owners()
    named = {o["owner"].lower() for o in have}
    wanted = set()
    for (url,) in (await session.execute(select(Project.clone_url).where(Project.kind == "git"))).all():
        found = workspace.github_of(url)
        if found:
            wanted.add(found[0])
    for spec in knowledge.load_config().get("sources") or []:
        if spec.get("kind") == "github":
            wanted.update(spec.get("owners") or [])
    return {"tokens": have, "missing": sorted(o for o in wanted if o.lower() not in named)}


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=400)
    owner: str = Field(default="", max_length=39)       # empty: found from the token
    replace: int | None = Field(default=None, ge=1)     # a token's id: swaps that one instead of adding
    name: str = Field(default="", max_length=40)        # to tell it apart, e.g. what it is for

    @field_validator("replace", mode="before")
    @classmethod
    def _older_page(cls, value):
        # A Tasks page loaded before an owner could have several tokens sends true or false here:
        # false adds, true swaps the owner's first token, as it did then
        if isinstance(value, bool):
            return 1 if value else None
        return value


async def _save_token(owner, payload):
    try:
        return await github_access.save(owner, payload.token, replace=payload.replace, name=payload.name)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"GitHub could not be reached: {e}")


@router.post("/github")
async def add_github_token(payload: TokenIn, _: Principal = Depends(require_admin)):
    """Checks the token with GitHub, then adds it to its owner's tokens. Never echoed back."""
    return await _save_token(payload.owner, payload)


@router.put("/github/{owner}")
async def set_github_token(owner: str, payload: TokenIn, _: Principal = Depends(require_admin)):
    return await _save_token(owner, payload)


@router.post("/github/{owner}/check")
async def check_github_token(owner: str, id: int | None = None, _: Principal = Depends(require_admin)):
    """What one of an owner's tokens can do (its first, without an id)."""
    if id is None:
        value = github_access.token(owner, "write") or github_access.token(owner, "read")
    else:
        value = next((e["value"] for e in github_access.entries(owner) if e["id"] == id), "")
    if not value:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No such token for {owner}")
    try:
        found = await github_access.check(owner, value)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"GitHub could not be reached: {e}")
    return {"owner": owner, "id": id, **found}


class TokenNameIn(BaseModel):
    name: str = Field(default="", max_length=40)


@router.put("/github/{owner}/{number}/name")
async def name_github_token(owner: str, number: int, payload: TokenNameIn, _: Principal = Depends(require_admin)):
    try:
        github_access.rename(owner, number, payload.name)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"owner": owner, "id": number, "name": " ".join(payload.name.split())}


@router.delete("/github/{owner}")
async def remove_github_token(owner: str, id: int = 1, _: Principal = Depends(require_admin),
                              session: AsyncSession = Depends(get_session)):
    # A project that names this token would stop working without a word: say which ones first
    using = [p.name for p in (await session.execute(select(Project).where(Project.github_token == id))).scalars()
             if (workspace.github_of(p.clone_url) or ("",))[0].lower() == owner.lower()]
    if using:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{', '.join(using)} use{'s' if len(using) == 1 else ''} this "
                            f"token. Choose another for {'it' if len(using) == 1 else 'them'} in the project's settings first.")
    try:
        github_access.remove(owner, id)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"removed": owner, "id": id}


# --- the relay to a folder on somebody's computer ----------------------------------------------


async def _owned_local(session, project_id, principal):
    project = await session.get(Project, project_id)
    if project is None or project.kind != "local":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such local project")
    if not principal.user or project.added_by != principal.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who added this folder can connect it")
    return project


@router.get("/remote/{project_id}/next")
async def remote_next(project_id: int, client: str = "browser", can_run: bool = False,
                      principal: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    """Long-poll: the next operation for this folder, or {"request": null} after ~25 seconds."""
    await _owned_local(session, project_id, principal)
    await session.close()            # do not hold a database connection for the length of the wait
    request = await remote.next_request(project_id, "helper" if client == "helper" else "browser",
                                        can_run and client == "helper")
    return {"request": request}


class AnswerIn(BaseModel):
    id: int
    ok: bool
    result: object = None
    error: str | None = Field(default=None, max_length=4000)


@router.post("/remote/{project_id}/answer")
async def remote_answer(project_id: int, payload: AnswerIn, principal: Principal = Depends(require_manager),
                        session: AsyncSession = Depends(get_session)):
    await _owned_local(session, project_id, principal)
    return {"accepted": remote.answer(project_id, payload.id, payload.ok, payload.result, payload.error)}


@router.patch("/projects/{project_id}")
async def change_project(project_id: int, payload: ProjectPatch, principal: Principal = Depends(require_manager),
                         session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    _require_owner(principal, project)
    changes = payload.model_dump(exclude_unset=True)
    if "sandbox_network" in changes and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can choose whether a project gets a network")
    if changes.get("base_branch"):
        _check_branch(changes["base_branch"].strip())
    if "deploy_command" in changes and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can set a deploy command")
    if "sandbox_image" in changes and not IMAGE.match(changes["sandbox_image"] or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a container image name")
    if "github_token" in changes:
        chosen = changes.pop("github_token") or None
        if chosen != project.github_token:
            if chosen:
                try:
                    projects.check_token_choice(principal, project.clone_url, chosen)
                except projects.ProjectError as e:
                    raise HTTPException(e.status_code, str(e))
            elif not principal.is_admin:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can choose which GitHub token a project uses")
            project.github_token = chosen
    for key, value in changes.items():
        if value is None and key in ("base_branch", "sandbox_image", "verify_command", "sandbox_network"):
            continue                                    # left empty in the form: keep what is there
        setattr(project, key, value.strip() if isinstance(value, str) else value)
    if project.deploy_command == "":
        project.deploy_command = None
    await session.commit()
    return _project_out(project, principal)


@router.delete("/projects/{project_id}")
async def remove_project(project_id: int, _: Principal = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    await session.delete(project)
    await session.commit()
    return {"removed": project.name, "note": "The clone on the server is kept."}


# --- tasks -------------------------------------------------------------------------------------


class TaskIn(BaseModel):
    # None only for a presentation made without a project
    project_id: int | None = None
    description: str = Field(min_length=5, max_length=20_000)
    model: str = Field(max_length=256)
    conversation_id: int | None = None
    # Outside apps (tool servers) it may use; none unless ticked
    apps: list[str] = Field(default_factory=list, max_length=apps_of_tasks.MOST_APPS)
    # Pictures and documents uploaded through /chat/files, given with the description
    files: list[int] = Field(default_factory=list, max_length=20)
    # "code", or "presentation": a deck made from the description, the files and these links
    kind: str = Field(default="code", pattern="^(code|presentation)$")
    sources: list[str] = Field(default_factory=list, max_length=20)


async def _start(session, principal, project, description, model, conversation_id=None, apps=None, files=None,
                 **more):
    try:
        return await runner.create(session, principal, project, description, model, conversation_id, apps=apps,
                                   files=files, **more)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskIn, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    _enabled()
    project = None
    if payload.project_id is not None:
        project = await session.get(Project, payload.project_id)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    task = await _start(session, principal, project, payload.description, payload.model,
                        payload.conversation_id, payload.apps, payload.files,
                        kind=payload.kind, sources=payload.sources)
    return _task_out(task, project)


@router.get("/apps")
async def task_apps(principal: Principal = Depends(require_manager)):
    """The outside apps this person may give a task, and whether more can be added (by an admin)."""
    return {"apps": apps_of_tasks.listing(principal), "can_add": principal.is_admin}


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=8_000)
    model: str | None = Field(default=None, max_length=256)
    files: list[int] = Field(default_factory=list, max_length=20)


# How much of the earlier task a follow-up carries: its question and its answer, enough to go on from
FOLLOW_UP_CHARS = 8_000
# And of the turns before it in the same thread, newest first, until this much is used
EARLIER_TURNS_CHARS = 6_000
# What a follow-up's description carries after its question (the Tasks page folds it away)
FOLLOWS = "\n\n---\nThis follows task #"


def question_of(description):
    """What a task was asked, without what a follow-up carries of the tasks before it."""
    return (description or "").split(FOLLOWS, 1)[0].strip()


def _found(task):
    return (task.summary or task.plan or "").strip()


async def _thread(session, task):
    """Every task of the thread `task` is in, first to last: the task it started from and the
    follow-ups asked on it."""
    first = task.thread_id or task.id
    return (await session.execute(select(CodingTask).where((CodingTask.id == first) | (CodingTask.thread_id == first))
                                  .order_by(CodingTask.id))).scalars().all()


def follow_up_context(task, earlier_turns):
    """What a follow-up question is given of the thread so far: the task it is asked on in full, and
    the turns before that, newest first, as far as they fit."""
    found = _found(task)
    context = (f"{FOLLOWS}{task.id}, which was asked:\n{question_of(task.description)[:2_000]}"
               + (f"\n\nWhat it found:\n{found[:FOLLOW_UP_CHARS]}" if found else ""))
    turns, used = [], 0
    for turn in reversed(earlier_turns):
        text = f"#{turn.id} was asked:\n{question_of(turn.description)[:1_500]}"
        if _found(turn):
            text += f"\nIt found:\n{_found(turn)[:2_500]}"
        if used + len(text) > EARLIER_TURNS_CHARS:
            break
        turns.insert(0, text)
        used += len(text)
    if turns:
        context += "\n\nEarlier in the same conversation, oldest first:\n\n" + "\n\n".join(turns)
    return context


@router.post("/tasks/{task_id}/ask", status_code=status.HTTP_201_CREATED)
async def ask_more(task_id: int, payload: AskIn, principal: Principal = Depends(require_manager),
                   session: AsyncSession = Depends(get_session)):
    """More about a task or its project, asked in the same thread: it runs as a task of its own on
    the same project, with the same apps (so it has its own steps, changes and review), starting from
    what the thread was asked and found so far, and the Tasks page shows it under the earlier turns."""
    _enabled()
    task, project = await _task(session, task_id)
    deck = task.kind == "presentation"
    if project is None and (task.project_id is not None or not deck):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Its project is gone")
    question = payload.question.strip()
    thread = await _thread(session, task)
    context = follow_up_context(task, [t for t in thread if t.id < task.id])
    model = (payload.model or "").strip() or task.model
    apps = json.loads(task.apps) if task.apps else []
    files = payload.files
    if deck:
        # The next version of a deck keeps what the last was made from - its template, its pictures -
        # with anything new added
        kept = [i for i in (json.loads(task.files) if task.files else []) if i not in files]
        files = (kept + list(files))[-attachments.MOST_FILES:]
    try:
        follow_up = await runner.create(session, principal, project, question + context, model, task.conversation_id,
                                        apps=apps, files=files, thread_id=task.thread_id or task.id,
                                        kind=task.kind, sources=json.loads(task.sources) if task.sources else None)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _task_out(follow_up, project)


@router.get("/tasks")
async def list_tasks(project_id: int | None = None, limit: int = 50, _: Principal = Depends(require_manager),
                     session: AsyncSession = Depends(get_session)):
    # Outer: a presentation may have no project
    query = select(CodingTask, Project).outerjoin(Project).order_by(CodingTask.id.desc()).limit(max(1, min(limit, 200)))
    if project_id:
        query = query.where(CodingTask.project_id == project_id)
    rows = (await session.execute(query)).all()
    return {"tasks": [_task_out(t, p) for t, p in rows]}


async def _task(session, task_id):
    task = await session.get(CodingTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such task")
    return task, (await session.get(Project, task.project_id) if task.project_id else None)


async def _deck_of(session, task_id):
    """A presentation task's deck (a ChatFile), and the task."""
    task, _project = await _task(session, task_id)
    stored = await session.get(ChatFile, task.result_file_id) if task.result_file_id else None
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This task has no presentation")
    return task, stored


@router.get("/tasks/{task_id}/presentation")
async def task_presentation(task_id: int, download: bool = False, _: Principal = Depends(require_manager),
                            session: AsyncSession = Depends(get_session)):
    """The deck a presentation task made, for whoever may see the task."""
    _task_row, stored = await _deck_of(session, task_id)
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
    if download:
        from app.routers.chat import _attachment
        headers["Content-Disposition"] = _attachment(stored.name or f"task-{task_id}.pptx")
    return Response(stored.data, media_type=stored.mime_type, headers=headers)


@router.get("/tasks/{task_id}/slides")
async def task_slides(task_id: int, _: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    """The deck slide by slide, for the page: read from the Markdown it was made from."""
    _task_row, stored = await _deck_of(session, task_id)
    try:
        return {"name": stored.name, "slides": await asyncio.to_thread(slides.preview, stored.prompt or "")}
    except slides.SlidesError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, _: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    task, project = await _task(session, task_id)
    out = _task_out(task, project, full=True)
    out["attached"] = await attachments.described(session, out["files"])
    # The whole conversation it is part of, for the page to show around it
    thread = await _thread(session, task)
    out["thread"] = [] if len(thread) < 2 else [
        {"id": t.id, "question": question_of(t.description), "status": t.status, "model": t.model,
         "has_diff": bool(t.diff), "error": t.error,
         "summary_html": markdown.render(t.summary) if t.summary else None, "summary": t.summary}
        for t in thread]
    return out


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: int, after: int = 0, _: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(CodingTaskEvent).where(CodingTaskEvent.task_id == task_id,
                                                                CodingTaskEvent.id > after)
                                  .order_by(CodingTaskEvent.id).limit(500))).scalars().all()
    task = await session.get(CodingTask, task_id)
    return {"status": task.status if task else None, "stage": task.stage if task else None,
            "events": [{"id": e.id, "kind": e.kind, "text": e.text, "data": json.loads(e.data) if e.data else None,
                        "at": e.created_at.isoformat()} for e in rows]}


def _changes_something(task, project):
    """A presentation changes nothing, so there is nothing to approve or take back."""
    if task.kind == "presentation" or project is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A presentation changes nothing, so there is nothing to "
                                                      "approve or undo")


def _may_decide(principal, project):
    """Who decides what happens to a finished task: an admin for a project on GitHub, since that
    merges and deploys; for a folder on somebody's computer, its owner too - it is their folder."""
    if principal.is_admin:
        return True
    return project.kind == "local" and principal.user is not None and project.added_by == principal.user.id


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: int, principal: Principal = Depends(require_manager),
                       session: AsyncSession = Depends(get_session)):
    """Merges and deploys a project's task; for a local folder, keeps the changes already made."""
    task, project = await _task(session, task_id)
    _changes_something(task, project)
    if not _may_decide(principal, project):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can approve this")
    if task.status != "review":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Only a task waiting for review can be approved; this one is {task.status}")
    if principal.user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Approve while signed in, so it is known who did")
    if project.kind == "local":
        await runner.keep_local(task_id, principal.user)
        return {"status": "done"}
    task.status = "approved"
    await session.commit()
    asyncio.create_task(runner.approve(task_id, principal.user))
    return {"status": "approved"}


@router.post("/tasks/{task_id}/reject")
async def reject_task(task_id: int, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    """Rejects a task. For a local folder that means undoing it: every file put back as it was."""
    task, project = await _task(session, task_id)
    _changes_something(task, project)
    if task.status not in ("review", "failed", "cancelled"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status} task cannot be rejected")
    if project.kind == "local":
        if not _may_decide(principal, project):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the folder's owner or an admin can undo this")
        try:
            await runner.undo_local(task_id, principal.user or principal)
        except (remote.NotConnected, remote.ClientError) as e:
            raise HTTPException(status.HTTP_409_CONFLICT, f"Could not undo it: {e}")
        return {"status": "rejected"}
    if task.status == "cancelled":
        raise HTTPException(status.HTTP_409_CONFLICT, "A cancelled task has nothing to reject")
    await runner.reject(task_id, principal.user or principal)
    return {"status": "rejected"}


@router.get("/tasks/{task_id}/patch")
async def task_patch(task_id: int, _: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    """The task's changes as a patch file, for `git apply` on any copy of the project."""
    task, _project = await _task(session, task_id)
    if not task.diff:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This task has no changes")
    return PlainTextResponse(task.diff if task.diff.endswith("\n") else task.diff + "\n", media_type="text/x-diff",
                             headers={"Content-Disposition": f'attachment; filename="task-{task_id}.patch"'})


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, _: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    task, _project = await _task(session, task_id)
    if task.status == "queued":
        task.status = "cancelled"
        await session.commit()
        return {"status": "cancelled"}
    if task.status != "running":
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status} task is not running")
    runner.cancel(task_id)
    return {"status": "cancelling"}


# Still going, or waiting for someone to decide: these are finished first (cancelled, approved, rejected)
NOT_REMOVABLE = ("queued", "running", "approved", "merged", "deploying", "review")


@router.delete("/tasks/{task_id}")
async def remove_task(task_id: int, thread: bool = False, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    """Takes a finished task off the list, with its steps - with `thread`, every task of its thread.
    Its branch and pull request, if any, are left where they are; only the server's working copy of
    it goes."""
    task, project = await _task(session, task_id)
    whole = await _thread(session, task)
    going = whole if thread else [task]
    for each in going:
        name = f"#{each.id} " if len(going) > 1 else ""
        if each.status in NOT_REMOVABLE:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Review {name or 'it '}first: approve or reject it" if each.status == "review"
                                else f"{name or 'It '}is {each.status}; cancel it or let it finish first")
        if not principal.is_admin and (principal.user is None or each.user_id != principal.user.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Only the person who started this task, or an admin, can remove it")
    removed = {each.id for each in going}
    # The first task of a thread going alone: the next one becomes the first, so the rest stay together
    left = [t for t in whole if t.id not in removed]
    if left and task.id == (task.thread_id or task.id) and not thread:
        for t in left:
            t.thread_id = None if t is left[0] else left[0].id
    for each in going:
        if project is not None and project.kind != "local":
            await workspace.discard(project, each.id)
        await session.execute(delete(CodingTaskEvent).where(CodingTaskEvent.task_id == each.id))
        await session.delete(each)
    await session.commit()
    return {"removed": sorted(removed) if thread else task_id}


class RestartIn(BaseModel):
    """Another model to go on with: the one the task had may have been removed since."""
    model: str | None = Field(default=None, max_length=256)


async def _switch_model(task, principal, payload):
    """Puts the task on the model asked for, if it is another one and may be used. Nothing is saved
    until the caller commits, so a refusal leaves the task as it was."""
    wanted = (payload.model or "").strip() if payload else ""
    if not wanted or wanted == task.model:
        return None
    try:
        await runner.check_model(principal, wanted)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    was, task.model = task.model, wanted
    return f"on {wanted} (was {was})"


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: int, payload: RestartIn | None = None, principal: Principal = Depends(require_manager),
                     session: AsyncSession = Depends(get_session)):
    task, project = await _task(session, task_id)
    if task.status not in ("failed", "cancelled", "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status} task cannot be retried")
    # In a folder on somebody's computer the first try's changes are still there; starting again on
    # top of them would leave Undo unable to put the folder back as it was before either
    kind = project.kind if project is not None else None      # None: a presentation without a project
    if kind == "local" and task.originals and task.originals != "{}" and task.status != "rejected":
        raise HTTPException(status.HTTP_409_CONFLICT, "Its earlier changes are still in the folder. Undo them "
                                                      "first, then retry.")
    switched = await _switch_model(task, principal, payload)
    task.status, task.stage, task.error, task.pr_url, task.state = "queued", None, None, None, None
    task.branch = None if kind == "git" else task.branch
    if kind == "local":
        task.originals, task.diff = None, None
    await session.commit()
    await runner.event(task_id, "note", "Retrying from the start" + (f", {switched}" if switched else ""))
    runner.submit(task_id)
    return {"status": "queued"}


@router.post("/tasks/{task_id}/continue")
async def continue_task(task_id: int, payload: RestartIn | None = None, principal: Principal = Depends(require_manager),
                        session: AsyncSession = Depends(get_session)):
    """Picks a stopped task up where it stopped: same conversation, same files, same branch, with the
    reason it stopped told to the model. Retry is the one that starts again from nothing."""
    task, _project = await _task(session, task_id)
    if not _can_continue(task):
        raise HTTPException(status.HTTP_409_CONFLICT, "This task has nothing saved to continue from; use Retry")
    switched = await _switch_model(task, principal, payload)
    state = json.loads(task.state)
    state["resume"] = True
    task.status, task.stage, task.error, task.state = "queued", None, None, json.dumps(state)
    await session.commit()
    await runner.event(task_id, "note", "Continuing from where it stopped" + (f", {switched}" if switched else ""))
    runner.submit(task_id)
    return {"status": "queued"}
