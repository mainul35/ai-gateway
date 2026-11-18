"""app/tools/web_reader.py: reading the page a user links to. Nothing is fetched: app.net and the
search are replaced.

    python -m pytest -q tests/test_web_reader.py
"""
import asyncio

import httpx
import pytest

from app import net
from app.tools import web_reader, web_search

ARTICLE = ("<html><head><title>Turmoil in software</title><script>var x = 1;</script></head><body>"
           "<nav>Home | About</nav><article><h1>Turmoil in software</h1>"
           + "".join(f"<p>Paragraph {n}: the software industry keeps changing in ways worth reading about.</p>"
                     for n in range(400))
           + "</article><footer>Copyright</footer></body></html>")


def test_links():
    said = ("Read https://example.com/a.html, and https://www.linkedin.com/posts/x and https://lnkd.in/p/abc "
            "then (https://example.org/b) and https://example.com/a.html again")
    assert web_reader.links(said) == ["https://example.com/a.html", "https://example.org/b"]


def test_links_wanted_points_back_at_an_earlier_link():
    first = "What does https://example.com/post say?"
    assert web_reader.links_wanted([first]) == ["https://example.com/post"]
    assert web_reader.links_wanted([first, "Get the entire content of that link"]) == ["https://example.com/post"]
    assert web_reader.links_wanted([first, "Thanks, now write a poem"]) == []
    assert web_reader.links_wanted([]) == []


def test_search_query():
    assert web_reader.search_query("https://www.google.com/search?q=As+per+the+current+situation&sca=1") \
        == "As per the current situation"
    assert web_reader.search_query("https://www.bing.com/search?q=rust") == "rust"
    assert web_reader.search_query("https://duckduckgo.com/?q=jdk") == "jdk"
    assert web_reader.search_query("https://www.google.com/maps/place/x") is None
    assert web_reader.search_query("https://example.com/search?q=x") is None


def fetched(body, kind="text/html; charset=utf-8", status=200, url="https://example.com/a"):
    return net.Fetched(url=url, status_code=status, headers=httpx.Headers({"content-type": kind}),
                       content=body.encode() if isinstance(body, str) else body, encoding="utf-8", truncated=False)


@pytest.fixture
def serve(monkeypatch):
    answers = {}

    async def get(client, url, max_bytes, truncate=False, hops=5, headers=None):
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(net, "get", get)
    return answers


def test_reads_a_page_without_its_menus(serve):
    serve["https://example.com/a"] = fetched(ARTICLE)
    page = asyncio.run(web_reader.read("https://example.com/a"))
    assert page["title"] == "Turmoil in software"
    assert "Paragraph 399" in page["text"] and "Home | About" not in page["text"] and "var x" not in page["text"]


def test_tool_reads_a_long_page_in_parts(serve):
    serve["https://example.com/a"] = fetched(ARTICLE)
    first, bad = asyncio.run(web_reader.call("read_url", {"url": "https://example.com/a"}))
    assert not bad and "Paragraph 0" in first and f"start={web_reader.TOOL_CHARS}" in first
    second, bad = asyncio.run(web_reader.call("read_url", {"url": "https://example.com/a",
                                                            "start": web_reader.TOOL_CHARS}))
    assert not bad and "Paragraph 399" in second and "Paragraph 0:" not in second


def test_reasons_a_page_cannot_be_read(serve):
    serve["https://example.com/js"] = fetched("<html><body><div id=root></div><script>app()</script></body></html>")
    serve["https://example.com/no"] = fetched("Forbidden", status=403)
    serve["https://example.com/zip"] = fetched(b"PK..", kind="application/zip")
    serve["https://example.com/inside"] = net.Refused("it leads to a private address")
    for url, reason in [("https://example.com/js", "JavaScript"), ("https://example.com/no", "403"),
                        ("https://example.com/zip", "not a page"), ("https://example.com/inside", "private"),
                        ("ftp://example.com/x", "only http")]:
        with pytest.raises(web_reader.ReadError, match=reason):
            asyncio.run(web_reader.read(url))
    text, bad = asyncio.run(web_reader.call("read_url", {"url": "https://example.com/no"}))
    assert bad and "403" in text


def test_plain_text(serve):
    serve["https://example.com/notes.txt"] = fetched("line one\nline two", kind="text/plain",
                                                     url="https://example.com/notes.txt")
    page = asyncio.run(web_reader.read("https://example.com/notes.txt"))
    assert page["text"] == "line one\nline two" and page["title"] == "notes.txt"


def test_a_search_engine_link_runs_the_search(monkeypatch):
    asked = []

    async def search(queries, limit=None, wants_recent=False):
        asked.append(queries)
        return [{"title": "Layoffs in 2026", "url": "https://news.example/layoffs", "snippet": "s"}]

    async def fetch_pages(results, least=200, wanted=None):
        results[0]["content"] = "The full story."
        return results
    monkeypatch.setattr(web_search, "is_available", lambda: True)
    monkeypatch.setattr(web_search, "search", search)
    monkeypatch.setattr(web_search, "fetch_pages", fetch_pages)
    page = asyncio.run(web_reader.read("https://www.google.com/search?q=software+turmoil"))
    assert asked == [["software turmoil"]]
    assert "The full story." in page["text"] and "https://news.example/layoffs" in page["text"]
    assert page["search"] == "software turmoil"


def test_prompt():
    source = web_reader.as_source({"url": "https://example.com/a", "title": "A", "text": "x" * (web_reader.CHAT_CHARS + 10)})
    text = web_reader.prompt([source], [("https://example.com/b", "the site answered 404")])
    assert "[1] A" in text and "give it to them" in text and "404" in text and "left out for length" in text
