"""Talks to MCP servers, so the gateway can use tools it does not contain.

Everything the playground can do today was written into it: the map, the picture search, the photo
tools. That does not scale, and it means the gateway can only ever do what someone put in this
repository. MCP is the way out: a server publishes a list of tools with their arguments, anybody's
client calls them, and the gateway gains whatever a server offers without a line of code here
knowing what it is.

Two kinds of server, because published ones and hand-written ones tend to be different:

    stdio  a command the gateway launches, spoken to over its standard input and output. This is
           what almost every published server is. There is no node on this host, so npm-published
           servers run through Docker; Python ones run through uvx, which is already here.
    http   a server already running somewhere else, spoken to over Streamable HTTP.

A connection lasts one operation and no longer. Holding child processes open between requests
would be faster, and it is what a later version should do, but it means owning a process registry,
an idle timer and a reconnect path - and a leaked MCP server is a process on somebody's machine
with a connection to their filesystem. One `async with` per operation cannot leak.

Nothing here trusts a server's answer. A tool's description and its results are text from somewhere
else, which is shown to the user and given to a model, never executed and never used to decide what
this gateway may do.
"""
import asyncio
import contextlib
import logging
import os
import shutil

import yaml

from app import settings

log = logging.getLogger("tools.mcp")

CONNECT_TIMEOUT = 30        # a cold uvx or docker pull is slow the first time
CALL_TIMEOUT = 120          # one tool doing its work
MAX_RESULT_CHARS = 20_000   # what comes back goes into a prompt, so it cannot be unbounded


class McpError(Exception):
    """Something went wrong talking to a server. The message is shown to the user."""


class Server:
    """One configured MCP server. Never holds a secret: those are read at connect time."""

    def __init__(self, entry):
        self.name = str(entry.get("name") or "").strip()
        self.description = str(entry.get("description") or "").strip()
        self.transport = str(entry.get("transport") or "stdio").strip().lower()
        self.enabled = bool(entry.get("enabled", True))
        self.command = str(entry.get("command") or "").strip()
        self.args = [str(a) for a in (entry.get("args") or [])]
        self.url = str(entry.get("url") or "").strip()
        # Values may be ${NAME}, resolved from the environment or config.properties at connect time
        self.env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
        self.headers = {str(k): str(v) for k, v in (entry.get("headers") or {}).items()}
        # Tools the admin has allowed; empty means every tool the server offers
        self.only = [str(t) for t in (entry.get("only") or [])]

    def trouble(self):
        """Why this entry cannot be used, or None. Checked before anything is launched."""
        if not self.name:
            return "A server needs a name."
        if self.transport not in ("stdio", "http"):
            return f"{self.name}: transport must be stdio or http, not {self.transport!r}."
        if self.transport == "stdio":
            if not self.command:
                return f"{self.name}: a stdio server needs a command to run."
            if not shutil.which(self.command):
                return (f"{self.name}: {self.command!r} is not installed on this server. "
                        f"Python servers can use uvx; anything published to npm can run in Docker.")
        elif not self.url.startswith(("http://", "https://")):
            return f"{self.name}: an http server needs a url."
        return None

    def as_json(self, tools=None, trouble=None):
        return {
            "name": self.name, "description": self.description, "transport": self.transport,
            "enabled": self.enabled,
            # What it runs or where it is, so the admin page can show it. No secret is in either:
            # a ${NAME} stays written as ${NAME} until the moment of connecting.
            "runs": " ".join([self.command, *self.args]) if self.transport == "stdio" else self.url,
            "only": self.only,
            "tools": tools if tools is not None else None,
            "trouble": trouble,
        }


def _resolved(value):
    """${NAME} read from the environment, or from config.properties, which is not in the repository.

    Secrets belong in one of those two places and never in config/mcp.yaml, which is committed.
    """
    if not (value.startswith("${") and value.endswith("}")):
        return value
    name = value[2:-1].strip()
    found = os.environ.get(name) or settings.get(name) or settings.get(name.lower().replace("_", "."))
    if not found:
        raise McpError(f"{value} is not set. Put it in the environment or in config.properties "
                       f"(which is not in the repository), not in config/mcp.yaml.")
    return found


def _resolve_all(pairs):
    return {key: _resolved(value) for key, value in pairs.items()}


