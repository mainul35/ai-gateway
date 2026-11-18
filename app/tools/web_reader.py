"""Reading a web page the user names: "give me the content of <link>".

Search only ever showed the model a few thousand characters of a few pages it chose itself. This
reads the page the user pointed at, as much of it as the model can take: an article, a README, a
PDF, a plain text file. Through app.net like every other outside fetch, so a link (or a redirect it
gives) can never point the server at its own network.

A search engine's result page (google.com/search?q=...) cannot be read this way: Google, Bing and
DuckDuckGo answer a program with a consent page or a challenge, and scraping them is against their
terms. So a link to one is read as what it is - a search - and the same query is run through the
gateway's own search engine (SearXNG), whose results and pages the model then gets.

Two ways in, sharing one reader:
- a plain chat: links in the user's message are read before the model starts (links_wanted, prompt);
- the agent: tools web__read_url and web__search, so it can open a link from earlier in the
  conversation, or one it found, and read on past the first part of a long page.
"""
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app import access, net, settings
from app.tools import documents, web_search

log = logging.getLogger("tools.web")

SERVER = "web"
MOST_LINKS = 3                # links read from one message
READ_BYTES = 8_000_000        # a page, or a PDF, is read up to this much
READ_TIMEOUT = 20
CHAT_CHARS = 24_000           # text of one page given to a plain chat
TOOL_CHARS = 30_000           # text of one page per tool call; the agent asks for more with `start`
THIN = 200                    # less text than this from an HTML page means it is drawn by its scripts

URL = re.compile(r"https?://[^\s<>\"'`]+", re.I)
# Handled by app/tools/linkedin.py, which knows how to get a job or post out of LinkedIn
LINKEDIN = re.compile(r"(^|\.)(linkedin\.com|lnkd\.in)$", re.I)
# Words that point back at a link given earlier: "get the content of that link"
REFERS = re.compile(r"\b(link|url|page|site|website|article|content|post|that|it)\b", re.I)

# host pattern -> the parameter that holds the query
SEARCH_ENGINES = [
    (re.compile(r"(^|\.)google\.[a-z.]+$"), "/search", "q"),
    (re.compile(r"(^|\.)bing\.com$"), "/search", "q"),
    (re.compile(r"(^|\.)duckduckgo\.com$"), "/", "q"),
    (re.compile(r"(^|\.)search\.yahoo\.com$"), "/search", "p"),
    (re.compile(r"(^|\.)search\.brave\.com$"), "/search", "q"),
    (re.compile(r"(^|\.)yandex\.[a-z.]+$"), "/search", "text"),
    (re.compile(r"(^|\.)baidu\.com$"), "/s", "wd"),
]


class ReadError(Exception):
    """The page could not be read; the message says why, for the user."""


class Builtin:
    """Stands where an MCP server would in the agent's tool map."""
    name = SERVER


BUILTIN = Builtin()


def is_enabled():
    return settings.get_bool("tools.web.enabled", "TOOLS_WEB_ENABLED", True)


def is_available_to(principal):
    """The same permission as web search: it is the same kind of outside fetch."""
    return is_enabled() and principal is not None and access.can_use_feature(principal, "web_search")


# --- links -------------------------------------------------------------------------------------


def links(text):
    """The web links in a message, in order, at most MOST_LINKS, LinkedIn's left to its own reader."""
    found = []
    for match in URL.finditer(text or ""):
        url = match.group(0).rstrip(".,;:!?)]}*_")
        host = (urlparse(url).hostname or "").lower()
        if not host or LINKEDIN.search(host) or url in found:
            continue
        found.append(url)
    return found[:MOST_LINKS]


def links_wanted(said):
    """The links a plain chat should read for the user's latest message: the ones in it, or, when it
    has none and points back ("give me the content of that link"), the ones in the message before
    that had any. `said` is the user's messages, oldest first."""
    if not said:
        return []
    latest = links(said[-1])
    if latest or not REFERS.search(said[-1]):
        return latest
    for earlier in reversed(said[-4:-1]):
        found = links(earlier)
        if found:
            return found
    return []


