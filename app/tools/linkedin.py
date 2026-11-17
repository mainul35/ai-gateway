"""LinkedIn links in a chat message, read so the model can work with what they point at.

A job (linkedin.com/jobs/view/..., or a search page open on one: ?currentJobId=...) and a post
(linkedin.com/posts/..., /feed/update/urn:li:..., /pulse/... articles) are both public pages that
LinkedIn serves without a login, and that is all that is read here: no account, no cookies, one page
per link the user pasted, through app.net like every other outside fetch. A post its author shared
with connections only needs a login, and is reported as not public rather than guessed at.

Jobs come from the guest job page (jobs-guest/jobs/api/jobPosting/<id>): the same top card and
description as the full page in a tenth of the bytes. Posts and articles carry their text, author and
date as structured data (ld+json, DiscussionForumPosting or Article), which is steadier than LinkedIn's
CSS class names.
"""
import datetime
import html
import json
import logging
import re
from html.parser import HTMLParser

import httpx

from app import net, settings
from app.tools import web_search

log = logging.getLogger("tools.linkedin")

TIMEOUT = 10
PAGE_BYTES = 2_000_000      # a post's page is about 370 KB, a job's guest page about 50 KB
TEXT_CHARS = 20_000         # text kept of one job description or post
MOST_LINKS = 3              # links read from one message

_HOST = r"https?://(?:[a-z]{2,3}\.|www\.)?linkedin\.com"
JOB = re.compile(_HOST + r"/jobs/view/(?:[^/?#\s]*?-)?(\d{6,})", re.I)
JOB_IN_SEARCH = re.compile(_HOST + r"/jobs/[^\s#]*?[?&]currentJobId=(\d{6,})", re.I)
POST = re.compile(_HOST + r"/(?:posts/[^\s?#]+|feed/update/urn:li:(?:activity|share|ugcPost):\d+/?|pulse/[^\s?#]+)",
                  re.I)


class LinkedInError(Exception):
    """A link that could not be read, with a reason a person can follow."""


def is_enabled():
    return settings.get_bool("linkedin.enabled", "LINKEDIN_ENABLED", True)


def links(text):
    """The LinkedIn jobs and posts a message links to, in order, at most MOST_LINKS:
    [{"kind": "job", "id": ..., "url": ...} | {"kind": "post", "url": ...}]."""
    if not isinstance(text, str):
        return []
    found = []
    for pattern, kind in ((JOB, "job"), (JOB_IN_SEARCH, "job"), (POST, "post")):
        for m in pattern.finditer(text):
            if kind == "job":
                item = {"kind": "job", "id": m.group(1), "url": f"https://www.linkedin.com/jobs/view/{m.group(1)}/",
                        "at": m.start()}
            else:
                # Always the www host over https, without the tracking query: the same page for everyone
                path = re.sub(r"^" + _HOST, "", m.group(0), flags=re.I)
                item = {"kind": "post", "url": "https://www.linkedin.com" + path, "at": m.start()}
                # /posts/...-activity-<n>-xxxx and /feed/update/urn:li:activity:<n> are the same post
                activity = re.search(r"activity[-:](\d{10,})", path)
                if activity:
                    item["id"] = activity.group(1)
            if not any(f["url"] == item["url"] or ("id" in item and f.get("id") == item["id"]) for f in found):
                found.append(item)
    found.sort(key=lambda f: f["at"])
    for f in found:
        del f["at"]
    return found[:MOST_LINKS]


async def _fetch(url):
    async with net.client(timeout=TIMEOUT, headers={"User-Agent": web_search.USER_AGENT,
                                                    "Accept": "text/html",
                                                    "Accept-Language": "en"}) as client:
        try:
            page = await net.get(client, url, max_bytes=PAGE_BYTES, truncate=True)
        except httpx.HTTPError as e:
            raise LinkedInError(f"LinkedIn did not answer ({type(e).__name__})")
        except net.Refused as e:
            raise LinkedInError(str(e))
    if page.status_code in (429, 999):
        # 999 is LinkedIn's own "too many requests from you"
        raise LinkedInError("LinkedIn is refusing requests from this server for now; try again later")
    if page.status_code == 404:
        raise LinkedInError("LinkedIn says it does not exist (removed, or the link is wrong)")
    if page.status_code != 200:
        raise LinkedInError(f"LinkedIn answered {page.status_code}")
    return page


