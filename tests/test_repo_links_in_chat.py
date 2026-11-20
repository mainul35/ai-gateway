"""app/routers/chat.py _read_web_pages: a GitHub repository linked in a plain chat is opened from git
with the tokens the person may use, not fetched as a github.com page (a private one's page is a 404
to anyone not signed in). Anything else is read as a web page. Nothing is called.

    python -m pytest -q tests/test_repo_links_in_chat.py
"""
import asyncio

from app.routers import chat
from app.tools import repos, self_tools, web_reader


def read(links, monkeypatch, allowed=True, overview=None):
    monkeypatch.setattr(repos, "is_enabled", lambda: True)
    monkeypatch.setattr(self_tools, "is_available_to", lambda principal: allowed)
    fetched = []

    async def page(url):
        fetched.append(url)
        if "github.com" in url:
            raise web_reader.ReadError("404")
        return {"title": "A page", "url": url, "text": "words"}

    async def opened(principal, args):
        if overview is None:
            raise repos.RepoError("no token you may use reaches it")
        return dict(overview)
    monkeypatch.setattr(web_reader, "read", page)
    monkeypatch.setattr(repos, "overview", opened)
    pages, failed = [], []

    async def run():
        async for _ in chat._read_web_pages(links, pages, failed, principal=object()):
            pass
    asyncio.run(run())
    return pages, failed, fetched


def test_a_repository_is_opened_from_git(monkeypatch):
    pages, failed, fetched = read(["https://github.com/Org/private-repo", "https://example.com/x"],
                                  monkeypatch, overview={"repository": "Org/private-repo", "readme": "hello"})
    assert fetched == ["https://example.com/x"]                  # the github.com page was not fetched
    assert pages[0]["title"] == "Org/private-repo" and "hello" in pages[0]["content"]
    assert not failed


def test_a_repository_that_cannot_be_opened_says_why(monkeypatch):
    pages, failed, fetched = read(["https://github.com/Org/private-repo"], monkeypatch)
    assert not pages and not fetched
    assert "no token you may use" in failed[0][1]


def test_without_the_repository_tools_it_is_a_web_page(monkeypatch):
    pages, failed, fetched = read(["https://github.com/Org/private-repo"], monkeypatch, allowed=False)
    assert fetched == ["https://github.com/Org/private-repo"]
