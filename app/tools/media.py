"""Pictures and videos of things that really exist, found on the web rather than drawn.

Asked to show the drink it had just recommended, the gateway drew one: a photograph-shaped picture
of a bottle that has never existed, with a made-up label, which answers the question "what might
this look like" and not the question that was asked. A real product, a real building, a real animal
or a real event has been photographed already, and the honest answer is to go and find that
photograph and say where it came from.

Three sources, in order of how much they can be trusted to be about the right thing:

1. The image and video search engines behind SearXNG, which is what the person would have used.
2. The pages the ordinary web search returns, scraped for the picture they lead with - their
   og:image, which is the publisher's own choice of what the page is a picture of.
3. Nothing. A picture nobody has taken is not produced here; the gateway says it found none.

Everything that survives is fetched, opened and measured before it is shown, because a search
result is a claim that a URL is a picture and a good few of those claims are wrong: expired CDN
links, hotlink blocks, tracking pixels and placeholder graphics all come back with a cheerful 200.
The bytes are then kept by the gateway and served from there, so that a page reopened next year
still shows what it showed today, the browser never announces the reader to a dozen third-party
CDNs, and no image is missing because somebody else's server now refuses it.

What is never claimed is that the picture is of the right thing. These are search results, so each
one is shown with the site it came from and a link to the page that carried it, and the reader can
see at a glance whether the source is the manufacturer or a stock library.
"""
import asyncio
import hashlib
import io
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app import settings
from app.tools import web_search

log = logging.getLogger("tools.media")

IMAGES, VIDEOS = "images", "videos"

CANDIDATES = 24         # search results looked at per subject, before any are fetched
KEEP = 6                # pictures shown per subject
MAX_SUBJECTS = 3        # separate things one message may ask to see
FETCH_TIMEOUT = 8       # one picture
BUDGET = 30             # all of them together, so a slow CDN cannot hold up the answer
MAX_BYTES = 8_000_000
SMALLEST = 200          # a side shorter than this is an icon, a logo or a tracking pixel
# A video still is small by nature: the engines hand back 320x180 and 354x199, and holding
# those to the picture floor threw away five videos out of six.
THUMB_SMALLEST = 120
BIGGEST = 1400          # longest side kept; anything larger is scaled down before it is stored
SCRAPE_PAGES = 5        # pages read when the image engines came back with nothing

# Sites whose image results are a login wall, a watermark, or a picture of a different thing
UNHELPFUL = ("lookaside.fbsbx.com", "lookaside.instagram.com", "pinimg.com", "gettyimages.",
             "shutterstock.com", "alamy.com", "dreamstime.com", "123rf.com", "istockphoto.com")


class MediaError(Exception):
    pass


def is_available():
    return bool(settings.searxng_url())


# --- what is being asked for ---------------------------------------------------------------------

VIDEO_WORDS = re.compile(r"\b(video|videos|clip|clips|footage|film|trailer|advert|advertisement|"
                         r"commercial|youtube|watch it|moving)\b", re.I)
# "show me", "find me", "what does it look like" - wanting to see, as opposed to wanting one made
LOOK_WORDS = re.compile(
    r"\b(show|find|get|fetch|search|look ?up|see|send)\b[^.?!]{0,30}"
    r"\b(photos?|pictures?|images?|videos?|clips?|footage|pics?|screenshots?)\b"
    r"|\bwhat (does|do|did)\b[^.?!]{0,40}\blook like\b"
    r"|\b(real|actual|genuine|original)\b[^.?!]{0,15}\b(photos?|pictures?|images?|footage)\b"
    # "can I see the actual product": no picture word anywhere, and unmistakably about the thing
    r"|\b(?:the\s+)?(real|actual|genuine|original)\s+"
    r"(thing|product|item|one|ones|packaging|packet|bottle|box|label|model)\b"
    r"|\b(photos?|pictures?|images?|videos?) of (the|this|that|it|them|him|her|these|those)\b", re.I)
# Words that mean the picture is to be invented, which settles it whatever else the message says
MADE_UP_WORDS = re.compile(r"\b(draw|sketch|paint|illustrate|imagine|render|generate|create)\b", re.I)
# What separates "show me the photos of the drinks you suggested" from "show me a nice nature
# photo": the first points at one particular thing that exists and has been photographed, the
# second describes a kind of picture and is a perfectly good thing to ask an image model for. The
# words for both are otherwise identical, so this is the whole difference.
DEFINITE = re.compile(
    r"\b(?:of|about|see|like)\s+(?:the|that|those|these|this|it|them|his|her|their|your|my)\b"
    r"|\byou\s+(?:just\s+)?(?:\w+\s+)?(?:suggest\w*|recommend\w*|mention\w*|said|listed|named|"
    r"describ\w*|talked about)\b"
    r"|\bthe\s+(?:one|ones|first|second|third|same|above)\b"
    r"|\blook(?:s|ed|ing)?\s+like\b", re.I)