class _Picker(HTMLParser):
    """The text of the elements whose class names include one of `wanted`, as lines, by that name.
    Block elements and <br> end a line; lists keep a bullet, so a description keeps its shape."""

    BLOCKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}
    VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed"}

    def __init__(self, wanted):
        super().__init__(convert_charrefs=True)
        self.wanted = wanted
        self.found = {name: [] for name in wanted}
        self.open = []              # [name, depth, parts] of each wanted element we are inside
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            for item in self.open:
                item[2].append("\n- " if tag == "li" else "\n")
        if tag in self.VOID:
            return
        self.depth += 1
        classes = (dict(attrs).get("class") or "").split()
        for name in self.wanted:
            if name in classes:
                self.open.append([name, self.depth, []])

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if tag in self.BLOCKS and tag != "li":      # the next item's own "- " line follows a list item
            for item in self.open:
                item[2].append("\n")
        while self.open and self.open[-1][1] == self.depth:
            name, _, parts = self.open.pop()
            self.found[name].append(_lines("".join(parts)))
        self.depth -= 1

    def handle_data(self, data):
        for item in self.open:
            item[2].append(data)


def _lines(text):
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", line).strip() for line in text.splitlines()]
    out = []
    for line in lines:
        if line in ("", "-"):
            if out and out[-1] != "":
                out.append("")
            continue
        if line.startswith("- ") and len(out) > 1 and out[-1] == "" and out[-2].startswith("- "):
            out.pop()                               # one list stays one list
        out.append(line)
    return "\n".join(out).strip()


JOB_PARTS = ("top-card-layout__title", "topcard__org-name-link", "topcard__flavor--bullet",
             "posted-time-ago__text", "description__job-criteria-subheader", "description__job-criteria-text",
             "show-more-less-html__markup")


def parse_job(page_html, url):
    """A job's facts and description from its guest page, or LinkedInError."""
    picker = _Picker(JOB_PARTS)
    try:
        picker.feed(page_html)
    except Exception:
        raise LinkedInError("the job page could not be read")
    got = picker.found
    title = next(iter(got["top-card-layout__title"]), "")
    description = next(iter(got["show-more-less-html__markup"]), "")
    if not description:
        if not title and "authwall" in page_html:
            raise LinkedInError("LinkedIn asked for a login to show this job")
        raise LinkedInError("the job page has no description (closed, or LinkedIn changed its page)")
    # The first bullet is the location; the other is the applicant count
    location = next(iter(got["topcard__flavor--bullet"]), "")
    details = list(zip(got["description__job-criteria-subheader"], got["description__job-criteria-text"]))
    return {"kind": "job", "url": url, "title": title, "company": next(iter(got["topcard__org-name-link"]), ""),
            "location": location, "posted": next(iter(got["posted-time-ago__text"]), ""),
            "details": [(k, v) for k, v in details if k and v], "text": description[:TEXT_CHARS]}


