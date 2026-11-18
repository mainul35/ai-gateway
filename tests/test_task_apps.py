"""app/coding/apps.py: which outside apps a coding task may be given, and how their tools are called
beside the project's own. No servers are started.

    python -m pytest -q tests/test_task_apps.py
"""
import asyncio

import pytest

from app.coding import apps
from app.tools import mcp_client


class FakeServer:
    def __init__(self, name):
        self.name = name
        self.description = f"{name} app"


@pytest.fixture
def open_apps(monkeypatch):
    servers = [FakeServer("jira"), FakeServer("slack")]
    monkeypatch.setattr(apps, "available_to", lambda principal: servers)
    return servers


def test_check_keeps_apps_open_to_the_person_in_order(open_apps):
    assert apps.check(None, ["slack", " jira ", "slack", ""]) == ["slack", "jira"]
    assert apps.check(None, []) == []
    assert apps.check(None, None) == []


def test_check_refuses_an_app_not_open_to_the_person(open_apps):
    with pytest.raises(ValueError, match="notion is not an app"):
        apps.check(None, ["jira", "notion"])


def test_check_refuses_too_many(open_apps):
    with pytest.raises(ValueError, match="at most"):
        apps.check(None, [f"a{n}" for n in range(apps.MOST_APPS + 1)])


def test_listing(open_apps):
    assert apps.listing(None) == [{"name": "jira", "description": "jira app"},
                                  {"name": "slack", "description": "slack app"}]


def test_tools_for_notes_apps_gone_since(monkeypatch, open_apps):
    from app.tools import agent

    async def offered(servers):
        return ([{"type": "function", "function": {"name": f"{s.name}__read"}} for s in servers],
                {f"{s.name}__read": (s, "read") for s in servers}, [])
    monkeypatch.setattr(agent, "offered", offered)
    tools, where, trouble = asyncio.run(apps.tools_for(None, ["jira", "notion"]))
    assert [t["function"]["name"] for t in tools] == ["jira__read"]
    assert where["jira__read"][1] == "read"
    assert trouble == ["notion: no longer switched on or open to you"]


class Toolbox:
    current = {"a.py": "x"}

    async def call(self, name, args):
        return f"project {name}", False, {"edited": "a.py"}


def test_with_apps_sends_app_tools_to_their_server_and_the_rest_to_the_project(monkeypatch):
    server = FakeServer("jira")
    calls = []

    async def call(s, tool, arguments):
        calls.append((s.name, tool, arguments))
        if tool == "broken":
            raise mcp_client.McpError("jira.broken took too long")
        return "issue text", False
    monkeypatch.setattr(mcp_client, "call", call)
    box = apps.WithApps(Toolbox(), {"jira__get_issue": (server, "get_issue"), "jira__broken": (server, "broken")})
    assert asyncio.run(box.call("jira__get_issue", {"key": "A-1"})) == ("issue text", False, None)
    assert asyncio.run(box.call("read_file", {"path": "a.py"})) == ("project read_file", False, {"edited": "a.py"})
    assert asyncio.run(box.call("jira__broken", {})) == ("jira.broken took too long", True, None)
    assert calls[0] == ("jira", "get_issue", {"key": "A-1"})
    assert box.current == {"a.py": "x"}           # everything else is the project's toolbox
