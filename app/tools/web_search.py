"""Web search for the playground: SearXNG for results, then the text of the top pages.

The results are handed to the model as numbered sources, so it can answer from them and cite [1], [2].
"""
import asyncio
import datetime
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from app import settings

log = logging.getLogger("tools.search")

PAGE_TIMEOUT = 8
PAGE_BYTES = 1_500_000      # stop reading a page after this much HTML
PAGE_CHARS = 3000           # text kept per fetched page
SNIPPET_CHARS = 400
RECENCY_WORDS = re.compile(
    r"\b(latest|newest|current|currently|today|now|recent|recently|this (week|month|year)|"
    r"up to date|up-to-date|still|yet|news|released?|release date|version)\b", re.I)
# Identifies the gateway while still looking like a browser. Wikipedia refuses a plain spoofed
# browser agent with 403, and some sites refuse anything that does not start with Mozilla.
USER_AGENT = "Mozilla/5.0 (compatible; ai-gateway/0.1; +https://github.com/mainul35/ai-gateway)"


class SearchError(Exception):
    pass


# A question about what is current is answered by whoever publishes the thing, not by a tutorial site
# that copied an answer years ago. Ranking cannot judge truth, but it can prefer the primary source.
PRIMARY_HINTS = ("docs.", "developer.", "download", "release", "blog.", "news.", "support.", "help.")
PRIMARY_SUFFIXES = (".org", ".dev", ".io", ".gov", ".edu")
REFERENCE_SITES = ("wikipedia.org", "endoflife.date")            # curated and kept current
COMMUNITY_SITES = ("stackoverflow.com", "github.com", "gitlab.com")  # useful, but not the publisher
# Sites that rewrite other people's answers; they are the ones that stay stale for years
LOW_VALUE_SITES = ("w3schools.com", "geeksforgeeks.org", "javatpoint.com", "tutorialspoint.com",
                   "guru99.com", "simplilearn.com", "medium.com", "quora.com", "reddit.com",
                   "pinterest.com", "slideshare.net", "coursehero.com", "baeldung.com/tag")


