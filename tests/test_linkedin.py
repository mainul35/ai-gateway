"""app/tools/linkedin.py on small made-up pages shaped like LinkedIn's: which links a message has, and
what is read from a job's guest page, a post's structured data and an article's text blocks. No network.

    python -m pytest -q tests/test_linkedin.py
"""
import json

import pytest

from app.tools import linkedin


def test_links_jobs_posts_and_articles_in_order():
    text = ("look at https://de.linkedin.com/jobs/view/backend-engineer-at-acme-1234567890?trk=x and "
            "https://www.linkedin.com/posts/someone_topic-activity-7400000000000000001-AbCd?utm_source=share "
            "then https://www.linkedin.com/pulse/a-title-someone-abcde?tl=ja")
    assert linkedin.links(text) == [
        {"kind": "job", "id": "1234567890", "url": "https://www.linkedin.com/jobs/view/1234567890/"},
        {"kind": "post", "id": "7400000000000000001",
         "url": "https://www.linkedin.com/posts/someone_topic-activity-7400000000000000001-AbCd"},
        {"kind": "post", "url": "https://www.linkedin.com/pulse/a-title-someone-abcde"},
    ]


def test_links_same_page_twice_counts_once():
    text = ("https://www.linkedin.com/jobs/view/1234567890 "
            "https://www.linkedin.com/jobs/search/?keywords=x&currentJobId=1234567890 "
            "https://www.linkedin.com/posts/a_b-activity-7400000000000000001-xy "
            "https://www.linkedin.com/feed/update/urn:li:activity:7400000000000000001/")
    assert [link["kind"] for link in linkedin.links(text)] == ["job", "post"]


def test_links_ignores_other_sites_and_profiles():
    assert linkedin.links("https://www.linkedin.com/in/someone/ https://example.com/jobs/view/1234567") == []
    assert linkedin.links(None) == []
    assert linkedin.links([{"type": "text"}]) == []


def test_links_at_most_three():
    text = " ".join(f"https://www.linkedin.com/jobs/view/{n}" for n in range(1000000, 1000005))
    assert len(linkedin.links(text)) == linkedin.MOST_LINKS


JOB_PAGE = """<html><body><section class="top-card-layout">
<h2 class="top-card-layout__title topcard__title">Backend Engineer</h2>
<span class="topcard__flavor"><a class="topcard__org-name-link" href="#"> Acme GmbH </a></span>
<span class="topcard__flavor topcard__flavor--bullet"> Berlin, Germany </span>
<span class="posted-time-ago__text"> 2 weeks ago </span>
<figure class="topcard__flavor--bullet"><figcaption>Over 100 applicants</figcaption></figure>
</section>
<div class="show-more-less-html__markup"><strong>What you do<br><br></strong><ul><li>Build APIs</li>
<li>Run them &amp; fix them</li></ul><p>We offer a team.</p></div>
<ul><li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader"> Seniority level </h3>
<span class="description__job-criteria-text"> Mid-Senior level </span></li>
<li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader"> Employment type </h3>
<span class="description__job-criteria-text"> Full-time </span></li></ul></body></html>"""


def test_parse_job():
    job = linkedin.parse_job(JOB_PAGE, "https://www.linkedin.com/jobs/view/1234567890/")
    assert job["title"] == "Backend Engineer"
    assert job["company"] == "Acme GmbH"
    assert job["location"] == "Berlin, Germany"
    assert job["posted"] == "2 weeks ago"
    assert job["details"] == [("Seniority level", "Mid-Senior level"), ("Employment type", "Full-time")]
    assert job["text"] == "What you do\n\n- Build APIs\n- Run them & fix them\n\nWe offer a team."
    source = linkedin.as_source(job)
    assert source["title"] == "Backend Engineer - Acme GmbH (LinkedIn job)"
    assert "Employment type: Full-time" in source["content"]
    assert source["content"].endswith("We offer a team.")