def servers():
    """Every configured server, in the order the file lists them."""
    path = settings.get("mcp.servers.file", "MCP_SERVERS_FILE") or "config/mcp.yaml"
    if not os.path.isabs(path):
        path = os.path.join(settings.PROJECT_ROOT, path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("Could not read %s: %s", path, e)
        return []
    return [Server(entry) for entry in (loaded.get("servers") or []) if isinstance(entry, dict)]


def find(name):
    return next((s for s in servers() if s.name == name), None)


def is_enabled():
    return settings.get_bool("features.mcp.enabled", "FEATURE_MCP", True)


def available():
    """The servers that are switched on and could actually be reached."""
    return [s for s in servers() if s.enabled and not s.trouble()]


# --- talking to one -------------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def session(server):
    """One connection, for the length of one `async with` and no longer."""
    trouble = server.trouble()
    if trouble:
        raise McpError(trouble)
    try:
        from mcp import Client, StdioServerParameters
    except ImportError as e:
        raise McpError(f"The MCP client library is not installed on this server ({e}).") from e

    if server.transport == "stdio":
        # The child is given only what the entry names. A tool server has no business reading this
        # gateway's environment, which holds the database URL and the master key.
        target = StdioServerParameters(command=server.command, args=server.args,
                                       env=_resolve_all(server.env) or None)
    else:
        target = server.url
        if server.headers:
            # A URL on its own carries no headers, so anything needing a token gets a transport
            from mcp.client.streamable_http import StreamableHTTPTransport
            target = StreamableHTTPTransport(server.url, headers=_resolve_all(server.headers))

    try:
        # The handshake happens on entry, so a server that is not there fails here rather than
        # halfway through somebody's question
        async with Client(target, read_timeout_seconds=CALL_TIMEOUT) as client:
            yield client
    except McpError:
        raise
    except asyncio.TimeoutError as e:
        raise McpError(f"{server.name} did not answer within {CONNECT_TIMEOUT} seconds.") from e
    except Exception as e:                      # anything a transport or a server can do
        raise McpError(f"{server.name} could not be reached ({e.__class__.__name__}: {e}).") from e


def _tool_json(tool, server):
    return {
        "server": server.name,
        "name": tool.name,
        # Shown to the admin and given to a model: text from somewhere else, never instructions here
        "description": (tool.description or "").strip()[:800],
        # v2 of the library renamed these to snake_case; getattr keeps v1 working too
        "schema": getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
                  or {"type": "object", "properties": {}},
    }


async def tools_of(server):
    """Every tool a server offers, after the admin's `only` list has had its say."""
    async with session(server) as client:
        found = await asyncio.wait_for(client.list_tools(), timeout=CONNECT_TIMEOUT)
    tools = [_tool_json(tool, server) for tool in found.tools]
    if server.only:
        tools = [t for t in tools if t["name"] in server.only]
    return tools


async def probe(server):
    """What the admin page needs: does it answer, and what does it offer."""
    trouble = server.trouble()
    if trouble:
        return server.as_json(trouble=trouble)
    try:
        return server.as_json(tools=await tools_of(server))
    except McpError as e:
        return server.as_json(trouble=str(e))
    except Exception as e:
        log.info("probe of %s failed: %s", server.name, e)
        return server.as_json(trouble=f"{e.__class__.__name__}: {e}")


def _as_text(result):
    """A tool's answer as something that can go in a prompt."""
    parts = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(getattr(item, "text", "") or "")
        elif kind == "image":
            kind_of = getattr(item, 'mime_type', None) or getattr(item, 'mimeType', 'unknown type')
            parts.append(f"[an image, {kind_of}]")
        elif kind == "resource":
            resource = getattr(item, "resource", None)
            parts.append(getattr(resource, "text", None) or f"[a resource: {getattr(resource, 'uri', '')}]")
        else:
            parts.append(f"[{kind or 'something'} this gateway cannot show]")
    text = "\n".join(p for p in parts if p).strip()
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n[…cut after {MAX_RESULT_CHARS} characters]"
    return text or "(the tool returned nothing)"


async def call(server, tool, arguments):
    """Runs one tool and returns (text, failed).

    A tool that reports failure is not an error here: the model asked for it, the answer is that it
    did not work, and that is something to tell the model rather than to end the turn over.
    """
    if server.only and tool not in server.only:
        raise McpError(f"{tool} is not one of the tools allowed for {server.name}.")
    async with session(server) as client:
        try:
            result = await asyncio.wait_for(client.call_tool(tool, arguments or {}),
                                            timeout=CALL_TIMEOUT)
        except asyncio.TimeoutError as e:
            raise McpError(f"{server.name}.{tool} took longer than {CALL_TIMEOUT} seconds.") from e
    failed = getattr(result, "is_error", None)
    if failed is None:
        failed = getattr(result, "isError", False)
    return _as_text(result), bool(failed)
