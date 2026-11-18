"""Outside apps a coding task may use: the gateway's tool servers (MCP), the same ones the playground's
agent reaches - an issue tracker, a chat, a wiki, a database - set up by an admin on the Tool servers
page and each open to the people it is open to (access.tool_servers_for).

A task gets none of them unless the person starting it ticks them, because what a task does in an
outside app happens at once: unlike its changes to the project, nobody reviews it first.
"""
from app import access
from app.tools import mcp_client

MOST_APPS = 8

GUIDE = """

Outside apps: you also have the tools of {apps}, named <app>__<tool>. Use them to read what the task
needs (an issue, a ticket, a page, a record). Unlike your work on the project, what they do happens at
once and nobody reviews it first: change something in them - post, create, update, delete - only when
the task asks for exactly that."""


def available_to(principal):
    """The tool servers this person may give a task: switched on, reachable, and open to them."""
    if not mcp_client.is_enabled():
        return []
    return access.tool_servers_for(principal, mcp_client.available())


def listing(principal):
    return [{"name": s.name, "description": s.description} for s in available_to(principal)]


def check(principal, names):
    """The apps asked for, each one this person may use; ValueError names one that is not."""
    wanted = []
    for name in names or []:
        name = str(name).strip()
        if name and name not in wanted:
            wanted.append(name)
    if len(wanted) > MOST_APPS:
        raise ValueError(f"A task can use at most {MOST_APPS} apps")
    open_to = {s.name for s in available_to(principal)}
    for name in wanted:
        if name not in open_to:
            raise ValueError(f"{name} is not an app you can give a task: it is switched off, cannot be "
                             "reached, or is not open to you")
    return wanted


async def tools_for(principal, names):
    """(tools in the model's format, {tool name: (server, tool)}, what could not be used and why).

    Checked again when the task runs, not only when it was started: an app switched off or closed to
    the person since is left out, with a note."""
    from app.tools import agent       # here: agent reaches the coding package through its own tools
    servers = [s for s in available_to(principal) if s.name in set(names)]
    trouble = [f"{name}: no longer switched on or open to you" for name in names
               if name not in {s.name for s in servers}]
    if not servers:
        return [], {}, trouble
    tools, where, problems = await agent.offered(servers)
    return tools, where, trouble + problems


class WithApps:
    """A task's own tools, with the apps' tools beside them: a call to an app goes to its server,
    everything else to the project's toolbox as before."""

    def __init__(self, toolbox, where):
        self._toolbox = toolbox
        self._where = where

    def __getattr__(self, name):
        return getattr(self._toolbox, name)

    async def call(self, name, args):
        if name not in self._where:
            return await self._toolbox.call(name, args)
        server, tool = self._where[name]
        try:
            text, failed = await mcp_client.call(server, tool, args)
        except mcp_client.McpError as e:
            text, failed = str(e), True
        except Exception as e:
            text, failed = f"{server.name} did not answer: {e.__class__.__name__}: {e}", True
        return text, failed, None
