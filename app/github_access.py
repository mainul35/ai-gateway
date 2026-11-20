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


class Taken(ValueError):
    """The owner has a token already, and replacing it was not asked for."""


def saved(owner):
    """The owner's name as a token is saved under, whatever the case it is typed in; None if none is."""
    for name in config.properties():
        if name.startswith("github.token.") and name[len("github.token."):].lower() == owner.lower():
            return name[len("github.token."):]
    return None


async def check(owner, value):
    """What a token can do on an owner's repositories, and whose repositories it sees at all
    ("owners"). With no owner, only the second. Raises ValueError when GitHub refuses the token."""
    headers = {"Authorization": f"Bearer {value}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=20) as client:
        me = await client.get("https://api.github.com/user", headers=headers)
        if me.status_code == 401:
            raise ValueError("GitHub does not accept this token (expired, revoked or mistyped)")
        if me.status_code >= 400:
            raise ValueError(f"GitHub answered {me.status_code}: {me.text[:200]}")
        login = me.json().get("login")
        # A fine-grained token lists only the repositories it was given, so their owners are the token's
        repos = await client.get("https://api.github.com/user/repos", headers=headers,
                                 params={"per_page": 100,
                                         "affiliation": "owner,collaborator,organization_member"})
        seen = repos.json() if repos.status_code < 400 else []
        if owner and not any(_owner_of(r).lower() == owner.lower() for r in seen):
            # An organisation's repositories can be missing from the person's own list
            org = await client.get(f"https://api.github.com/orgs/{owner}/repos", headers=headers,
                                   params={"per_page": 100, "type": "all"})
            seen += org.json() if org.status_code < 400 else []
    owners = sorted({_owner_of(r) for r in seen if _owner_of(r)}, key=str.lower)
    listed = [r for r in seen if owner and _owner_of(r).lower() == owner.lower()]
    return {"login": login, "owners": owners, "repositories": len(listed),
            "can_push": any((r.get("permissions") or {}).get("push") for r in listed),
            "private": sum(1 for r in listed if r.get("private"))}


def _owner_of(repo):
    return (repo.get("owner") or {}).get("login") or ""


async def save(owner, value, replace=False):
    """Saves a token under its owner. With no owner, the owner is the one whose repositories the
    token sees. An owner that has a token already keeps it unless replace is asked for (Taken)."""
    owner = (owner or "").strip()
    if owner and not OWNER.match(owner):
        raise ValueError("That is not a GitHub user or organisation name")
    value = (value or "").strip()
    if not TOKEN.match(value):
        raise ValueError("That does not look like a GitHub token (github_pat_... or ghp_...)")
    found = await check(owner, value)
    owners = found["owners"]
    if not owner:
        if len(owners) != 1:
            raise ValueError(
                f"The token works (it belongs to {found['login']}) but sees no repositories. Give it access "
                f"to them, with Contents read and write." if not owners else
                f"The token sees the repositories of {', '.join(owners)}. Write which one it is for as the Owner.")
        owner = owners[0]
        found = await check(owner, value)
    elif not found["repositories"]:
        others = [o for o in owners if o.lower() != owner.lower()]
        raise ValueError(
            f"The token works (it belongs to {found['login']}) but sees none of {owner}'s repositories"
            + (f": it is for {', '.join(others)}. Write that as the Owner, or leave the Owner empty."
               if others else ". Give it access to them, with Contents read and write."))
    else:
        # Saved under the name GitHub gives, so "Acme" and "acme" are one owner, not two
        owner = next((o for o in owners if o.lower() == owner.lower()), owner)
    existing = saved(owner)
    if existing and not replace:
        raise Taken(f"{existing} already has a token ({masked(settings.get(key(existing)) or '')}). Press Replace "
                    f"on its row to swap it. A token for another organisation goes under that "
                    f"organisation's name, or leave the Owner empty to find it from the token.")
    if existing and existing != owner:
        config_writer.remove_key(key(existing))
    config_writer.write_secret(key(owner), value)
    return {"owner": owner, **found}


def remove(owner):
    if not OWNER.match(owner or ""):
        raise ValueError("That is not a GitHub user or organisation name")
    config_writer.remove_key(key(owner))