def _domain(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _published(result):
    raw = result.get("publishedDate") or ""
    try:
        return datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _authority(url):
    """Rough standing of a source, from its address alone.

    Deliberately not "does the domain contain the word being asked about": that promoted every site
    with java in its name over the release notes and the encyclopaedia entry that actually answered.
    """
    domain = _domain(url)
    if any(site in domain for site in LOW_VALUE_SITES):
        return -3
    score = 0
    if any(site in domain for site in REFERENCE_SITES):
        score += 2
    elif any(site in domain for site in COMMUNITY_SITES):
        score += 1
    if any(hint in url.lower() for hint in PRIMARY_HINTS):
        score += 1     # release notes, documentation, a newsroom: where a thing is announced
    if domain.endswith(PRIMARY_SUFFIXES):
        score += 0.5
    return score


def rank(results, query, wants_recent):
    """Best first: what the engines agreed on, then standing, then freshness when it was asked for."""
    today = datetime.date.today()

    def key(result):
        published = _published(result)
        age_days = (today - published).days if published else None
        freshness = 0
        if wants_recent and age_days is not None:
            freshness = 2 if age_days <= 120 else 1 if age_days <= 550 else -1
        elif age_days is not None and age_days > 1500:
            freshness = -1  # very old, and nothing in the question said history
        # The engines' own ranking is the base; ours only nudges it
        return -(float(result.get("score") or 0) + _authority(result["url"]) + freshness)

    ranked = sorted(results, key=key)
    # At most two pages from one site: five pages of the same manual are not five sources
    seen, kept = {}, []
    for result in ranked:
        domain = _domain(result["url"])
        if seen.get(domain, 0) >= 2:
            continue
        seen[domain] = seen.get(domain, 0) + 1
        kept.append(result)
    return kept


def is_available():
    return settings.feature_enabled("web_search") and bool(settings.searxng_url())


async def _ask_searxng(client, query):
    response = await client.get(f"{settings.searxng_url()}/search",
                                params={"q": query, "format": "json", "safesearch": 1,
                                        # Otherwise the same article comes back in several languages
                                        "language": settings.search_language()},
                                # SearXNG's bot detection wants to know the client; it is us
                                headers={"X-Real-IP": "127.0.0.1"})
    response.raise_for_status()
    return response.json().get("results") or []


async def search(query, limit=None, wants_recent=False):
    """Returns [{title, url, site, published, snippet}], most useful first."""
    limit = limit or settings.search_results()
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            results = await _ask_searxng(client, query)
            if not results:
                # Engines time out or hit a CAPTCHA now and then, and one empty search is not an answer
                log.info("Empty result for %r, asking once more", query)
                await asyncio.sleep(1)
                results = await _ask_searxng(client, query)
    except (httpx.HTTPError, ValueError) as e:
        raise SearchError(f"search service unavailable ({e.__class__.__name__})") from e

    seen, unique = set(), []
    for result in results:
        url = result.get("url")
        if url and url not in seen and url.startswith(("http://", "https://")):
            seen.add(url)
            unique.append(result)

    found = []
    for result in rank(unique, query, wants_recent)[:limit]:
        published = _published(result)
        found.append({"title": (result.get("title") or result["url"]).strip(), "url": result["url"],
                      "site": _domain(result["url"]),
                      "published": published.isoformat() if published else None,
                      "snippet": re.sub(r"\s+", " ", result.get("content") or "").strip()[:SNIPPET_CHARS]})
    return found


# --- page text ------------------------------------------------------------------

def _is_public_host(host):
    """Pages are fetched from the server, so never let a result point it at the LAN or itself."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            return False
    return bool(addresses)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skipping = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)

    def text(self):
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        # Menus and buttons leave many very short lines; keep the ones that read like prose
        prose = "\n".join(line for line in lines if len(line) > 40)
        if len(prose) >= 300:
            return prose
        # A release table or a list of versions is all short lines, and is exactly what some questions
        # need; when the strict pass found almost nothing, keep the shorter lines too
        return "\n".join(line for line in lines if len(line) > 3)


async def _page_text(client, url):
    host = urlparse(url).hostname
    if not host or not await asyncio.to_thread(_is_public_host, host):
        return None
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                return None
            # A redirect here means _follow's HEAD saw none; skip rather than follow it unchecked
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > PAGE_BYTES:
                    break
            encoding = response.encoding or "utf-8"
    except httpx.HTTPError:
        return None
    parser = _TextExtractor()
    try:
        parser.feed(body.decode(encoding, errors="replace"))
    except Exception:
        return None
    return parser.text()[:PAGE_CHARS] or None


async def _follow(client, url, hops=3):
    """Resolves redirects one at a time, refusing any that lead to a private address."""
    for _ in range(hops):
        host = urlparse(url).hostname
        if not host or not await asyncio.to_thread(_is_public_host, host):
            return None
        try:
            response = await client.head(url)
        except httpx.HTTPError:
            return url  # some servers reject HEAD; the GET is checked again anyway
        if response.is_redirect and response.headers.get("location"):
            url = str(response.url.join(response.headers["location"]))
            continue
        return url
    return None


async def fetch_pages(results):
    """Reads the top pages, and puts the ones that could actually be read first.

    A page built entirely in the browser gives nothing back, and a source with nothing but a search
    summary behind it is what leaves an answer quoting a line written years ago. Every candidate is
    tried, and a source that yielded no text is dropped while enough others remain.
    """
    wanted = settings.search_fetch_pages()
    if not wanted or not results:
        return results
    async with httpx.AsyncClient(timeout=PAGE_TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html"}) as client:
        async def one(result):
            final = await _follow(client, result["url"])
            return await _page_text(client, final) if final else None

        texts = await asyncio.gather(*(one(r) for r in results), return_exceptions=True)

    read, unread = [], []
    for result, text in zip(results, texts):
        if isinstance(text, str) and len(text) >= 200 and len(read) < wanted:
            result["content"] = text
            read.append(result)
        else:
            unread.append(result)
    # Keep the order the ranking chose within each group, and never return nothing at all
    return (read + unread)[:len(results)] if read else results


def wants_recent(query, message=""):
    """Whether the question is about what is current, which is when freshness should outweigh rank."""
    return bool(RECENCY_WORDS.search(f"{query} {message}"))


def sources_prompt(results, query):
    """System message that gives the model the sources and asks for citations."""
    blocks = []
    for number, result in enumerate(results, 1):
        body = result.get("content") or result.get("snippet") or ""
        published = f"\nPublished: {result['published']}" if result.get("published") else ""
        # The site and date are part of the source: they are how the model tells a primary source
        # from a copy, and a current page from one written years ago
        blocks.append(f"[{number}] {result['title']}\nSite: {result.get('site', '')}\n"
                      f"URL: {result['url']}{published}\n{body}")
    # Without today's date a model reads anything after its training as science fiction and says the
    # sources are set in the future; it has to be told that they are current and that it is not
    today = datetime.date.today().strftime("%A, %d %B %Y")
    return (
        f"Today is {today}. A web search for \"{query}\" returned the sources below. They were "
        "published before today and are more up to date than your own knowledge: the versions, "
        "releases and events in them are real and already in the past, however recent they look to "
        "you. Never call them future or hypothetical, and never refuse to answer because of their "
        "dates.\n\n"
        "Use them to answer the user's latest message, formatted in Markdown. Cite sources inline "
        "with their numbers in square brackets, like [1] or [2][3].\n\n"
        "The sources are not equally good. Weigh them:\n"
        "- Prefer whoever publishes the thing itself (its own site, documentation, release notes or "
        "blog) over anyone writing about it.\n"
        "- When two sources disagree about what is newest or current, go with the most recent and "
        "most official one, say which you followed, and do not present the older claim as current.\n"
        "- A page can be years out of date while still being online, and a search summary can be "
        "older than the page it came from. Trust a date written in the text over your impression.\n"
        "- If the sources do not answer the question, say so and answer from your own knowledge, "
        "making clear which parts are not from the sources.\n\n" + "\n\n".join(blocks)
    )


QUERY_PROMPT = (
    "Write one web search query that would find the information needed to answer the user's latest "
    "message, taking the conversation into account. Reply with the query only: no quotes, no explanation."
)


def clean_query(text, fallback):
    lines = [line for line in re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).splitlines() if line.strip()]
    query = lines[0].strip().strip("\"'`").strip() if lines else ""
    # Small models like to dress a query up as a command ("/google search ...", "!go ...", "Search:"),
    # and a search engine takes those literally
    query = re.sub(r"^(?:\s*(?:/\w+|!\w+|google[:\s]+|query\s*:|search(?:\s+for|\s*:)))+\s*",
                   "", query, flags=re.I).strip()
    query = re.sub(r"[-_]{1,}", " ", query)      # postgresql-latest-release is not a phrase
    query = re.sub(r"\s{2,}", " ", query).strip(" -:\"'")
    return query[:200] if len(query) >= 2 else fallback[:200]