def search_query(url):
    """The query of a search engine's result page, or None when the link is not one."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for pattern, path, param in SEARCH_ENGINES:
        if pattern.search(host) and parsed.path.rstrip("/").startswith(path.rstrip("/")):
            query = (parse_qs(parsed.query).get(param) or [""])[0].strip()
            return query or None
    return None


# --- reading -----------------------------------------------------------------------------------


def _html_text(html):
    parser = web_search._TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        raise ReadError("the page could not be read")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if len(line) > 3)
    return re.sub(r"\s+", " ", parser.title).strip()[:300], text


async def _results(query):
    """(results, their text): a search through the gateway's own engine, with the top pages read."""
    try:
        results = await web_search.search([query])
    except web_search.SearchError as e:
        raise ReadError(f"the search failed ({e})")
    if not results:
        raise ReadError(f'a search for "{query}" found nothing')
    results = await web_search.fetch_pages(results)
    return results, "\n\n".join(f"{n}. {r['title']}\nURL: {r['url']}\n{r.get('content') or r.get('snippet') or ''}"
                                for n, r in enumerate(results, 1))


async def _search(query, url):
    if not web_search.is_available():
        raise ReadError("it is a search engine's result page, which cannot be read directly, and this "
                        "gateway has no search engine of its own to run the search with")
    try:
        results, found = await _results(query)
    except ReadError as e:
        raise ReadError(f"it is a search engine's result page, so the same search was run instead, and {e}")
    host = urlparse(url).hostname or "the search engine"
    text = (f'This link is {host}\'s result page for the search "{query}". A search engine\'s result '
            f"page cannot be read by a program (it answers with a consent page or a challenge), so the "
            f"same search was run through the gateway's own search engine. Its results, with the text "
            f"of the top pages:\n\n" + found)
    return {"url": url, "title": f'Search: {query}', "text": text, "search": query,
            "results": [{"title": r["title"], "url": r["url"]} for r in results]}


