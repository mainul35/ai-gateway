"""Git for coding tasks: a clone per project, a worktree and a branch per task, and GitHub around it.

Each project is cloned once under coding.projects.dir (owner/repo). Each task gets its own git
worktree on its own branch, cut from the project's base branch as it is on GitHub right now, so tasks
never see each other's half-done work and the clone itself is never edited.

A GitHub token is passed to git as an HTTP header in the environment of that one command - never in
a URL, a config file or the command line, where it would be kept or shown. Tokens are separate from
the knowledge base's read-only ones: coding.github.token.<owner> needs Contents and Pull requests,
read and write, on the repositories the gateway may change.
"""
import asyncio
import base64
import logging
import os
import re

import httpx

from app import knowledge, settings

log = logging.getLogger("coding.workspace")

GITHUB = re.compile(r"^(?:https://github\.com/|git@github\.com:)(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")
AUTHOR_NAME = "AI Gateway"


class GitError(Exception):
    pass


def projects_dir():
    return os.path.expanduser(settings.get("coding.projects.dir", "CODING_PROJECTS_DIR") or "~/projects")


def github_of(clone_url):
    """(owner, repo) for a GitHub URL, else None."""
    match = GITHUB.match((clone_url or "").strip())
    return (match["owner"], match["repo"]) if match else None


def token_for(clone_url):
    found = github_of(clone_url)
    if not found:
        return ""
    return (settings.get(f"coding.github.token.{found[0]}")
            or settings.get("coding.github.token", "CODING_GITHUB_TOKEN") or "")


def clone_path(name):
    return os.path.join(projects_dir(), *name.split("/"))


def worktree_path(task_id):
    return os.path.join(projects_dir(), ".tasks", f"task-{task_id}")


async def git(args, cwd=None, token="", timeout=600):
    """Runs git and returns its output; GitError with the end of stderr when it fails."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise GitError(f"git {args[0]} took longer than {timeout}s")
    if process.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {err.decode(errors='replace').strip()[-600:]}")
    return out.decode(errors="replace")


# --- projects ----------------------------------------------------------------------------------


async def clone(name, clone_url):
    """Clones a project if it is not here yet. Returns its default branch."""
    path = clone_path(name)
    token = token_for(clone_url)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        await git(["clone", "--quiet", "--filter=blob:none", clone_url, path], token=token)
    else:
        await git(["fetch", "--quiet", "--prune", "origin"], cwd=path, token=token)
    head = (await git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=path)).strip()
    return head.split("/", 1)[1] if "/" in head else head or "main"


# --- a task's worktree -------------------------------------------------------------------------


def branch_name(task_id, description):
    slug = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")[:40].strip("-") or "change"
    return f"agent/task-{task_id}-{slug}"


async def start(project, task_id, branch):
    """A fresh worktree on a new branch from the base branch as GitHub has it now."""
    path, root = worktree_path(task_id), clone_path(project.name)
    await git(["fetch", "--quiet", "origin", project.base_branch], cwd=root, token=token_for(project.clone_url))
    if os.path.isdir(path):
        await discard(project, task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    await git(["worktree", "add", "--quiet", "-B", branch, path, f"origin/{project.base_branch}"], cwd=root)
    return path


async def changes(path, base_branch):
    """(stat, diff) of everything the task has changed, new files included."""
    await git(["add", "-A"], cwd=path)
    stat = await git(["diff", "--cached", "--stat", f"origin/{base_branch}"], cwd=path)
    diff = await git(["diff", "--cached", f"origin/{base_branch}"], cwd=path)
    return stat.strip(), diff


def secrets_added(diff):
    """Added lines that look like credentials, as (file, line) - checked before anything is pushed."""
    found, current = [], ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++") and knowledge.looks_secret(current, line[1:]):
            found.append((current, line[1:80]))
    return found


async def commit(path, message, email):
    await git(["add", "-A"], cwd=path)
    await git(["-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={email}",
               "commit", "--quiet", "-m", message], cwd=path)
    return (await git(["rev-parse", "HEAD"], cwd=path)).strip()


async def push(project, path, branch):
    await git(["push", "--quiet", "--force", "origin", f"HEAD:refs/heads/{branch}"],
              cwd=path, token=token_for(project.clone_url))


async def discard(project, task_id):
    """Removes a task's worktree; the branch stays, on GitHub and here, until it is merged."""
    path = worktree_path(task_id)
    try:
        await git(["worktree", "remove", "--force", path], cwd=clone_path(project.name))
    except GitError:
        pass
    if os.path.isdir(path):
        await asyncio.to_thread(_rmtree, path)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


# --- GitHub ------------------------------------------------------------------------------------


async def _github(method, path, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, f"https://api.github.com{path}", headers=headers, **kwargs)
    if response.status_code >= 400:
        raise GitError(f"GitHub said {response.status_code}: {response.text[:300]}")
    return response.json() if response.content else {}


async def open_pull_request(project, branch, title, body):
    """Opens a pull request into the base branch. Returns (url, number), or (None, None) when the
    project is not on GitHub."""
    found = github_of(project.clone_url)
    if not found:
        return None, None
    token = token_for(project.clone_url)
    if not token:
        raise GitError(f"no GitHub token to open a pull request on {found[0]}: set "
                       f"coding.github.token.{found[0]} in config.properties")
    owner, repo = found
    existing = await _github("GET", f"/repos/{owner}/{repo}/pulls", token,
                             params={"head": f"{owner}:{branch}", "state": "open"})
    if existing:
        return existing[0]["html_url"], existing[0]["number"]
    pull = await _github("POST", f"/repos/{owner}/{repo}/pulls", token,
                         json={"title": title[:200], "head": branch, "base": project.base_branch, "body": body})
    return pull["html_url"], pull["number"]


async def merge_pull_request(project, number, message):
    """Merges with a merge commit - the --no-ff history the projects are kept in."""
    owner, repo = github_of(project.clone_url)
    await _github("PUT", f"/repos/{owner}/{repo}/pulls/{number}/merge", token_for(project.clone_url),
                  json={"merge_method": "merge", "commit_title": message[:200]})


async def merge_locally(project, branch, message, email):
    """For a project that is not on GitHub: merge --no-ff into the base branch and push it."""
    root = clone_path(project.name)
    await git(["checkout", "--quiet", project.base_branch], cwd=root)
    await git(["reset", "--quiet", "--hard", f"origin/{project.base_branch}"], cwd=root)
    await git(["-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={email}",
               "merge", "--quiet", "--no-ff", "-m", message, branch], cwd=root)
    await git(["push", "--quiet", "origin", project.base_branch], cwd=root, token=token_for(project.clone_url))


def pull_number(pr_url):
    match = re.search(r"/pull/(\d+)", pr_url or "")
    return int(match.group(1)) if match else None