# A brand or a model name: capitalised where a sentence would not capitalise, or letters and
# digits run together. Not a bare number - "show me 3 photos of a sunset" names nothing.
NAMED = re.compile(r"(?!^)\b[A-Z][A-Za-z'’-]{2,}\b|\b[A-Za-z]+\d+[A-Za-z0-9]*\b|\b\d+[A-Za-z]{2,}\b")


def points_at_something(text):
    """Whether the message is about one particular real thing, rather than a kind of picture."""
    text = (text or "").strip()
    if DEFINITE.search(text):
        return True
    # The first word is capitalised because it starts the sentence, which says nothing
    rest = text.split(None, 1)[1] if " " in text else ""
    return bool(NAMED.search(rest))


def wants_video(text):
    return bool(VIDEO_WORDS.search(text or ""))


def kind_of(text):
    return VIDEOS if wants_video(text) else IMAGES


SUBJECTS_PROMPT = """The user wants to be shown a real photograph or video of something, rather
than a picture made up for them. Read the conversation and say what to search for.

Answer with one line per thing, at most three lines, and nothing else. No numbering, no bullets, no
explanation, no quotes.

Each line is what you would type into an image search to find that exact thing: its brand and its
name and what kind of thing it is, spelled out in full. The user's words are often a reference to
something said earlier - "the drinks you suggested", "that model", "the one you mentioned" - and a
line that repeats the reference instead of resolving it finds nothing. Name the thing.

If the message asks about several things, give one line each, most important first. If it asks
about one thing, give one line.

Examples:
  earlier: "Pick up a bottle of Contrex and a pack of plain almonds."
  message: "show me the photos of the drinks you are suggesting"
  -> Contrex mineral water bottle
  -> plain almonds pack

  message: "what does a Fairphone 5 look like"
  -> Fairphone 5 smartphone

  message: "show me a video of the Shinkansen leaving Tokyo station"
  -> Shinkansen departing Tokyo station"""


def build_request(model, message, history=()):
    """Asks the small model what the words are actually pointing at."""
    recent = "\n".join(f"{m['role']}: {(m.get('content') or '')[:700]}" for m in list(history)[-6:])
    context = f"Conversation so far:\n{recent}\n\n" if recent else ""
    return {
        "model": model, "stream": False, "temperature": 0, "max_tokens": 200,
        "reasoning_effort": "none",
        "messages": [{"role": "system", "content": SUBJECTS_PROMPT},
                     {"role": "user", "content": f"{context}Message: {message}\n\nSearch for:"}],
    }


# Words the search box does not want: they describe the asking, not the thing
ASKING = re.compile(
    r"^\s*(?:(?:please|can you|could you|i want to|i would like to|i'?d like to|let me|"
    r"show|find|get|fetch|search|look ?up|see|give|send)\b\s*)+"
    r"(?:me|us|it|them)?\s*(?:the|a|an|some|any)?\s*"
    r"(?:real|actual|genuine|original)?\s*"
    r"(?:photos?|pictures?|images?|videos?|clips?|footage|pics?|screenshots?)?\s*"
    # The article belongs to the asking, not the thing: searching for "the Fairphone 5" is
    # searching for a page with the word the on it
    r"(?:of|for|about)?\s*(?:the|a|an|some)?\s*", re.I)


def plain_subject(message):
    """The user's own words with the asking stripped off; used when the model gives nothing."""
    text = ASKING.sub("", (message or "").strip())
    text = re.sub(r"[?!.]+\s*$", "", text).strip(" ,:;")
    return text[:120]


def read_subjects(answer, message):
    """The lines the model gave back, or the user's own words when it gave none worth having."""
    text = re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S)
    subjects = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("\"'`")
        line = re.sub(r"^(?:search(?: for)?|query|look up|find)\s*[:\-]\s*", "", line, flags=re.I)
        if len(line) < 3 or len(line) > 120 or line.endswith(":"):
            continue
        if line.lower() not in (s.lower() for s in subjects):
            subjects.append(line)
    fallback = plain_subject(message)
    if not subjects:
        return [fallback] if fallback else []
    return subjects[:MAX_SUBJECTS]