async def read(url):
    """{url, title, text} of one page (or a search's results, for a search engine's link).
    Raises ReadError with a reason."""
    if not re.match(r"https?://", url or "", re.I):
        raise ReadError("only http and https links can be read")
    query = search_query(url)
    if query:
        return await _search(query, url)
    # Says what it is, as search does: a site that lets programs in lets this in, and one that
    # does not is told the truth rather than shown a browser it is not
    headers = {"User-Agent": web_search.USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain,*/*",
               "Accept-Language": "en-US,en;q=0.8"}
    async with net.client(timeout=READ_TIMEOUT, headers=headers) as client:
        try:
            page = await net.get(client, url, max_bytes=READ_BYTES, truncate=True)
        except net.Refused as e:
            raise ReadError(str(e))
        except httpx.HTTPError as e:
            raise ReadError(f"the site did not answer ({type(e).__name__})")
    if page.status_code in (401, 403, 429):
        raise ReadError(f"the site refused to show it to a program (it answered {page.status_code}); it may "
                        "need a sign-in, or block automated readers")
    if page.status_code != 200:
        raise ReadError(f"the site answered {page.status_code}")
    kind = page.content_type
    name = unquote(urlparse(page.url).path.rsplit("/", 1)[-1]) or "page"
    if "html" in kind or (not kind and page.content[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))):
        title, text = _html_text(page.text)
        if len(text) < THIN:
            raise ReadError("the page draws its content with JavaScript in the browser, so there is almost "
                            "no text in what the site sends" + (f" (all it had: {text!r})" if text else ""))
    elif kind == "application/pdf" or _document(name, page.content):
        if kind == "application/pdf" and not name.lower().endswith(".pdf"):
            name += ".pdf"
        try:
            text = documents.extract(name, page.content)
        except documents.DocumentError as e:
            raise ReadError(str(e))
        title = name
    elif kind.startswith("text/") or kind in ("application/json", "application/xml") or kind.endswith("+json"):
        title, text = name, page.text
    else:
        raise ReadError(f"it is not a page or a document ({kind or 'unknown type'})")
    if page.truncated:
        text += "\n\n[The page is larger than the gateway reads; this is its beginning.]"
    return {"url": page.url, "title": title or name, "text": text.strip()}


def _document(name, data):
    try:
        return documents.kind_of(name, data) is not None
    except documents.DocumentError:
        return False


# --- a plain chat ------------------------------------------------------------------------------


def as_source(page):
    host = urlparse(page["url"]).hostname or ""
    return {"title": page["title"] or host, "url": page["url"], "site": host, "published": None,
            "content": _part(page["text"], 0, CHAT_CHARS)}


def _part(text, start, size):
    piece = text[start:start + size]
    if start + size < len(text):
        piece += (f"\n\n[Characters {start + 1}-{start + len(piece)} of {len(text)}. The rest was left out "
                  "for length.]")
    return piece


def prompt(sources, failures):
    """System message with the pages the user linked, numbered for citations, and what could not be
    read and why."""
    parts = []
    if sources:
        blocks = [f"[{n}] {s['title']}\nURL: {s['url']}\n{s['content']}" for n, s in enumerate(sources, 1)]
        parts.append("The user's message links to the web pages below. They were fetched just now; their "
                     "text is what the page says. When the user asks for the content, give it to them - "
                     "in full, or as much as they asked for, keeping its structure as Markdown - rather "
                     "than a summary. Otherwise answer from it. Cite the pages as [1], [2]. Do not add "
                     "anything the text does not contain.\n\n" + "\n\n".join(blocks))
    if failures:
        parts.append("These linked pages could not be read, so you do not know what they say. Tell the user, "
                     "with the reason, and do not guess their content:\n" +
                     "\n".join(f"- {url}: {reason}" for url, reason in failures))
    return "\n\n".join(parts)


# --- the agent's tools ---------------------------------------------------------------------------

GUIDE = ("You can open web links: web{sep}read_url reads any page, PDF or text file, including one the user "
         "gave earlier in the conversation, and web{sep}search searches the web. When the user gives a link "
         "or asks what a page says, read it with web{sep}read_url instead of saying you cannot open links. "
         "Asked for a page's content, give the content itself, not a summary of it.")

TOOLS = {
    "read_url": {
        "description": "Reads one web page, PDF or text file at a URL and returns its text (without menus). "
                       "Use it whenever the user gives a link or asks for what a page says. A long page "
                       f"comes in parts of {TOOL_CHARS} characters: call again with `start` for the next. "
                       "A search engine's result link (google.com/search?q=...) returns that search's "
                       "results instead, with the top pages' text.",
        "schema": {"type": "object", "properties": {
            "url": {"type": "string", "description": "The full http(s) address"},
            "start": {"type": "integer", "description": "Character to start from, for the next part of a "
                                                        "long page (default 0)"}},
            "required": ["url"]},
    },
    "search": {
        "description": "Searches the web and returns the top results with the text of the best pages. Use "
                       "it to find pages; use read_url to read one in full.",
        "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}


def listing():
    return [{"server": SERVER, "name": name, "description": tool["description"], "schema": tool["schema"]}
            for name, tool in TOOLS.items() if name != "search" or web_search.is_available()]


async def call(tool, args):
    """Runs one tool. Returns (text, failed), like mcp_client.call."""
    args = args or {}
    if tool == "read_url":
        url = str(args.get("url") or "").strip()
        try:
            start = max(0, int(args.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            page = await read(url)
        except ReadError as e:
            return f"Could not read {url}: {e}", True
        if start and start >= len(page["text"]):
            return f"{url} has {len(page['text'])} characters; there is nothing after {start}.", True
        return (f"URL: {page['url']}\nTitle: {page['title']}\n\n" + _part(page["text"], start, TOOL_CHARS)
                + (f"\n[Call read_url again with start={start + TOOL_CHARS} for the next part.]"
                   if start + TOOL_CHARS < len(page["text"]) else "")), False
    if tool == "search":
        query = str(args.get("query") or "").strip()
        if not query:
            return "Say what to search for.", True
        if not web_search.is_available():
            return "This gateway has no search engine.", True
        try:
            return (await _results(query))[1], False
        except ReadError as e:
            return str(e), True
    return f"{SERVER} has no tool called {tool}.", True
