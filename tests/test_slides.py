"""app/tools/slides.py: Markdown read as slides and built into a PowerPoint deck, plain or on a
template; and a deck read back as a document. Nothing is fetched.

    python -m pytest -q tests/test_slides.py
"""
import io
import zipfile

import pytest

pptx = pytest.importorskip("pptx")

from app.tools import documents, slides, written  # noqa: E402

DECK = """Here is your presentation:

# Quarterly review
Where we are and what comes next

## Results
- Revenue up **12%**
  - Mostly new customers
- Costs flat
Notes: Open with the good news.
Then the costs.

## Compared
| Option | Cost |
| --- | --- |
| A | Low |
| B | High |

## Growth
```chart
type: column
title: Sign-ups
x: Q1, Q2, Q3
---
Web    | 10, 20, 30
Mobile | 5, 8, 13
```
Mobile is catching up.

## Steps
""" + "\n".join(f"{n}. Step {n}" for n in range(1, 15)) + """

# Questions
"""


def test_split_reads_titles_slides_notes_and_shares_out_a_long_list():
    deck = slides.split(DECK)
    assert [(s["kind"], s["title"]) for s in deck] == [
        ("title", "Quarterly review"), ("content", "Results"), ("content", "Compared"), ("content", "Growth"),
        ("content", "Steps"), ("content", "Steps"), ("content", "Questions")]
    assert deck[0]["body"] == "Where we are and what comes next"
    assert deck[1]["notes"] == "Open with the good news.\nThen the costs." and "Notes" not in deck[1]["body"]
    # 14 steps: two slides of 7, the second counting on from 8
    first, second = deck[4]["body"].splitlines(), deck[5]["body"].splitlines()
    assert len(first) == 7 and second[0] == "8. Step 8" and second[-1] == "14. Step 14"
    assert deck[6]["body"] == ""


def test_markdown_without_headings_still_makes_a_deck():
    deck = slides.split("Just a paragraph of text about something.\n\n- one\n- two")
    assert deck[0]["kind"] == "title" and deck[1]["kind"] == "content" and "- one" in deck[1]["body"]


def test_a_heading_inside_a_code_block_is_not_a_slide():
    deck = slides.split("# T\n\n## A\n```bash\n# a comment\n## another\n```")
    assert [s["title"] for s in deck] == ["T", "A"] and "# a comment" in deck[1]["body"]


def test_chart_of_reads_mdcharts_form():
    chart = slides.chart_of("type: line\ntitle: T\nx: a, b, c\n---\nOne | 1, 2, 3\nTwo | 4, 5")
    assert chart["type"] == "line" and chart["categories"] == ["a", "b", "c"]
    assert chart["series"] == [("One", [1.0, 2.0, 3.0]), ("Two", [4.0, 5.0, None])]
    pie = slides.chart_of("type: pie\nFirst | 60\nSecond | 40%")
    assert pie["categories"] == ["First", "Second"] and pie["series"][0][1] == [60.0, 40.0]
    assert slides.chart_of("type: bar\nno numbers here") is None


def test_parts_tell_text_from_what_is_looked_at():
    found = slides.parts("Intro **bold**\n\n- a\n  - b\n1. c\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n"
                         "```chart\nA | 1\n```\n\n![diagram](/chat/files/7)\n\n![elsewhere](https://example.com/x.png)")
    kinds = [p[0] for p in found]
    assert kinds == ["text", "table", "chart", "picture"]
    paragraphs = found[0][1]
    assert [(p["level"], p["marker"]) for p in paragraphs] == [(0, None), (0, "•"), (1, "•"), (0, "1.")]
    assert any(r["bold"] for r in paragraphs[0]["runs"])
    assert found[3][1:] == (7, "diagram")            # only the person's own pictures, never an address


def _template(potx=False):
    """A template as a person would attach it: a deck with a slide of its own, which is dropped."""
    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[1]).shapes.title.text = "Old slide"
    out = io.BytesIO()
    deck.save(out)
    if not potx:
        return out.getvalue()
    source, copied = zipfile.ZipFile(io.BytesIO(out.getvalue())), io.BytesIO()
    with zipfile.ZipFile(copied, "w") as target:
        for item in source.infolist():
            part = source.read(item.filename)
            if item.filename == "[Content_Types].xml":
                part = part.replace(b"presentationml.presentation.main+xml", b"presentationml.template.main+xml")
            target.writestr(item, part)
    return copied.getvalue()


def _opened(data):
    return pptx.Presentation(io.BytesIO(data))


def test_make_builds_every_slide_with_a_real_chart_and_notes():
    title, data = slides.make(DECK)
    deck = _opened(data)
    assert title == "Quarterly review" and len(deck.slides) == 7
    assert any(shape.has_chart for shape in deck.slides[3].shapes)
    assert any(getattr(shape, "has_table", False) and shape.has_table for shape in deck.slides[2].shapes)
    assert deck.slides[1].notes_slide.notes_text_frame.text.startswith("Open with the good news")


@pytest.mark.parametrize("potx", [False, True])
def test_make_on_a_template_uses_its_layouts_and_drops_its_slides(potx):
    _, data = slides.make(DECK, template=_template(potx))
    deck = _opened(data)
    assert len(deck.slides) == 7
    words = " ".join(s.text_frame.text for slide in deck.slides for s in slide.shapes if s.has_text_frame)
    assert "Old slide" not in words and "Quarterly review" in words and "Revenue up" in words
    # Nothing left reading "Click to add text"
    assert all(ph.text_frame.text.strip() for slide in deck.slides for ph in slide.placeholders if ph.has_text_frame)


def test_a_file_that_is_not_powerpoint_is_refused_plainly():
    with pytest.raises(slides.SlidesError, match="could not be opened"):
        slides.make(DECK, template=b"not a deck")


def test_written_makes_a_deck_and_knows_its_kind():
    name, data, mime = written.make(DECK, "pptx")
    assert name == "Quarterly review.pptx" and mime == written.FORMATS["pptx"][1] and data[:2] == b"PK"
    assert written.format_of(mime) == "pptx" and written.format_of("application/pdf") == "pdf"
    assert written.format_of("text/markdown; charset=utf-8") == "markdown"
    assert "## " in written.writing_prompt("pptx") and "PowerPoint" in written.reminder("pptx")


def test_a_deck_is_read_back_as_a_document_and_an_empty_template_is_accepted():
    _, data = slides.make(DECK)
    assert documents.kind_of("deck.pptx") == "pptx" and documents.kind_of("x.potx") == "pptx"
    text = documents.extract("deck.pptx", data)
    assert "--- slide 2 ---" in text and "Revenue up" in text and "Notes: Open with the good news" in text
    empty = io.BytesIO()
    pptx.Presentation().save(empty)
    assert "template" in documents.extract("blank.pptx", empty.getvalue())