# --- finding candidates ---------------------------------------------------------------------------

def _host(url):
    name = (urlparse(url).hostname or "").lower()
    return name[4:] if name.startswith("www.") else name


async def _ask_searxng(query, category):
    if not settings.searxng_url():
        raise MediaError("No search service is configured, so there is nowhere to look.")
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.get(
                f"{settings.searxng_url()}/search",
                params={"q": query, "format": "json", "safesearch": 1, "categories": category},
                headers={"X-Real-IP": "127.0.0.1"})
            response.raise_for_status()
            return response.json().get("results") or []
    except (httpx.HTTPError, ValueError) as e:
        raise MediaError(f"the image search did not answer ({e.__class__.__name__})") from e


async def find_images(subject, limit=CANDIDATES):
    """Candidate pictures of one thing: where the file is, and the page that carried it."""
    found, seen = [], set()
    for result in await _ask_searxng(subject, IMAGES):
        source = result.get("img_src") or ""
        if not source.startswith(("http://", "https://")) or source in seen:
            continue
        if any(site in _host(source) for site in UNHELPFUL):
            continue
        seen.add(source)
        found.append({
            "src": source,
            "page": result.get("url") or source,
            "title": re.sub(r"\s+", " ", str(result.get("title") or "")).strip()[:200],
            "site": _host(result.get("url") or source),
            "found_by": "image search",
        })
        if len(found) >= limit:
            break
    return found


