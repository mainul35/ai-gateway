"""Coding tasks: projects the gateway may change, and the work it does on them.

Managers and admins add projects and start tasks. Only an admin approves a finished task - which
merges it and runs the project's deploy command on this server - and only an admin sets that
command, since it is code this server runs.
"""
import asyncio
import json
import re

import httpx

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github_access, knowledge
from app.auth import Principal, require_admin, require_manager
from app.coding import detect, remote, runner, workspace
from app.db import get_session
from app.models import CodingTask, CodingTaskEvent, Project

router = APIRouter(prefix="/coding", tags=["coding"])

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
        out["can_push"] = bool(workspace.token_for(project.clone_url)) or not out["on_github"]
    if detected:
        out["detected"] = detected
    return out


def _task_out(task, project=None, full=False):
    out = {"id": task.id, "project_id": task.project_id, "project": project.name if project else None,
           "description": task.description, "model": task.model, "status": task.status, "stage": task.stage,
           "branch": task.branch, "pr_url": task.pr_url, "error": task.error,
           "created_at": task.created_at.isoformat(), "updated_at": task.updated_at.isoformat()}
    out["can_continue"] = _can_continue(task)
    if full:
        out.update(plan=task.plan, summary=task.summary, diff=task.diff, verify_output=task.verify_output)
    return out


def _can_continue(task):
    """Stopped part-way with its conversation saved, in a stage it can be picked up from."""
    if task.status not in ("failed", "cancelled") or not task.state:
        return False
    try:
        state = json.loads(task.state)
    except ValueError:
        return False
    return bool(state.get("messages")) and state.get("stage") in ("explore", "edit", "ship")


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


def _project_name(clone_url):
    found = workspace.github_of(clone_url)
    if found:
        return f"{found[0]}/{found[1]}"
    parts = [p for p in re.split(r"[/:]", clone_url.rstrip("/").removesuffix(".git")) if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


@router.get("/projects")
async def list_projects(principal: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    projects = (await session.execute(select(Project).order_by(Project.name))).scalars().all()
    return {"projects": [_project_out(p, principal) for p in projects]}


@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def add_project(payload: ProjectIn, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    _enabled()
    url = payload.clone_url.strip()
    if not workspace.github_of(url) and not principal.is_admin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Give the project's GitHub address, "
                                                          "e.g. https://github.com/owner/repo")
    if payload.deploy_command and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can set a deploy command")
    if payload.sandbox_image and not IMAGE.match(payload.sandbox_image):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a container image name")
    name = _project_name(url)
    if (await session.execute(select(Project).where(Project.name == name))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, f"{name} is already a project")
    try:
        default_branch = await workspace.clone(name, url)
    except workspace.GitError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not clone it: {e}")
    found = detect.detect(detect.LocalFolder(workspace.clone_path(name)))
    project = Project(name=name, kind="git", clone_url=url,
                      base_branch=(payload.base_branch or default_branch).strip(),
                      verify_command=(payload.verify_command if payload.verify_command is not None
                                      else found["verify"]).strip(),
                      sandbox_image=payload.sandbox_image or found["image"],
                      sandbox_network=found["network"] if payload.sandbox_network is None else payload.sandbox_network,
                      deploy_command=(payload.deploy_command or None),
                      notes=payload.notes, added_by=principal.user.id if principal.user else None)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return _project_out(project, principal, detected=found["kind"])


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


@router.put("/github/{owner}")
async def set_github_token(owner: str, payload: TokenIn, _: Principal = Depends(require_admin)):
    """Checks the token with GitHub, then saves it. Never echoed back."""
    try:
        found = await github_access.save(owner, payload.token)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"GitHub could not be reached: {e}")
    return {"owner": owner, **found}


@router.post("/github/{owner}/check")
async def check_github_token(owner: str, _: Principal = Depends(require_admin)):
    value = github_access.token(owner, "write") or github_access.token(owner, "read")
    if not value:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No token for {owner}")
    try:
        return {"owner": owner, **await github_access.check(owner, value)}
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"GitHub could not be reached: {e}")


@router.delete("/github/{owner}")
async def remove_github_token(owner: str, _: Principal = Depends(require_admin)):
    try:
        github_access.remove(owner)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"removed": owner}


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
    changes = payload.model_dump(exclude_unset=True)
    if "deploy_command" in changes and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can set a deploy command")
    if "sandbox_image" in changes and not IMAGE.match(changes["sandbox_image"] or ""):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a container image name")
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
    project_id: int
    description: str = Field(min_length=5, max_length=20_000)
    model: str = Field(max_length=256)
    conversation_id: int | None = None


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskIn, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    _enabled()
    project = await session.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    try:
        task = await runner.create(session, principal, project, payload.description, payload.model,
                                   payload.conversation_id)
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _task_out(task, project)


@router.get("/tasks")
async def list_tasks(project_id: int | None = None, limit: int = 50, _: Principal = Depends(require_manager),
                     session: AsyncSession = Depends(get_session)):
    query = select(CodingTask, Project).join(Project).order_by(CodingTask.id.desc()).limit(max(1, min(limit, 200)))
    if project_id:
        query = query.where(CodingTask.project_id == project_id)
    rows = (await session.execute(query)).all()
    return {"tasks": [_task_out(t, p) for t, p in rows]}


async def _task(session, task_id):
    task = await session.get(CodingTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such task")
    return task, await session.get(Project, task.project_id)


@router.get("/tasks/{task_id}")
async def get_task(task_id: int, _: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    task, project = await _task(session, task_id)
    return _task_out(task, project, full=True)


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


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: int, _: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    task, project = await _task(session, task_id)
    if task.status not in ("failed", "cancelled", "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status} task cannot be retried")
    # In a folder on somebody's computer the first try's changes are still there; starting again on
    # top of them would leave Undo unable to put the folder back as it was before either
    if project.kind == "local" and task.originals and task.originals != "{}" and task.status != "rejected":
        raise HTTPException(status.HTTP_409_CONFLICT, "Its earlier changes are still in the folder. Undo them "
                                                      "first, then retry.")
    task.status, task.stage, task.error, task.pr_url, task.state = "queued", None, None, None, None
    task.branch = None if project.kind == "git" else task.branch
    if project.kind == "local":
        task.originals, task.diff = None, None
    await session.commit()
    await runner.event(task_id, "note", "Retrying from the start")
    runner.submit(task_id)
    return {"status": "queued"}


@router.post("/tasks/{task_id}/continue")
async def continue_task(task_id: int, _: Principal = Depends(require_manager),
                        session: AsyncSession = Depends(get_session)):
    """Picks a stopped task up where it stopped: same conversation, same files, same branch, with the
    reason it stopped told to the model. Retry is the one that starts again from nothing."""
    task, _project = await _task(session, task_id)
    if not _can_continue(task):
        raise HTTPException(status.HTTP_409_CONFLICT, "This task has nothing saved to continue from; use Retry")
    state = json.loads(task.state)
    state["resume"] = True
    task.status, task.stage, task.error, task.state = "queued", None, None, json.dumps(state)
    await session.commit()
    await runner.event(task_id, "note", "Continuing from where it stopped")
    runner.submit(task_id)
    return {"status": "queued"}
