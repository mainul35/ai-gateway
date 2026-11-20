"""GitHub tokens, set from the gateway rather than by editing a file on the server.

A fine-grained personal access token belongs to one owner - a user or an organisation - and reaches
the repositories it was given. An owner can have several: one for some repositories, another for
the rest, or a new one beside one about to expire. They are kept in config/config.properties (not
in the repository) as github.token.<owner> for the first and github.token.<owner>.<n> for the
others; an owner's name cannot hold a dot, so the two never mix. For a repository, the gateway
uses the first of its owner's tokens that can reach it (and, for coding tasks, push to it) - unless
the project names one of them, and then only that one is used: a token made for one project is not
lent to another. Each token can be given a name (github.tokenname.<owner>.<n>, not a secret) so
the person choosing one for a project knows which is which.

The knowledge base reads repositories with them and coding tasks push branches and open pull
requests with them. The older, separate settings (knowledge.github.token.<owner>,
coding.github.token.<owner>) still work after these, so nothing configured before stops working.

A token is tried against GitHub before it is saved, and what it can do is shown: whose it is, how
many of the owner's repositories it can read, and whether it can push to them. It is never sent
back in full - only its first and last few characters, enough to tell two apart.
"""
import hashlib
import re
import time

import httpx

from app import config_writer, settings
from utils import config

OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TOKEN = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})$")
ENTRY = re.compile(r"^github\.token\.([A-Za-z0-9-]+)(?:\.(\d+))?$")
OLDER = ("knowledge.github.token.", "coding.github.token.")
NAME = re.compile(r"^[\w .()-]{1,40}$")
REACH_SECONDS = 600             # how long "this token reaches that repository" is trusted


def key(owner, number=1):
    return f"github.token.{owner}" if int(number) == 1 else f"github.token.{owner}.{int(number)}"


def name_key(owner, number=1):
    return f"github.tokenname.{owner}.{int(number)}"


def masked(value):
    return f"{value[:11]}…{value[-4:]}" if len(value) > 20 else "…" + value[-4:]


def entries(owner=None):
    """The tokens set in the gateway: [{owner, id, value}], an owner's first one first."""
    found = []
    for name, value in config.properties().items():
        match = ENTRY.match(name)
        if match and value and (owner is None or match[1].lower() == owner.lower()):
            number = int(match[2] or 1)
            found.append({"owner": match[1], "id": number, "value": value,
                          "name": settings.get(name_key(match[1], number)) or ""})
    return sorted(found, key=lambda e: (e["owner"].lower(), e["id"]))


def tokens(owner, purpose="read"):
    """Every token that may serve an owner, best first: "read" for the knowledge base, "write" for
    coding tasks. The ones set in the gateway come first: they are the ones somebody can see."""
    specific = "knowledge" if purpose == "read" else "coding"
    found = [e["value"] for e in entries(owner)]
    for older in (settings.get(f"{specific}.github.token.{owner}"), settings.get(f"{specific}.github.token")):
        if older and older not in found:
            found.append(older)
    return found


def token(owner, purpose="read"):
    """An owner's first token, or "": enough to know whether there is one at all."""
    found = tokens(owner, purpose)
    return found[0] if found else ""


_reach = {}         # (token digest, "owner/repo", purpose) -> (reaches, when)


class Missing(ValueError):
    """The token a project names is not there any more."""


def chosen(owner, number):
    """One of an owner's tokens by its id: its entry, or None."""
    return next((e for e in entries(owner) if e["id"] == int(number)), None)


async def repo_token(owner, repo, purpose="read", pinned=None):
    """The token to use on one repository. With pinned (a token's id, set on the project), that one
    and no other. Otherwise the first of the owner's that GitHub lets read it (and, for "write",
    push to it); with one token, or none that is shown to reach it, the first, so git's own error
    says what is wrong."""
    if pinned:
        entry = chosen(owner, pinned)
        if entry is None:
            raise Missing(f"The GitHub token chosen for this project ({owner} #{pinned}) has been removed. "
                          f"Choose another in the project's settings.")
        return entry["value"]
    found = tokens(owner, purpose)
    if len(found) < 2 or not repo:
        return found[0] if found else ""
    for value in found:
        if await _reaches(value, owner, repo, purpose):
            return value
    return found[0]