def _how_long(result):
    """How long the video runs, as something to print.

    The engines are not consistent: some send "3:30:04", some send a number of seconds, and some
    send nothing. A float once went straight into a slice and took the whole search down with it.
    """
    for value in (result.get("length"), result.get("duration")):
        if isinstance(value, str) and value.strip():
            return value.strip()[:16]
        if isinstance(value, (int, float)) and value > 0:
            seconds = int(value)
            hours, rest = divmod(seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    return ""


YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([\w-]{11})")


def _still(result, page):
    """Where to get the picture that stands in for the video.

    Most results are YouTube, and most of the thumbnails the engines hand back for them are on a
    search engine's own CDN, which refuses a request that did not come from its own results page.
    YouTube's still for a video is at a predictable address and is served to anyone, so when the
    video is a YouTube one that address is used instead: it took the videos kept from one in six
    to most of them.
    """
    found = YOUTUBE.search(page or "")
    if found:
        return f"https://i.ytimg.com/vi/{found.group(1)}/hqdefault.jpg"
    return result.get("thumbnail") or result.get("img_src") or ""


async def find_videos(subject, limit=CANDIDATES // 2):
    """Candidate videos: the page to watch on, and a thumbnail to show meanwhile."""
    found, seen = [], set()
    for result in await _ask_searxng(subject, VIDEOS):
        page = result.get("url") or ""
        if not page.startswith(("http://", "https://")) or page in seen:
            continue
        seen.add(page)
        found.append({
            "src": _still(result, page),
            "page": page,
            "title": re.sub(r"\s+", " ", str(result.get("title") or "")).strip()[:200],
            "site": _host(page),
            "length": _how_long(result),
            "author": str(result.get("author") or "")[:80],
            # Only the players that offer a privacy-preserving embed are ever framed
            "embed": result.get("iframe_src") if "nocookie" in (result.get("iframe_src") or "") else "",
            "found_by": "video search",
        })
        if len(found) >= limit:
            break
    return found


class _Pictures(HTMLParser):
    """What a page says it is a picture of, and the pictures in it, in that order of trust.

    og:image is the publisher's own answer to "what does this page show", which is exactly the
    question being asked, so it is worth more than any number of images scraped out of the body.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.leading, self.body = [], []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content") or ""
            if name in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image",
                        "twitter:image:src") and content:
                self.leading.append(content)
        elif tag in ("img", "source"):
            source = values.get("src") or (values.get("srcset") or "").split()[0:1]
            source = source if isinstance(source, str) else (source[0] if source else "")
            # A thumbnail in a sidebar is not what the page is about; the size attributes, when
            # they are there at all, are the only hint available before anything is fetched
            try:
                small = (int(values.get("width", 999)) < SMALLEST
                         or int(values.get("height", 999)) < SMALLEST)
            except ValueError:
                small = False
            if source and not small:
                self.body.append(source)

    def urls(self, base):
        out, seen = [], set()
        for found in self.leading + self.body:
            full = urljoin(base, found.strip())
            if full.startswith(("http://", "https://")) and full not in seen:
                seen.add(full)
                out.append(full)
        return out


async def scrape_pages(subject, limit=CANDIDATES):
    """The picture each of the top pages leads with, for the things image search does not index.

    A local shop, a small manufacturer or anything written about in one language and asked about in
    another can be entirely absent from an image index while its own website has a photograph of it
    on the front page.
    """
    try:
        results = await web_search.search([subject], limit=SCRAPE_PAGES)
    except web_search.SearchError as e:
        log.info("no pages to scrape for %r (%s)", subject, e)
        return []
    found = []
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": web_search.USER_AGENT}) as client:
        async def one(result):
            url = result.get("url") or ""
            host = urlparse(url).hostname
            if not host or not await asyncio.to_thread(web_search.is_public_host, host):
                return []
            try:
                response = await client.get(url)
                if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                    return []
                parser = _Pictures()
                parser.feed(response.text[:400_000])
            except (httpx.HTTPError, ValueError, AssertionError):
                return []
            except Exception as e:                       # a broken page must not end the search
                log.info("could not read %s (%s)", url, e.__class__.__name__)
                return []
            return [{"src": source, "page": url, "site": _host(url),
                     "title": str(result.get("title") or "")[:200], "found_by": "the page itself"}
                    for source in parser.urls(url)[:4]]

        for group in await asyncio.gather(*(one(r) for r in results), return_exceptions=True):
            if isinstance(group, list):
                found += group
    return found[:limit]


# --- fetching, checking and shrinking ---------------------------------------------------------------

def _shrink(data, smallest=SMALLEST):
    """Opens the picture to prove it is one, drops the tiny ones, and scales the rest down.

    Returns (bytes, mime, width, height), or None when it is not a usable picture. Opening it is
    the check: a tracking pixel, an HTML error page served as image/jpeg and a truncated download
    all fail here, and every one of those has come back from a search result with a 200.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except Exception:
        return None
    if picture.width < smallest or picture.height < smallest:
        return None
    transparent = picture.mode in ("RGBA", "LA", "P") and "transparency" in picture.info
    picture = ImageOps.exif_transpose(picture)
    if max(picture.width, picture.height) > BIGGEST:
        picture.thumbnail((BIGGEST, BIGGEST), Image.LANCZOS)
    out = io.BytesIO()
    if transparent:
        picture.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png", picture.width, picture.height
    picture.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue(), "image/jpeg", picture.width, picture.height


async def fetch(client, url, smallest=SMALLEST):
    """One picture, if that is what is really there. Never follows a link into this machine."""
    host = urlparse(url).hostname
    if not host or not await asyncio.to_thread(web_search.is_public_host, host):
        return None
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind and not kind.startswith("image/"):
                return None
            data = b""
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_BYTES:
                    return None
    except httpx.HTTPError:
        return None
    except Exception as e:
        log.info("could not fetch %s (%s)", url[:120], e.__class__.__name__)
        return None
    return await asyncio.to_thread(_shrink, data, smallest) if data else None


async def collect(candidates, keep=KEEP, budget=BUDGET, smallest=SMALLEST):
    """Fetches candidates in order and keeps the first `keep` that turn out to be real pictures.

    In order, because the search engines' ranking is the best evidence available about which
    picture is of the right thing, and a fetch that fails must cost the next one its place rather
    than the whole answer its ranking. Duplicates are dropped by what the bytes are, not by where
    they came from: the same press photograph comes back from five sites at five URLs.
    """
    kept, seen = [], set()
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": web_search.USER_AGENT,
                                          "Accept": "image/*,*/*"}) as client:
        remaining = budget

        async def group(batch):
            return await asyncio.gather(*(fetch(client, c["src"], smallest) for c in batch),
                                        return_exceptions=True)

        # A few at a time: enough to hide one slow server behind the others, few enough that a
        # page of twenty-four results is not all downloaded to show six.
        for start in range(0, len(candidates), 4):
            if len(kept) >= keep or remaining <= 0:
                break
            batch = candidates[start:start + 4]
            began = asyncio.get_running_loop().time()
            try:
                results = await asyncio.wait_for(group(batch), timeout=remaining)
            except asyncio.TimeoutError:
                log.info("ran out of time fetching pictures after %d", len(kept))
                break
            remaining -= asyncio.get_running_loop().time() - began
            for candidate, result in zip(batch, results):
                if not isinstance(result, tuple) or len(kept) >= keep:
                    continue
                data, mime, width, height = result
                fingerprint = hashlib.sha256(data).hexdigest()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                kept.append({**candidate, "data": data, "mime": mime,
                             "width": width, "height": height})
    return kept
