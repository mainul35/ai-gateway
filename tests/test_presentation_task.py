"""app/coding/presentation.py: a presentation task's links and tools. Nothing is fetched: the page
reader is replaced.

    python -m pytest -q tests/test_presentation_task.py
"""
import asyncio

import pytest

from app.coding import presentation
from app.tools import web_reader


def run(coroutine):
    return asyncio.run(coroutine)


def test_sources_are_plain_web_addresses_without_repeats():
    links = presentation.clean_sources([" https://wiki.example/page ", "", "https://github.com/o/r",
                                        "https://wiki.example/page"])
    assert links == ["https://wiki.example/page", "https://github.com/o/r"]
    for bad in ("file:///etc/passwd", "wiki.example/page", "https://a b"):
        with pytest.raises(ValueError, match="not a web address"):
            presentation.clean_sources([bad])
    with pytest.raises(ValueError, match="at most"):
        presentation.clean_sources([f"https://e.example/{n}" for n in range(presentation.MOST_SOURCES + 1)])


def test_write_slides_ends_the_task_only_with_a_deck():
    tools = presentation.DeckTools()
    text, failed, outcome = run(tools.call("write_slides", {"markdown": "Some thoughts."}))
    assert failed and outcome is None and "## " in text
    deck = "# Title\nSub\n\n## First\n- a point\n- another point"
    text, failed, outcome = run(tools.call("write_slides", {"markdown": deck}))
    assert not failed and outcome == {"slides": deck}


def test_reading_a_page_needs_the_permission_and_project_tools_only_read(monkeypatch):
    async def read(tool, args):
        return f"read {args['url']}", False
    monkeypatch.setattr(web_reader, "call", read)
    assert run(presentation.DeckTools().call("read_url", {"url": "https://e.example"}))[:2] == \
        ("read https://e.example", False)
    assert run(presentation.DeckTools(can_read_web=False).call("read_url", {"url": "https://e.example"}))[1]

    class Project:
        async def call(self, name, args):
            return f"project {name}", False, None
    tools = presentation.DeckTools(Project())
    assert run(tools.call("read_file", {"path": "README.md"}))[0] == "project read_file"
    for changing in ("edit_file", "create_file", "run_command"):
        assert run(tools.call(changing, {}))[1]            # a presentation changes and runs nothing
    assert run(tools.call("note", {"text": "x"}))[2] == {"note": "x"}