async def _reaches(value, owner, repo, purpose):
    remembered = (hashlib.sha256(value.encode()).hexdigest(), f"{owner}/{repo}".lower(), purpose)
    known = _reach.get(remembered)
    if known and time.monotonic() - known[1] < REACH_SECONDS:
        return known[0]
    headers = {"Authorization": f"Bearer {value}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            answer = await client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
    except httpx.HTTPError:
        return False                        # not remembered: GitHub may answer next time
    reaches = answer.status_code == 200 and (
        purpose != "write" or bool((answer.json().get("permissions") or {}).get("push")))
    _reach[remembered] = (reaches, time.monotonic())
    return reaches


def owners():
    """Every owner with a token of any kind: its gateway tokens (id and masked value) and which
    older settings it also has."""
    found = {}
    for entry in entries():
        row = found.setdefault(entry["owner"].lower(), {"owner": entry["owner"], "tokens": []})
        row["tokens"].append({"id": entry["id"], "name": entry["name"], "token": masked(entry["value"])})
    for name, value in config.properties().items():
        for prefix in OLDER:
            if name.startswith(prefix) and value:
                owner = name[len(prefix):]
                row = found.setdefault(owner.lower(), {"owner": owner, "tokens": []})
                row[prefix.split(".")[0]] = masked(value)
    return sorted(found.values(), key=lambda e: e["owner"].lower())


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
    owners_seen = sorted({_owner_of(r) for r in seen if _owner_of(r)}, key=str.lower)
    listed = [r for r in seen if owner and _owner_of(r).lower() == owner.lower()]
    return {"login": login, "owners": owners_seen, "repositories": len(listed),
            "can_push": any((r.get("permissions") or {}).get("push") for r in listed),
            "private": sum(1 for r in listed if r.get("private"))}


def _owner_of(repo):
    return (repo.get("owner") or {}).get("login") or ""


def _clean_name(name):
    name = " ".join((name or "").split())
    if name and not NAME.match(name):
        raise ValueError("A token's name can hold letters, digits, spaces and . - ( ), up to 40")
    return name


async def save(owner, value, replace=None, name=None):
    """Adds a token for an owner, beside any it has; with replace (a token's id), swaps that one,
    keeping its name unless another is given. With no owner, the owner is the one whose
    repositories the token sees."""
    owner = (owner or "").strip()
    name = _clean_name(name)
    if owner and not OWNER.match(owner):
        raise ValueError("That is not a GitHub user or organisation name")
    value = (value or "").strip()
    if not TOKEN.match(value):
        raise ValueError("That does not look like a GitHub token (github_pat_... or ghp_...)")
    found = await check(owner, value)
    seen = found["owners"]
    if not owner:
        if len(seen) != 1:
            raise ValueError(
                f"The token works (it belongs to {found['login']}) but sees no repositories. Give it access "
                f"to them, with Contents read and write." if not seen else
                f"The token sees the repositories of {', '.join(seen)}. Write which one it is for as the Owner.")
        owner = seen[0]
        found = await check(owner, value)
    elif not found["repositories"]:
        others = [o for o in seen if o.lower() != owner.lower()]
        raise ValueError(
            f"The token works (it belongs to {found['login']}) but sees none of {owner}'s repositories"
            + (f": it is for {', '.join(others)}. Write that as the Owner, or leave the Owner empty."
               if others else ". Give it access to them, with Contents read and write."))
    else:
        # Saved under the name GitHub gives, so "Acme" and "acme" are one owner, not two
        owner = next((o for o in seen if o.lower() == owner.lower()), owner)
    have = entries(owner)
    same = next((e for e in have if e["value"] == value), None)
    if same and not (replace and same["id"] == int(replace)):
        raise ValueError(f"This token is already saved for {owner}"
                         + (f" as {same['name']}" if same["name"] else f" (#{same['id']})")
                         + ". To add another, make a new token on GitHub and paste that one.")
    if replace:
        swapped = next((e for e in have if e["id"] == int(replace)), None)
        if swapped is None:
            raise ValueError(f"{owner} has no token {replace} to replace; it may have been removed.")
        if swapped["owner"] != owner:              # the same owner, written another way before
            config_writer.remove_key(key(swapped["owner"], swapped["id"]))
            config_writer.remove_key(name_key(swapped["owner"], swapped["id"]))
        number = swapped["id"]
        name = name or swapped["name"]
    else:
        # Under the owner's name as first saved, so its tokens stay together
        owner = have[0]["owner"] if have else owner
        taken = {e["id"] for e in have}
        number = next(n for n in range(1, len(taken) + 2) if n not in taken)
    config_writer.write_secret(key(owner, number), value)
    _write_name(owner, number, name)
    return {"owner": owner, "id": number, "name": name, **found}


def _write_name(owner, number, name):
    if name:
        config_writer.write_secret(name_key(owner, number), name)
    else:
        config_writer.remove_key(name_key(owner, number))


def rename(owner, number, name):
    entry = chosen(owner, number)
    if entry is None:
        raise ValueError(f"{owner} has no token #{number}")
    _write_name(entry["owner"], entry["id"], _clean_name(name))


def remove(owner, number=1):
    if not OWNER.match(owner or ""):
        raise ValueError("That is not a GitHub user or organisation name")
    config_writer.remove_key(key(owner, number))
    config_writer.remove_key(name_key(owner, number))
