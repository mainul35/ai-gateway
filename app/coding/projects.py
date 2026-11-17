"""Adding a GitHub repository as a coding project: the one way it is done, from the Tasks page or a chat.

Only the address is needed. The clone is made, and how the project is checked (its verify command and
sandbox image) is worked out from its files; anything the caller gives overrides that. The rules are
the Tasks page's: a manager may add a repository only from an owner the gateway is meant to work on
(allowed_owners), only an admin may give a project the network or a deploy command.
"""
import re

from sqlalchemy import select

from app import github_access, settings
from app.coding import detect, workspace
from app.models import Project

IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,200}$")


class ProjectError(Exception):
    """Why a project was not added, with the HTTP status the Tasks page answers it with."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def allowed_owners():
    """The GitHub owners a manager may add projects from: coding.github.owners, plus every owner the
    gateway holds a token for (those are the repositories it is meant to work on). Admins may add any."""
    listed = (settings.get("coding.github.owners", "CODING_GITHUB_OWNERS") or "").replace(",", " ").split()
    return {o.lower() for o in listed} | {e["owner"].lower() for e in github_access.owners()}


def name_of(clone_url):
    found = workspace.github_of(clone_url)
    if found:
        return f"{found[0]}/{found[1]}"
    parts = [p for p in re.split(r"[/:]", clone_url.rstrip("/").removesuffix(".git")) if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]


async def add_git(session, principal, clone_url, base_branch=None, verify_command=None, sandbox_image=None,
                  sandbox_network=None, deploy_command=None, notes=None):
    """Clones the repository and saves it as a project. Returns (project, what kind of project it
    looked like). Raises ProjectError."""
    url = clone_url.strip()
    found_github = workspace.github_of(url)
    if not found_github and not principal.is_admin:
        raise ProjectError(400, "Give the project's GitHub address, e.g. https://github.com/owner/repo")
    if not principal.is_admin and found_github[0].lower() not in allowed_owners():
        raise ProjectError(403, f"Projects from {found_github[0]} are not allowed here; an admin can add them, "
                                "or allow the owner (coding.github.owners)")
    if sandbox_network is not None and not principal.is_admin:
        raise ProjectError(403, "Only an admin can choose whether a project gets a network")
    if base_branch and not workspace.valid_branch(base_branch.strip()):
        raise ProjectError(400, f"{base_branch.strip()!r} is not a branch name")
    if deploy_command and not principal.is_admin:
        raise ProjectError(403, "Only an admin can set a deploy command")
    if sandbox_image and not IMAGE.match(sandbox_image):
        raise ProjectError(400, "That is not a container image name")
    name = name_of(url)
    if (await session.execute(select(Project).where(Project.name == name))).scalar_one_or_none():
        raise ProjectError(409, f"{name} is already a project")
    try:
        workspace.clone_path(name)
        default_branch = await workspace.clone(name, url)
    except workspace.GitError as e:
        raise ProjectError(400, f"Could not clone it: {e}")
    branch = (base_branch or default_branch).strip()
    if not workspace.valid_branch(branch):
        raise ProjectError(400, f"{branch!r} is not a branch name")
    found = detect.detect(detect.LocalFolder(workspace.clone_path(name)))
    project = Project(name=name, kind="git", clone_url=url, base_branch=branch,
                      verify_command=(verify_command if verify_command is not None else found["verify"]).strip(),
                      sandbox_image=sandbox_image or found["image"],
                      sandbox_network=found["network"] if sandbox_network is None else sandbox_network,
                      deploy_command=(deploy_command or None),
                      notes=notes, added_by=principal.user.id if principal.user else None)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project, found["kind"]
