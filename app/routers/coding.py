"""Coding tasks: projects the gateway may change, and the work it does on them.

Managers and admins add projects and start tasks. Only an admin approves a finished task - which
merges it and runs the project's deploy command on this server - and only an admin sets that
command, since it is code this server runs.
"""
import asyncio
import json
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import access, backends
from app.auth import Principal, require_admin, require_manager
from app.coding import runner, workspace
from app.db import get_session
from app.models import CodingTask, CodingTaskEvent, Project

router = APIRouter(prefix="/coding", tags=["coding"])

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,200}$")


def _enabled():
    if not runner.is_enabled():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Coding tasks are switched off (coding.enabled)")


def _project_out(project):
    return {"id": project.id, "name": project.name, "clone_url": project.clone_url,
            "base_branch": project.base_branch, "verify_command": project.verify_command,
            "sandbox_image": project.sandbox_image, "sandbox_network": project.sandbox_network,
            "deploy_command": project.deploy_command, "notes": project.notes,
            "on_github": workspace.github_of(project.clone_url) is not None,
            "can_push": bool(workspace.token_for(project.clone_url)) or not workspace.github_of(project.clone_url)}


def _task_out(task, project=None, full=False):
    out = {"id": task.id, "project_id": task.project_id, "project": project.name if project else None,
           "description": task.description, "model": task.model, "status": task.status, "stage": task.stage,
           "branch": task.branch, "pr_url": task.pr_url, "error": task.error,
           "created_at": task.created_at.isoformat(), "updated_at": task.updated_at.isoformat()}
    if full:
        out.update(plan=task.plan, summary=task.summary, diff=task.diff, verify_output=task.verify_output)
    return out


# --- projects ----------------------------------------------------------------------------------


class ProjectIn(BaseModel):
    clone_url: str = Field(min_length=3, max_length=512)
    base_branch: str | None = Field(default=None, max_length=128)
    verify_command: str = Field(default="", max_length=4000)
    sandbox_image: str = Field(default="python:3.12-slim", max_length=256)
    sandbox_network: bool = False
    deploy_command: str | None = Field(default=None, max_length=4000)
    notes: str | None = Field(default=None, max_length=8000)


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
async def list_projects(_: Principal = Depends(require_manager), session: AsyncSession = Depends(get_session)):
    projects = (await session.execute(select(Project).order_by(Project.name))).scalars().all()
    return {"projects": [_project_out(p) for p in projects]}


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
    if not IMAGE.match(payload.sandbox_image):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a container image name")
    name = _project_name(url)
    if (await session.execute(select(Project).where(Project.name == name))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, f"{name} is already a project")
    try:
        default_branch = await workspace.clone(name, url)
    except workspace.GitError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Could not clone it: {e}")
    project = Project(name=name, clone_url=url, base_branch=(payload.base_branch or default_branch).strip(),
                      verify_command=payload.verify_command.strip(), sandbox_image=payload.sandbox_image,
                      sandbox_network=payload.sandbox_network, deploy_command=(payload.deploy_command or None),
                      notes=payload.notes, added_by=principal.user.id if principal.user else None)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return _project_out(project)


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
        setattr(project, key, value.strip() if isinstance(value, str) else value)
    if project.deploy_command == "":
        project.deploy_command = None
    await session.commit()
    return _project_out(project)


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
    if principal.user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Start tasks while signed in, not with the master key")
    project = await session.get(Project, payload.project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such project")
    if not access.can_use_model(principal, payload.model):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"You do not have access to model '{payload.model}'")
    backend = await backends.resolve(payload.model)
    if backend is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Model '{payload.model}' is not available")
    if "tools" not in backend.capabilities:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{payload.model} cannot call tools; pick one that can")
    task = CodingTask(project_id=project.id, user_id=principal.user.id, conversation_id=payload.conversation_id,
                      description=payload.description.strip(), model=payload.model)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    await runner.event(task.id, "note", f"Queued by {principal.user.email or principal.user.name}")
    runner.submit(task.id)
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


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: int, principal: Principal = Depends(require_admin),
                       session: AsyncSession = Depends(get_session)):
    task, _ = await _task(session, task_id)
    if task.status != "review":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Only a task waiting for review can be approved; this one is {task.status}")
    if principal.user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Approve while signed in, so it is known who did")
    task.status = "approved"
    await session.commit()
    asyncio.create_task(runner.approve(task_id, principal.user))
    return {"status": "approved"}


@router.post("/tasks/{task_id}/reject")
async def reject_task(task_id: int, principal: Principal = Depends(require_manager),
                      session: AsyncSession = Depends(get_session)):
    task, _ = await _task(session, task_id)
    if task.status not in ("review", "failed"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status} task cannot be rejected")
    await runner.reject(task_id, principal.user or principal)
    return {"status": "rejected"}


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
    task, _project = await _task(session, task_id)
    if task.status not in ("failed", "cancelled", "rejected"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"A {task.status} task cannot be retried")
    task.status, task.stage, task.error, task.pr_url = "queued", None, None, None
    await session.commit()
    await runner.event(task_id, "note", "Retrying from the start")
    runner.submit(task_id)
    return {"status": "queued"}
