"""Your projects: the GitHub repositories a person connects, so their coding agents can read across them.

Anyone with an account connects GitHub users or organisations - with a read-only token for private
repositories, or none for public ones - and their repositories are indexed for them alone (app/knowledge.py,
pgvector). Their agents then search and read them through the gateway's MCP server (app/routers/mcp_server.py),
and keep notes there about how the projects fit together (project notes, below and in the MCP server).
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github_access, knowledge, secretbox
from app.auth import Principal, authenticate
from app.db import get_session
from app.models import GitHubConnection, ProjectNote

log = logging.getLogger("projects")

router = APIRouter(prefix="/projects", tags=["projects"])

MAX_CONNECTIONS = 5          # GitHub users and organisations per person


def _user(principal):
    if principal.user is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Projects belong to a user; the master key has none")
    return principal.user


def _connection_json(c):
    return {"id": c.id, "owner": c.owner, "has_token": bool(c.token_sealed), "login": c.login,
            "repositories": c.repositories, "private": c.private, "can_push": c.can_push,
            "created_at": c.created_at.isoformat()}


@router.get("")
async def my_projects(principal: Principal = Depends(authenticate), session: AsyncSession = Depends(get_session)):
    """Your GitHub connections, what is indexed of them, and how indexing is going."""
    user = _user(principal)
    connections = (await session.execute(select(GitHubConnection).where(
        GitHubConnection.user_id == user.id).order_by(GitHubConnection.id))).scalars()
    try:
        sources = await knowledge.list_sources(user.id, include_global=False)
    except Exception:            # the index's tables appear with the first indexing run
        sources = []
    running = knowledge.progress.get("source") or ""
    return {"enabled": knowledge.is_enabled(),
            "connections": [_connection_json(c) for c in connections],
            "projects": [{"name": knowledge.shown(s["name"]), "description": s["description"],
                          "files": s["files"], "passages": s["chunks"], "indexed_at": s["indexed_at"],
                          "error": s["error"]} for s in sources],
            "indexing": bool(knowledge.progress.get("running")) and running.startswith(f"{knowledge.USER_PREFIX}{user.id}/"),
            "limits": {"connections": MAX_CONNECTIONS, "repositories": knowledge.USER_MAX_REPOS,
                       "repository_mb": knowledge.USER_MAX_REPO_MB}}


class ConnectIn(BaseModel):
    owner: str = Field(min_length=1, max_length=100)
    token: str = Field(default="", max_length=300)


@router.post("/github", status_code=status.HTTP_201_CREATED)
async def connect_github(payload: ConnectIn, principal: Principal = Depends(authenticate),
                         session: AsyncSession = Depends(get_session)):
    """Adds (or replaces the token of) a GitHub user or organisation, checked against GitHub first."""
    user = _user(principal)
    if not knowledge.is_enabled():
        raise HTTPException(status.HTTP_409_CONFLICT, "The knowledge base is switched off on this gateway")
    owner, token = payload.owner.strip(), payload.token.strip()
    if not github_access.OWNER.match(owner):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is not a GitHub user or organisation name")
    existing = (await session.execute(select(GitHubConnection).where(
        GitHubConnection.user_id == user.id, GitHubConnection.owner == owner))).scalar_one_or_none()
    if existing is None:
        count = len((await session.execute(select(GitHubConnection.id).where(
            GitHubConnection.user_id == user.id))).all())
        if count >= MAX_CONNECTIONS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"At most {MAX_CONNECTIONS} GitHub users or "
                                                             "organisations each; remove one first")
    found = {"login": None, "repositories": 0, "private": 0, "can_push": False}
    if token:
        if not github_access.TOKEN.match(token):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That does not look like a GitHub token (github_pat_... or ghp_...)")
        try:
            found = await github_access.check(owner, token)
        except (ValueError, httpx.HTTPError) as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
        if not found["repositories"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"The token works (it belongs to {found['login']}) but sees none of {owner}'s "
                                "repositories. Give it access to them, with Contents: read-only.")
    else:
        # Public repositories only: the name has to exist, and the listing needs no token
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"https://api.github.com/users/{owner}",
                                        headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"GitHub has no user or organisation called {owner}")
        if response.status_code >= 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"GitHub answered {response.status_code}")
        found["repositories"] = response.json().get("public_repos") or 0
    connection = existing or GitHubConnection(user_id=user.id, owner=owner)
    connection.token_sealed = secretbox.seal(token) if token else None
    connection.login, connection.repositories = found["login"], found["repositories"]
    connection.private, connection.can_push = found["private"], bool(found["can_push"])
    if existing is None:
        session.add(connection)
    await session.commit()
    state = knowledge.request_reindex(f"{knowledge.USER_PREFIX}{user.id}/{owner}/*")
    return {**_connection_json(connection), "indexing": state,
            **({"warning": "This token can also push to these repositories. The gateway only reads them; "
                           "a token with Contents: read-only would be safer."} if found["can_push"] else {})}


@router.delete("/github/{connection_id}")
async def disconnect_github(connection_id: int, principal: Principal = Depends(authenticate),
                            session: AsyncSession = Depends(get_session)):
    """Removes a connection, its token, and everything indexed from it."""
    user = _user(principal)
    connection = await session.get(GitHubConnection, connection_id)
    if connection is None or connection.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such connection")
    owner = connection.owner
    await session.delete(connection)
    await session.commit()
    removed = await knowledge.forget(user.id, owner)
    return {"removed_projects": removed}


@router.post("/reindex")
async def reindex_mine(project: str | None = None, principal: Principal = Depends(authenticate)):
    """Indexes your projects again now (or one of them), for changes pushed since the last run."""
    user = _user(principal)
    pattern = f"{knowledge.USER_PREFIX}{user.id}/{project or '*'}"
    return {"indexing": knowledge.request_reindex(pattern)}


# --- project notes, as the person sees them ------------------------------------------------------

@router.get("/notes")
async def my_notes(project: str | None = None, principal: Principal = Depends(authenticate),
                   session: AsyncSession = Depends(get_session)):
    user = _user(principal)
    query = select(ProjectNote).where(ProjectNote.user_id == user.id)
    if project:
        query = query.where(ProjectNote.project == project)
    notes = (await session.execute(query.order_by(ProjectNote.project, ProjectNote.id))).scalars()
    return [{"id": n.id, "project": n.project, "content": n.content, "updated_at": n.updated_at.isoformat()}
            for n in notes]


@router.delete("/notes/{note_id}")
async def delete_note(note_id: int, principal: Principal = Depends(authenticate),
                      session: AsyncSession = Depends(get_session)):
    user = _user(principal)
    note = await session.get(ProjectNote, note_id)
    if note is None or note.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such note")
    await session.delete(note)
    await session.commit()
    return {"deleted": note_id}
