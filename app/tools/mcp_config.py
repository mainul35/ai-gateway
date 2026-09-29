"""Reads and writes config/mcp.yaml, so tool servers can be managed from the browser.

The file stays readable and hand-editable - it is still the thing you would reach for over ssh -
but it is no longer the only way in. Writing it rewrites the whole file, so the explanation at the
top is kept here rather than in the file: anything a person adds above the servers would be lost on
the next save, and quietly losing somebody's notes is worse than not having them.

Two things this deliberately will not do. It will not write a secret: a value that looks like a
credential and is not a ${NAME} reference is refused, because this file is in the repository. And
it will not take a name that is already used, because two servers answering to one name makes the
question "which tool is this" unanswerable.
"""
import contextlib
import os
import re
import shutil
import tempfile

import yaml

from app import settings
from app.tools import mcp_client

HEADER = """# MCP servers this gateway can call.
#
# This file is rewritten whenever a server is saved from the Tools page, so notes added by hand
# above this line will not survive. Everything the page can set is here; anything it cannot, you
# can still write by hand and the page will show it.
#
#   transport: stdio   a command the gateway launches and talks to over its standard input and
#                      output. Almost every published server is one of these. This host has no
#                      node, so servers published to npm run through Docker; Python ones run
#                      through uvx, which is already installed.
#   transport: http    a server already running somewhere, spoken to over Streamable HTTP.
#
# Secrets never go in this file - it is in the repository. Write ${NAME} and it is read from the
# environment, or from config/config.properties, which is not.
#
#   env:
#     GITHUB_TOKEN: ${GITHUB_TOKEN}
#
# `only:` narrows a server to the tools you name. Leave it out and every tool it offers is
# available. Nothing is called until enabled: true.

"""

NAME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.I)
# A value that is plainly a credential rather than a name of one. Long and random, or the shapes
# tokens are usually minted in.
SECRET_SHAPE = re.compile(r"^(sk-|ghp_|github_pat_|gho_|xox[baprs]-|AIza|ya29\.|eyJ)"
                          r"|^[A-Za-z0-9+/=_-]{40,}$")


class ConfigError(Exception):
    """Something about the entry is wrong. The message is shown to whoever is editing."""


def path():
    where = settings.get("mcp.servers.file", "MCP_SERVERS_FILE") or "config/mcp.yaml"
    return where if os.path.isabs(where) else os.path.join(settings.PROJECT_ROOT, where)


def _raw():
    """The file as data, or an empty document when there is not one yet."""
    if not os.path.exists(path()):
        return {"servers": []}
    try:
        with open(path(), encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"config/mcp.yaml could not be read: {e}") from e
    servers = loaded.get("servers")
    return {"servers": servers if isinstance(servers, list) else []}


def _checked_secrets(where, pairs):
    for key, value in (pairs or {}).items():
        if not str(key).strip():
            raise ConfigError(f"A {where} entry has no name.")
        if SECRET_SHAPE.search(str(value or "")):
            raise ConfigError(
                f"{key} looks like a credential rather than the name of one. This file is in the "
                f"repository, so write ${{{key}}} instead and put the value in the environment or "
                f"in config/config.properties.")


def clean(entry, existing_names=(), renaming_from=None):
    """One entry, checked and normalised, or ConfigError saying what is wrong with it."""
    name = str(entry.get("name") or "").strip()
    if not NAME_SHAPE.match(name):
        raise ConfigError("A name is required: letters, digits, dots, dashes and underscores, "
                          "up to 64 characters.")
    if name in existing_names and name != renaming_from:
        raise ConfigError(f"There is already a server called {name}.")

    transport = str(entry.get("transport") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        raise ConfigError("A server speaks either stdio or http.")

    out = {
        "name": name,
        "description": str(entry.get("description") or "").strip()[:300],
        "transport": transport,
        "enabled": bool(entry.get("enabled", False)),
    }

    if transport == "stdio":
        command = str(entry.get("command") or "").strip()
        if not command:
            raise ConfigError("A stdio server needs a command to run.")
        out["command"] = command
        out["args"] = [str(a) for a in (entry.get("args") or []) if str(a).strip()]
        env = {str(k).strip(): str(v) for k, v in (entry.get("env") or {}).items() if str(k).strip()}
        _checked_secrets("env", env)
        if env:
            out["env"] = env
    else:
        url = str(entry.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ConfigError("An http server needs a url starting with http:// or https://.")
        out["url"] = url
        headers = {str(k).strip(): str(v)
                   for k, v in (entry.get("headers") or {}).items() if str(k).strip()}
        _checked_secrets("header", headers)
        if headers:
            out["headers"] = headers

    only = [str(t).strip() for t in (entry.get("only") or []) if str(t).strip()]
    if only:
        out["only"] = only
    return out


def _write(servers):
    """Rewrites the file in one go, through a temporary file so a crash cannot truncate it."""
    body = yaml.safe_dump({"servers": servers}, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=100)
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=os.path.dirname(target), prefix=".mcp-", suffix=".yaml")
    try:
        handle.write(HEADER + body)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        shutil.move(handle.name, target)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


def add(entry):
    document = _raw()
    names = [str(s.get("name") or "") for s in document["servers"]]
    document["servers"].append(clean(entry, existing_names=names))
    _write(document["servers"])
    return document["servers"][-1]


def update(name, entry):
    document = _raw()
    names = [str(s.get("name") or "") for s in document["servers"]]
    if name not in names:
        raise ConfigError(f"No server called {name} is configured.")
    at = names.index(name)
    # Everything the page did not send keeps the value it had, so a toggle need not resend the rest
    merged = {**document["servers"][at], **{k: v for k, v in entry.items() if v is not None}}
    document["servers"][at] = clean(merged, existing_names=names, renaming_from=name)
    _write(document["servers"])
    return document["servers"][at]


def remove(name):
    document = _raw()
    kept = [s for s in document["servers"] if str(s.get("name") or "") != name]
    if len(kept) == len(document["servers"]):
        raise ConfigError(f"No server called {name} is configured.")
    _write(kept)
    return len(document["servers"]) - len(kept)


def as_edited(server):
    """One server in the shape the editing form uses, with nothing hidden from it.

    Values here are names of secrets, never secrets: anything that resolves to one is written
    ${LIKE_THIS} and stays that way until the moment of connecting.
    """
    return {
        "name": server.name, "description": server.description, "transport": server.transport,
        "enabled": server.enabled, "command": server.command, "args": list(server.args),
        "url": server.url, "env": dict(server.env), "headers": dict(server.headers),
        "only": list(server.only),
        "runs": " ".join([server.command, *server.args]) if server.transport == "stdio" else server.url,
        "trouble": server.trouble(),
    }


def everything():
    return [as_edited(server) for server in mcp_client.servers()]