def test_parse_job_without_description():
    with pytest.raises(linkedin.LinkedInError, match="no description"):
        linkedin.parse_job("<html><h2 class='top-card-layout__title'>X</h2></html>", "u")
    with pytest.raises(linkedin.LinkedInError, match="login"):
        linkedin.parse_job("<html><div class='authwall'></div></html>", "u")


def _page(data, extra=""):
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head>{extra}</html>'


def test_parse_post():
    page = _page({"@type": "DiscussionForumPosting", "headline": "Big news",
                  "articleBody": "Line one &amp; more\n\n\n\nLine two", "author": {"name": "Some One"},
                  "datePublished": "2026-01-09T07:30:24.071Z"})
    post = linkedin.parse_post(page, "https://www.linkedin.com/posts/x")
    assert (post["form"], post["author"], post["published"], post["title"]) == ("post", "Some One", "09 January 2026",
                                                                                 "Big news")
    assert post["text"] == "Line one & more\n\nLine two"
    assert linkedin.as_source(post)["title"] == "Post by Some One: Big news"


def test_parse_article_from_its_text_blocks():
    page = _page({"@graph": [{"@type": "Article", "name": "The Title", "headline": "First sentence.",
                              "author": [{"name": "Writer"}], "datePublished": "2018-09-20"}]},
                 '<div class="article-main__content"><p>First paragraph.</p></div>'
                 '<div class="article-main__content"><h2>Part</h2><p>Second.</p></div>')
    post = linkedin.parse_post(page, "u")
    assert (post["form"], post["title"], post["author"]) == ("article", "The Title", "Writer")
    assert post["text"] == "First paragraph.\n\nPart\n\nSecond."
    assert linkedin.as_source(post)["title"] == "Article by Writer: The Title"


def test_parse_post_not_public():
    with pytest.raises(linkedin.LinkedInError, match="not public"):
        linkedin.parse_post('<html><a href="/authwall">Sign in to view</a></html>', "u")
    with pytest.raises(linkedin.LinkedInError, match="no post text"):
        linkedin.parse_post(_page({"@type": "Organization", "name": "X"}), "u")


def test_prompt_numbers_pages_and_names_failures():
    text = linkedin.prompt([{"title": "A", "url": "https://a", "content": "body", "published": "1 May 2025"}],
                           [("https://b", "the post is not public")])
    assert "[1] A\nURL: https://a\nPublished: 1 May 2025\nbody" in text
    assert "- https://b: the post is not public" in text
    assert "do not guess" in text
    assert linkedin.prompt([], []) == ""


def test_short_links_are_expanded_to_the_post(monkeypatch):
    import asyncio

    async def expand(short):
        return {"https://lnkd.in/p/abc": "https://www.linkedin.com/posts/x_y-activity-7400000000000000009-Zz?utm_source=share",
                "https://lnkd.in/elsewhere": "https://example.com/page"}[short]
    monkeypatch.setattr(linkedin, "_expand", expand)
    found = asyncio.run(linkedin.links_in("about https://lnkd.in/p/abc. and https://lnkd.in/elsewhere"))
    assert found[0] == {"kind": "post", "id": "7400000000000000009",
                        "url": "https://www.linkedin.com/posts/x_y-activity-7400000000000000009-Zz"}
    assert found[1]["url"] == "https://lnkd.in/elsewhere" and "does not lead" in found[1]["error"]
    with pytest.raises(linkedin.LinkedInError, match="does not lead"):
        asyncio.run(linkedin.read(found[1]))


def test_a_short_link_that_cannot_be_followed_is_reported(monkeypatch):
    import asyncio

    async def expand(short):
        raise linkedin.LinkedInError("the short link did not answer (ConnectTimeout)")
    monkeypatch.setattr(linkedin, "_expand", expand)
    assert asyncio.run(linkedin.links_in("https://lnkd.in/p/abc")) == [
        {"kind": "post", "url": "https://lnkd.in/p/abc", "error": "the short link did not answer (ConnectTimeout)"}]
    assert asyncio.run(linkedin.links_in(None)) == []