def _structured(page_html):
    """Every ld+json object of a page, flattened out of lists and @graph."""
    out = []
    for m in re.finditer(r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page_html, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        todo = [data]
        while todo:
            item = todo.pop(0)
            if isinstance(item, list):
                todo.extend(item)
            elif isinstance(item, dict):
                out.append(item)
                if isinstance(item.get("@graph"), list):
                    todo.extend(item["@graph"])
    return out


POST_TYPES = {"DiscussionForumPosting", "SocialMediaPosting", "Article", "NewsArticle", "BlogPosting"}


def _name(value):
    if isinstance(value, dict):
        return value.get("name") or ""
    if isinstance(value, list) and value:
        return _name(value[0])
    return value if isinstance(value, str) else ""


def _article_text(page_html):
    """An article's (/pulse/) body: its structured data leaves it out, and the page has it in blocks."""
    picker = _Picker(("article-main__content",))
    try:
        picker.feed(page_html)
    except Exception:
        return ""
    return "\n\n".join(block for block in picker.found["article-main__content"] if block)


def parse_post(page_html, url):
    """A post's or article's author, date and text from its page, or LinkedInError."""
    for item in _structured(page_html):
        kinds = item.get("@type")
        kinds = set(kinds) if isinstance(kinds, list) else {kinds}
        if not kinds & POST_TYPES:
            continue
        body = item.get("articleBody") or item.get("text") or ""
        body = _lines(html.unescape(body)) if isinstance(body, str) else ""
        article = bool(kinds & {"Article", "NewsArticle", "BlogPosting"})
        if not body.strip() and article:
            body = _article_text(page_html)
        if body.strip():
            published = item.get("datePublished") or ""
            try:
                published = datetime.datetime.fromisoformat(published.replace("Z", "+00:00")).strftime("%d %B %Y")
            except ValueError:
                pass
            # An article's headline is its first sentence; its name is its title
            title = (item.get("name") if article else None) or item.get("headline") or ""
            return {"kind": "post", "form": "article" if article else "post", "url": url,
                    "title": html.unescape(title if isinstance(title, str) else "")[:300],
                    "author": _name(item.get("author")), "published": published, "text": body[:TEXT_CHARS]}
    if "authwall" in page_html or "Sign in to view" in page_html:
        raise LinkedInError("the post is not public: LinkedIn shows it only to people who are signed in")
    raise LinkedInError("the page has no post text LinkedIn shows without a login")


async def read(link):
    """One link from links() read: the job's or post's facts and text. Raises LinkedInError."""
    if link["kind"] == "job":
        page = await _fetch(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{link['id']}")
        return parse_job(page.text, link["url"])
    page = await _fetch(link["url"])
    return parse_post(page.text, page.url if "linkedin.com" in page.url else link["url"])


def as_source(item):
    """A read link in the shape web_search results have, so both go to the model and the page alike."""
    if item["kind"] == "job":
        facts = [f"Job title: {item['title']}", f"Company: {item['company']}", f"Location: {item['location']}"]
        if item["posted"]:
            facts.append(f"Posted: {item['posted']}")
        facts += [f"{k}: {v}" for k, v in item["details"]]
        title = " - ".join(p for p in (item["title"], item["company"]) if p) or "LinkedIn job"
        content = "\n".join(f for f in facts if not f.endswith(": ")) + "\n\nJob description:\n" + item["text"]
        return {"title": f"{title} (LinkedIn job)", "url": item["url"], "site": "linkedin.com", "content": content}
    form = "Article" if item.get("form") == "article" else "Post"
    by = f"{form} by {item['author']}" if item["author"] else f"LinkedIn {form.lower()}"
    return {"title": f"{by}: {item['title']}" if item["title"] else by, "url": item["url"], "site": "linkedin.com",
            "published": item["published"], "content": item["text"]}


def prompt(sources, failures):
    """System message with the linked pages, numbered for citations, and what could not be read."""
    blocks = []
    for number, s in enumerate(sources, 1):
        published = f"\nPublished: {s['published']}" if s.get("published") else ""
        blocks.append(f"[{number}] {s['title']}\nURL: {s['url']}{published}\n{s['content']}")
    parts = []
    if blocks:
        parts.append("The user's message links to the LinkedIn pages below. They were fetched just now, and "
                     "their text is exactly what LinkedIn shows. Answer the user's latest message from them, "
                     "formatted in Markdown; cite them inline as [1], [2]. Do not add requirements, dates or "
                     "details the text does not contain.\n\n" + "\n\n".join(blocks))
    if failures:
        parts.append("These linked LinkedIn pages could not be read, so you do not know what they say. Tell "
                     "the user, with the reason, and do not guess their content:\n" +
                     "\n".join(f"- {url}: {reason}" for url, reason in failures))
    return "\n\n".join(parts)
