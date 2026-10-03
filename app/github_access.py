"""GitHub tokens, one per owner, set from the gateway rather than by editing a file on the server.

A fine-grained personal access token belongs to one owner - a user or an organisation - so there is
one per owner, kept in config/config.properties (not in the repository) as github.token.<owner>.
The knowledge base reads repositories with it and coding tasks push branches and open pull requests
with it. The older, separate settings (knowledge.github.token.<owner>, coding.github.token.<owner>)
still work where no token is set here, so nothing configured before stops working.

A token is tried against GitHub before it is saved, and what it can do is shown: whose it is, how
many of the owner's repositories it can read, and whether it can push to them. It is never sent
back in full - only its first and last few characters, enough to tell two apart.
"""
import re

import httpx

from app import config_writer, settings
from utils import config

OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TOKEN = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})$")
PREFIXES = ("github.token.", "knowledge.github.token.", "coding.github.token.")


def key(owner):
    return f"github.token.{owner}"


def token(owner, purpose="read"):
    """The token to use for an owner: "read" for the knowledge base, "write" for coding tasks."""
    specific = "knowledge" if purpose == "read" else "coding"
    # The one set in the gateway first: it is the one somebody can see and replace
    return (settings.get(key(owner)) or settings.get(f"{specific}.github.token.{owner}")
            or settings.get(f"{specific}.github.token") or "")


def masked(value):
    return f"{value[:11]}…{value[-4:]}" if len(value) > 20 else "…" + value[-4:]


def owners():
    """Every owner with a token of any kind, and which ones."""
    found = {}
    for name, value in config.properties().items():
        for prefix in PREFIXES:
            if name.startswith(prefix) and value:
                owner = name[len(prefix):]
                entry = found.setdefault(owner, {"owner": owner})
                entry["token" if prefix == "github.token." else prefix.split(".")[0]] = masked(value)
    return sorted(found.values(), key=lambda e: e["owner"].lower())


async def check(owner, value):
    """What a token can do on an owner's repositories. Raises ValueError when GitHub refuses it."""
    headers = {"Authorization": f"Bearer {value}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=20) as client:
        me = await client.get("https://api.github.com/user", headers=headers)
        if me.status_code == 401:
            raise ValueError("GitHub does not accept this token (expired, revoked or mistyped)")
        if me.status_code >= 400:
            raise ValueError(f"GitHub answered {me.status_code}: {me.text[:200]}")
        login = me.json().get("login")
        repos = await client.get(f"https://api.github.com/orgs/{owner}/repos", headers=headers,
                                 params={"per_page": 100, "type": "all"})
        if repos.status_code == 404:
            repos = await client.get("https://api.github.com/user/repos", headers=headers,
                                     params={"per_page": 100, "affiliation": "owner,organization_member"})
        listed = [r for r in (repos.json() if repos.status_code < 400 else [])
                  if r.get("owner", {}).get("login", "").lower() == owner.lower()]
    return {"login": login, "repositories": len(listed),
            "can_push": any((r.get("permissions") or {}).get("push") for r in listed),
            "private": sum(1 for r in listed if r.get("private"))}


async def save(owner, value):
    if not OWNER.match(owner or ""):
        raise ValueError("That is not a GitHub user or organisation name")
    value = (value or "").strip()
    if not TOKEN.match(value):
        raise ValueError("That does not look like a GitHub token (github_pat_... or ghp_...)")
    found = await check(owner, value)
    if not found["repositories"]:
        raise ValueError(f"The token works (it belongs to {found['login']}) but sees none of {owner}'s "
                         f"repositories. Give it access to them, with Contents read and write.")
    config_writer.write_secret(key(owner), value)
    return found


def remove(owner):
    if not OWNER.match(owner or ""):
        raise ValueError("That is not a GitHub user or organisation name")
    config_writer.remove_key(key(owner))
