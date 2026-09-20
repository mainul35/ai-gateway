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

PAGE_TIMEOUT = 6
PAGE_BYTES = 1_500_000      # stop reading a page after this much HTML
PAGE_CHARS = 3000           # text kept per fetched page
SNIPPET_CHARS = 400
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


class SearchError(Exception):
    pass


def is_available():
    return settings.feature_enabled("web_search") and bool(settings.searxng_url())


async def search(query, limit=None):
    """Returns [{title, url, snippet}] from SearXNG."""
    limit = limit or settings.search_results()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{settings.searxng_url()}/search",
                                        params={"q": query, "format": "json", "safesearch": 1},
                                        # SearXNG's bot detection wants to know the client; it is us
                                        headers={"X-Real-IP": "127.0.0.1"})
            response.raise_for_status()
            results = response.json().get("results") or []
    except (httpx.HTTPError, ValueError) as e:
        raise SearchError(f"search service unavailable ({e.__class__.__name__})") from e

    seen, found = set(), []
    for result in results:
        url = result.get("url")
        if not url or url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        found.append({"title": (result.get("title") or url).strip(), "url": url,
                      "snippet": re.sub(r"\s+", " ", result.get("content") or "").strip()[:SNIPPET_CHARS]})
        if len(found) >= limit:
            break
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
        lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in "".join(self.parts).splitlines())
        # Menus and buttons leave many very short lines; keep the ones that read like prose
        return "\n".join(line for line in lines if len(line) > 40)


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
    """Adds the readable text of the top results as result["content"]."""
    count = min(settings.search_fetch_pages(), len(results))
    if not count:
        return results
    async with httpx.AsyncClient(timeout=PAGE_TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": USER_AGENT, "Accept": "text/html"}) as client:
        async def one(result):
            final = await _follow(client, result["url"])
            return await _page_text(client, final) if final else None

        texts = await asyncio.gather(*(one(r) for r in results[:count]), return_exceptions=True)
    for result, text in zip(results, texts):
        if isinstance(text, str):
            result["content"] = text
    return results


def sources_prompt(results, query):
    """System message that gives the model the sources and asks for citations."""
    blocks = []
    for number, result in enumerate(results, 1):
        body = result.get("content") or result.get("snippet") or ""
        blocks.append(f"[{number}] {result['title']}\nURL: {result['url']}\n{body}")
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
        "with their numbers in square brackets, like [1] or [2][3]. If the sources do not answer the "
        "question, say so and answer from your own knowledge, making clear which parts are not from "
        "the sources.\n\n" + "\n\n".join(blocks)
    )


QUERY_PROMPT = (
    "Write one web search query that would find the information needed to answer the user's latest "
    "message, taking the conversation into account. Reply with the query only: no quotes, no explanation."
)


def clean_query(text, fallback):
    lines = [line for line in re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).splitlines() if line.strip()]
    query = lines[0].strip().strip("\"'`").strip() if lines else ""
    return query[:200] if len(query) >= 2 else fallback[:200]
