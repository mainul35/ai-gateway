"""Slide decks the playground writes: Markdown in, a PowerPoint file (.pptx) out.

The same Markdown makes every deck, whoever wrote it: a model asked for a presentation (told to
write slides by WRITING_PROMPT), an answer saved with "Save as PowerPoint", or a document edited in
the side panel. So any Markdown makes a sensible deck:

- the first "# " heading is the title slide, and a line or two after it its subtitle;
- every other "# " or "## " heading starts a slide with that title; a line of --- starts one too;
- "### " and smaller are headings inside a slide;
- lists, paragraphs, tables, code, pictures of the person's own and ```chart blocks go on the slide
  they are under - a chart as a real PowerPoint chart, which can be edited there;
- a line "Notes: ..." (and the lines under it) is the slide's speaker notes;
- a slide with more than fits is carried on to the next, under the same title.

Drawn with python-pptx. Nothing is fetched: a picture is in the deck only when it is one of the
person's own uploads, handed in by the caller.
"""
import base64
import io
import logging
import math
import re

log = logging.getLogger("tools.slides")

MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MOST_SLIDES = 80

# How slides are written, for the playground (WRITING_PROMPT) and a presentation task alike
SLIDE_RULES = """- Begin with one "# " title naming the presentation, and one short line under it as its subtitle.
- Start every slide with a "## " heading: its title, a few words.
- On each slide 3 to 6 short bullet points, a few words to one line each - a slide is read at a \
glance, not a page of text. Use a table where things are compared, and a chart where there are \
numbers, as a block like this:
```chart
type: column
title: What the numbers are
x: 2023, 2024, 2025
---
First series  | 120, 140, 160
Second series | 90, 100, 130
```
  (type is one of bar, column, line, area, pie, donut; for a pie, one "Label | value" line each.)
- After a slide's points you may add a line starting "Notes:" with what the speaker says.
- If the user gave the information to present - notes, facts, figures, text to turn into slides - \
build the slides from that: keep their facts, names and numbers as given, do not add facts of your \
own, and arrange and shorten it into slides. Otherwise write it from the request and the \
conversation.
- About 8 to 12 slides unless the user says how many, ending with a summary or next steps.
- Write only the slides. No preface such as "here is your presentation", no offer at the end, no \
questions back.
- Write in the language the user writes in."""

WRITING_PROMPT = """The user has asked for a presentation, and this system makes the PowerPoint \
file for them: whatever you write in this reply is turned into slides and handed to them. You do not \
make the file and need no tool, library or code for it. Never say you cannot create files, and never \
explain how to make a presentation.

Write the slides now, in Markdown:
""" + SLIDE_RULES

REMINDER = """

(Write the slides themselves now, in Markdown: a "# " title, then a "## " heading for each slide. \
This system turns your reply into the PowerPoint file for me, so do not say you cannot make files \
and do not give code or steps for making one.)"""


class SlidesError(Exception):
    """The deck could not be made; the message says why, for a person."""


def is_available():
    try:
        import pptx  # noqa: F401
        return True
    except Exception:
        return False


# --- the Markdown, as slides -------------------------------------------------------------------

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,2})\s+(.*?)\s*#*\s*$")
BREAK = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
NOTES = re.compile(r"^\s*(?:>\s*)?(?:\*\*|__)?(?:speaker\s+)?notes?(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.*)$", re.I)


def _plain(text):
    return re.sub(r"[*_`]", "", text or "").strip()


def _new(title, kind="content"):
    return {"title": title, "kind": kind, "lines": [], "notes": []}


def split(text):
    """The slides in `text`: [{"title", "kind" (title | content), "body" (Markdown), "notes"}],
    a slide with too much on it carried on to the next."""
    from app.tools.written import document_part, title_of
    lines = document_part(text).splitlines()
    slides, current, fence, in_notes = [], None, None, False
    for line in lines:
        opening = FENCE.match(line)
        if fence is None and opening:
            fence = opening.group(1)[0] * 3
        elif fence is not None and line.strip().startswith(fence):
            fence = None
            if current is not None:
                current["lines"].append(line)
            continue
        if fence is None and not opening:
            heading = HEADING.match(line)
            if heading:
                in_notes = False
                kind = "title" if not slides and current is None and len(heading.group(1)) == 1 else "content"
                current = _new(_plain(heading.group(2)), kind)
                slides.append(current)
                continue
            if BREAK.match(line):
                in_notes = False
                if current is not None and "".join(current["lines"]).strip():
                    current = _new(current["title"] if current["kind"] == "content" else "")
                    slides.append(current)
                continue
            notes = NOTES.match(line)
            if notes and current is not None:
                in_notes = True
                if notes.group(1).strip():
                    current["notes"].append(notes.group(1).strip())
                continue
            if in_notes and line.strip():
                current["notes"].append(line.strip().lstrip(">").strip())
                continue
            in_notes = False
        if current is None:
            current = _new("", "content")
            slides.append(current)
        current["lines"].append(line)
    if not slides:
        raise SlidesError("There is nothing to put on slides.")
    if slides[0]["kind"] != "title":
        slides.insert(0, _new(title_of(text), "title"))
    deck = []
    for slide in slides:
        body = "\n".join(slide["lines"]).strip()
        notes = "\n".join(slide["notes"]).strip()
        if slide["kind"] == "title":
            subtitle, rest = _subtitle(body)
            deck.append({"title": slide["title"], "kind": "title", "body": subtitle, "notes": notes})
            if rest:
                deck.extend(_fitted(slide["title"], rest, ""))
            continue
        deck.extend(_fitted(slide["title"], body, notes))
    return deck[:MOST_SLIDES]


def _subtitle(body):
    """A title slide's subtitle - a short paragraph under the title - and what is left for a slide of
    its own."""
    if not body:
        return "", ""
    first, _, rest = body.partition("\n\n")
    if len(first) <= 220 and not re.match(r"\s*([-*+]\s|\d+[.)]\s|\||```|~~~|!\[)", first):
        return _plain(first.replace("\n", " ")), rest.strip()
    return "", body


# How much a slide holds, in lines of body text at a readable size
ROOM = 11


def _blocks(body):
    """The body's top-level blocks as (Markdown, weight in lines); a long list as its items."""
    parser = _parser()
    tokens = parser.parse(body)
    lines = body.splitlines()
    blocks = []
    for i, token in enumerate(tokens):
        if token.level != 0 or not token.map or token.nesting == -1:
            continue
        start, end = token.map
        if token.type in ("bullet_list_open", "ordered_list_open"):
            closing = next(j for j in range(i + 1, len(tokens))
                           if tokens[j].type == token.type.replace("open", "close") and tokens[j].level == 0)
            items = [t for t in tokens[i + 1:closing] if t.type == "list_item_open" and t.level == 1]
            numbered = token.type == "ordered_list_open"
            first = int(token.attrGet("start") or 1) if numbered else 1
            for n, item in enumerate(items):
                text = "\n".join(lines[item.map[0]:item.map[1]]).rstrip()
                if numbered:      # each piece of a split list keeps counting where it was
                    text = re.sub(r"^(\s*)\d+([.)])", lambda m: f"{m.group(1)}{first + n}{m.group(2)}", text, 1)
                blocks.append((text, _weight(text), "list"))
            continue
        text = "\n".join(lines[start:end]).rstrip()
        kind = "chart" if re.match(r"\s*(```|~~~)\s*chart", text) else token.type
        blocks.append((text, _weight(text, kind), kind))
    return blocks


def _weight(text, kind="text"):
    if kind == "chart" or re.match(r"\s*!\[", text):
        return 7
    if kind in ("fence", "code_block"):
        return 1 + 0.8 * len(text.splitlines())
    if kind == "table_open":
        return 1.2 * len([line for line in text.splitlines() if line.strip()])
    return sum(max(1, math.ceil(len(line.strip()) / 75)) for line in text.splitlines() if line.strip())


def _fitted(title, body, notes):
    """One slide, or as many as its body needs, each holding what fits."""
    if not body:
        return [{"title": title, "kind": "content", "body": "", "notes": notes}]
    blocks = _blocks(body)
    total = sum(weight for _, weight, _ in blocks)
    # Shared out evenly: a list of 14 is two slides of 7, not one of 11 and one of 3
    share = total / math.ceil(total / ROOM) if total > ROOM else ROOM
    pages, page, used, kind_before = [], [], 0, None
    for text, weight, kind in blocks:
        if page and (used + weight > ROOM or used + weight / 2 > share + 0.01):
            pages.append(page)
            page, used = [], 0
        # Items of one list stay one list; anything else stands apart from what came before
        joiner = "\n" if kind == "list" and kind_before == "list" else "\n\n"
        page.append((joiner if page else "") + text)
        used += weight
        kind_before = kind
    if page:
        pages.append(page)
    return [{"title": title, "kind": "content", "body": "".join(p).strip(), "notes": notes if n == 0 else ""}
            for n, p in enumerate(pages)]


_md = None


def _parser():
    global _md
    if _md is None:
        from markdown_it import MarkdownIt
        _md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    return _md


# --- what goes on a slide ----------------------------------------------------------------------

def _runs(inline, bold=False, italic=False):
    """[{"text", "bold", "italic", "code", "strike", "link"}] of an inline token."""
    out, b, it, strike, link = [], bold, italic, False, None
    for child in inline.children or []:
        kind = child.type
        if kind in ("text", "html_inline"):
            out.append({"text": child.content, "bold": b, "italic": it, "code": False, "strike": strike, "link": link})
        elif kind == "code_inline":
            out.append({"text": child.content, "bold": b, "italic": it, "code": True, "strike": strike, "link": link})
        elif kind == "strong_open":
            b = True
        elif kind == "strong_close":
            b = bold
        elif kind == "em_open":
            it = True
        elif kind == "em_close":
            it = italic
        elif kind == "s_open":
            strike = True
        elif kind == "s_close":
            strike = False
        elif kind == "link_open":
            href = child.attrGet("href") or ""
            link = href if re.match(r"^(https?:|mailto:)", href, re.I) else None
        elif kind == "link_close":
            link = None
        elif kind == "softbreak":
            out.append({"text": " ", "bold": b, "italic": it, "code": False, "strike": strike, "link": link})
        elif kind == "hardbreak":
            out.append({"text": "\n", "bold": b, "italic": it, "code": False, "strike": strike, "link": link})
    return out


def parts(body):
    """A slide's body as what is drawn: ("text", [paragraphs]), ("table", rows), ("code", text),
    ("chart", source), ("picture", file id, alt). Paragraphs are {"runs", "level", "marker"}."""
    tokens = _parser().parse(body or "")
    out, text, lists, quote = [], [], [], 0
    item_open = False

    def flush():
        if text:
            out.append(("text", text[:]))
            text.clear()

    i = 0
    while i < len(tokens):
        token = tokens[i]
        kind = token.type
        if kind in ("bullet_list_open", "ordered_list_open"):
            lists.append({"numbered": kind == "ordered_list_open", "n": int(token.attrGet("start") or 1)})
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            item_open = True
        elif kind == "blockquote_open":
            quote += 1
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "heading_open":
            text.append({"runs": _runs(tokens[i + 1], bold=True), "level": 0, "marker": None, "heading": True})
            i += 3
            continue
        elif kind == "inline":
            pictures = [c for c in token.children or [] if c.type == "image"]
            for picture in pictures:
                found = re.match(r"^/chat/files/(\d+)$", picture.attrGet("src") or "")
                if found:
                    flush()
                    out.append(("picture", int(found.group(1)), picture.content or ""))
            runs = _runs(token, italic=quote > 0)
            if "".join(r["text"] for r in runs).strip():
                marker = None
                if lists and item_open:
                    top = lists[-1]
                    marker = f"{top['n']}." if top["numbered"] else "•"
                    top["n"] += 1
                text.append({"runs": runs, "level": max(0, len(lists) - 1) + (1 if lists and not marker else 0),
                             "marker": marker})
            item_open = False
        elif kind in ("fence", "code_block"):
            flush()
            if kind == "fence" and (token.info or "").strip().lower().startswith("chart"):
                out.append(("chart", token.content))
            elif token.content.strip():
                out.append(("code", token.content.rstrip("\n")))
        elif kind == "table_open":
            flush()
            rows, row = [], None
            while tokens[i].type != "table_close":
                t = tokens[i]
                if t.type == "tr_open":
                    row = []
                elif t.type == "tr_close":
                    rows.append(row)
                elif t.type == "inline" and row is not None:
                    row.append(_runs(t))
                i += 1
            if rows:
                out.append(("table", rows))
        i += 1
    flush()
    return out


def chart_of(source):
    """A ```chart block (mdchart's form) as {"type", "title", "categories", "series": [(name,
    values)], "unit", "delta"}; None when it has no numbers."""
    settings, rows, in_data = {}, [], False
    for line in (source or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not in_data and re.match(r"^-{3,}$", line):
            in_data = True
            continue
        if not in_data and "|" not in line and ":" in line[1:]:
            key, _, value = line.partition(":")
            settings[key.strip().lower()] = value.strip()
            continue
        in_data = True
        rows.append(line)

    def number(text):
        try:
            return float(text.replace(",", "").rstrip("%").strip())
        except ValueError:
            return None

    named = []
    for row in rows:
        name, bar, rest = row.partition("|")
        if not bar:
            name, rest = "", row
        values = [number(v) for v in rest.split(",") if v.strip()]       # as mdchart reads them
        if any(v is not None for v in values):
            named.append((name.strip(), values))
    if not named:
        return None
    kind = (settings.get("type") or "bar").lower()
    xs = [x.strip() for x in (settings.get("x") or "").split(",") if x.strip()]
    if any(len(values) > 1 for _, values in named):
        width = max(len(values) for _, values in named)
        categories = (xs + [str(n + 1) for n in range(len(xs), width)])[:width]
        series = [(name or f"Series {n + 1}", (values + [None] * width)[:width]) for n, (name, values) in enumerate(named)]
    else:
        categories = [name or str(n + 1) for n, (name, _) in enumerate(named)]
        series = [(settings.get("series") or settings.get("title") or "Value", [values[0] for _, values in named])]
    if kind in ("pie", "donut"):
        series = series[:1]
    return {"type": kind, "title": settings.get("title") or "", "categories": categories, "series": series,
            "unit": settings.get("unit") or "", "delta": settings.get("delta") or ""}


# --- drawing -----------------------------------------------------------------------------------

WIDE, HIGH = 13.333, 7.5            # inches, 16:9
LEFT, RIGHT = 0.65, 0.65
INK, SOFT, ACCENT, DEEP, PALE = "1F2430", "6B7280", "4F46E5", "312E81", "EEF0FA"
SERIES = ["2A78D6", "E3863B", "2E9E6A", "C2457E", "7A5BD0", "B8932A", "3AA6B9", "8A6E5A"]


def _palette():
    try:
        from app.tools.written import _chart_colours
        colours = _chart_colours()
        found = [colours.get(f"--mdc-series-{n}", "").lstrip("#") for n in range(1, 9)]
        if all(re.match(r"^[0-9A-Fa-f]{6}$", c) for c in found):
            return found
    except Exception:
        pass
    return SERIES


def _rgb(hex_colour):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hex_colour)


def _fill(shape, colour):
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(colour)
    shape.line.fill.background()


def _put_runs(paragraph, runs, size, colour=INK, bold=False):
    """The runs into the paragraph; `colour` None leaves the colour to the template."""
    from pptx.util import Pt
    for r in runs:
        pieces = r["text"].split("\n")
        for n, piece in enumerate(pieces):
            if n:
                paragraph.add_line_break()
            if not piece:
                continue
            run = paragraph.add_run()
            run.text = piece
            font = run.font
            font.size = Pt(size)
            font.bold = bool(r["bold"] or bold)
            font.italic = bool(r["italic"])
            if colour is not None:
                font.color.rgb = _rgb(ACCENT if r["link"] else colour)
            if r["code"]:
                font.name = "Consolas"
            if r["link"]:
                run.hyperlink.address = r["link"]
            if r["strike"]:
                run._r.get_or_add_rPr().set("strike", "sngStrike")


def _text_size(paragraphs, width, height, largest=24, smallest=12):
    """The largest size the paragraphs fit in a box of width x height inches."""
    for size in range(largest, smallest - 1, -2):
        per_line = max(10, width * 72 / (size * 0.52))
        lines = 0
        for p in paragraphs:
            words = "".join(r["text"] for r in p["runs"])
            room = per_line - 3 * p["level"] - (2 if p["marker"] else 0)
            lines += sum(max(1, math.ceil(len(piece) / max(room, 8))) for piece in words.split("\n"))
        needed = (lines * size * 1.22 + len(paragraphs) * size * 0.45) / 72
        if needed <= height:
            return size
    return smallest


def _draw_text(slide, paragraphs, x, y, w, h, placeholder=None):
    """The paragraphs in a box of their own, or - from a template - in the slide's body placeholder,
    where the template's bullets, font and colours are kept and only the size is chosen."""
    from pptx.util import Inches, Pt
    from pptx.enum.text import MSO_ANCHOR
    if placeholder is not None:
        _themed_text(placeholder, paragraphs, w, h)
        return
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = MSO_ANCHOR.TOP
    frame.margin_left = frame.margin_right = Inches(0.05)
    size = _text_size(paragraphs, w - 0.1, h - 0.1)
    for n, p in enumerate(paragraphs):
        paragraph = frame.paragraphs[0] if n == 0 else frame.add_paragraph()
        paragraph.space_after = Pt(size * 0.45)
        indent = 0.38 * p["level"]
        smaller = size - 2 * min(p["level"], 2)
        if p["marker"]:
            hang = 0.3 if p["marker"] == "•" else 0.42
            _hanging(paragraph, indent + hang, hang)
            mark = paragraph.add_run()
            mark.text = p["marker"] + "\t"
            mark.font.size = Pt(smaller)
            mark.font.bold = True
            mark.font.color.rgb = _rgb(ACCENT)
        elif indent:
            _hanging(paragraph, indent, 0)
        _put_runs(paragraph, p["runs"], smaller + (2 if p.get("heading") else 0),
                  DEEP if p.get("heading") else INK)


def _hanging(paragraph, left, hang):
    """A paragraph indented `left` inches, its first line `hang` inches less: a bullet hangs."""
    from pptx.util import Inches
    props = paragraph._p.get_or_add_pPr()
    props.set("marL", str(int(Inches(left))))
    props.set("indent", str(-int(Inches(hang))))


def _themed_text(placeholder, paragraphs, w, h):
    from lxml import etree
    from pptx.oxml.ns import qn
    frame = placeholder.text_frame
    frame.clear()
    frame.word_wrap = True
    size = _text_size(paragraphs, w - 0.3, h - 0.2, largest=26)
    for n, p in enumerate(paragraphs):
        paragraph = frame.paragraphs[0] if n == 0 else frame.add_paragraph()
        paragraph.level = min(p["level"], 4)
        props = paragraph._p.get_or_add_pPr()
        if p["marker"] and p["marker"] != "•":
            # Numbered as PowerPoint numbers, in the template's font and colour
            etree.SubElement(props, qn("a:buAutoNum"), type="arabicPeriod", startAt=p["marker"].rstrip("."))
        elif not p["marker"]:
            props.set("indent", "0")
            if not p["level"]:
                props.set("marL", "0")
            etree.SubElement(props, qn("a:buNone"))
        _put_runs(paragraph, p["runs"], size - 2 * min(p["level"], 2) + (2 if p.get("heading") else 0),
                  None, bold=bool(p.get("heading")))


def _draw_table(slide, rows, x, y, w, h, themed=False):
    from pptx.util import Inches, Pt
    columns = max(len(r) for r in rows)
    rows = [r + [[]] * (columns - len(r)) for r in rows]
    size = 16 if len(rows) <= 5 else 14 if len(rows) <= 8 else 12 if len(rows) <= 12 else 10
    row_height = min(h / len(rows), size * 2.4 / 72)
    shape = slide.shapes.add_table(len(rows), columns, Inches(x), Inches(y), Inches(w), Inches(row_height * len(rows)))
    table = shape.table
    for r, row in enumerate(rows):
        table.rows[r].height = Inches(row_height)
        for c, runs in enumerate(row):
            cell = table.cell(r, c)
            cell.margin_left = cell.margin_right = Inches(0.08)
            cell.margin_top = cell.margin_bottom = Inches(0.03)
            frame = cell.text_frame
            frame.word_wrap = True
            frame.paragraphs[0].text = ""
            if themed:            # the template's own table style colours it
                _put_runs(frame.paragraphs[0], runs, size, None, bold=r == 0)
                continue
            _put_runs(frame.paragraphs[0], runs, size, "FFFFFF" if r == 0 else INK, bold=r == 0)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(ACCENT if r == 0 else (PALE if r % 2 == 0 else "FFFFFF"))


def _draw_code(slide, code, x, y, w, h):
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    lines = code.splitlines() or [""]
    widest = max(len(line) for line in lines)
    size = max(8, min(16, int(h * 72 / (len(lines) * 1.3 + 1)), int(w * 72 / (max(widest, 20) * 0.62))))
    back = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w),
                                  Inches(min(h, (len(lines) * size * 1.3 + size * 1.6) / 72)))
    _fill(back, "F6F7FB")
    back.adjustments[0] = 0.04
    frame = back.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = Inches(0.18)
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    frame.vertical_anchor = MSO_ANCHOR.TOP
    for n, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if n == 0 else frame.add_paragraph()
        paragraph.alignment = PP_ALIGN.LEFT
        run = paragraph.add_run()
        run.text = line
        run.font.name = "Consolas"
        run.font.size = Pt(size)
        run.font.color.rgb = _rgb(INK)


def _draw_chart(slide, source, x, y, w, h, themed=False):
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION, XL_LABEL_POSITION
    from pptx.util import Inches, Pt
    chart = chart_of(source)
    if chart is None:
        _draw_code(slide, source.rstrip("\n"), x, y, w, h)
        return
    if chart["type"] == "stat":
        _draw_stat(slide, chart, x, y, w, h)
        return
    kinds = {"bar": XL_CHART_TYPE.BAR_CLUSTERED, "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
             "line": XL_CHART_TYPE.LINE_MARKERS, "area": XL_CHART_TYPE.AREA, "pie": XL_CHART_TYPE.PIE,
             "donut": XL_CHART_TYPE.DOUGHNUT}
    kind = kinds.get(chart["type"], XL_CHART_TYPE.COLUMN_CLUSTERED)
    round_one = kind in (XL_CHART_TYPE.PIE, XL_CHART_TYPE.DOUGHNUT)
    data = CategoryChartData()
    data.categories = chart["categories"]
    for name, values in chart["series"]:
        data.add_series(name, values)
    frame = slide.shapes.add_chart(kind, Inches(x), Inches(y), Inches(w), Inches(h), data)
    graph = frame.chart
    graph.font.size = Pt(12)
    if not themed:
        graph.font.color.rgb = _rgb(INK)
    graph.has_title = bool(chart["title"])
    if chart["title"]:
        graph.chart_title.text_frame.text = chart["title"]
        graph.chart_title.text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        graph.chart_title.text_frame.paragraphs[0].runs[0].font.bold = True
    # From a template, the series take its theme's accent colours
    palette = None if themed else _palette()
    plot = graph.plots[0]
    if round_one:
        if themed:
            plot.vary_by_categories = True
        for n, point in enumerate(plot.series[0].points if palette else []):
            point.format.fill.solid()
            point.format.fill.fore_color.rgb = _rgb(palette[n % len(palette)])
        plot.has_data_labels = True
        labels = plot.data_labels
        labels.show_percentage, labels.show_value, labels.show_category_name = True, False, False
        labels.number_format, labels.number_format_is_linked = "0%", False
        labels.font.size = Pt(12)
        graph.has_legend = True
        graph.legend.position = XL_LEGEND_POSITION.RIGHT
        graph.legend.include_in_layout = False
        return
    for n, series in enumerate(plot.series if palette else []):
        colour = _rgb(palette[n % len(palette)])
        if kind == XL_CHART_TYPE.LINE_MARKERS:
            series.format.line.color.rgb = colour
            series.format.line.width = Pt(2.25)
            series.smooth = False
            series.marker.format.fill.solid()
            series.marker.format.fill.fore_color.rgb = colour
        else:
            series.format.fill.solid()
            series.format.fill.fore_color.rgb = colour
    graph.has_legend = len(chart["series"]) > 1
    if graph.has_legend:
        graph.legend.position = XL_LEGEND_POSITION.BOTTOM
        graph.legend.include_in_layout = False
    if len(chart["series"]) == 1 and len(chart["categories"]) <= 12 and kind != XL_CHART_TYPE.AREA:
        plot.has_data_labels = True
        plot.data_labels.font.size = Pt(11)
        plot.data_labels.number_format_is_linked = True
        if kind in (XL_CHART_TYPE.BAR_CLUSTERED, XL_CHART_TYPE.COLUMN_CLUSTERED):
            plot.data_labels.position = XL_LABEL_POSITION.OUTSIDE_END
    if themed:
        return
    try:
        graph.value_axis.major_gridlines.format.line.color.rgb = _rgb("E5E7EB")
        graph.value_axis.format.line.fill.background()
        graph.value_axis.tick_labels.font.size = Pt(11)
        graph.category_axis.tick_labels.font.size = Pt(11)
    except Exception:
        pass


def _draw_stat(slide, chart, x, y, w, h):
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    value = next((v for v in chart["series"][0][1] if v is not None), 0)
    shown = f"{value:,.0f}" if float(value).is_integer() else f"{value:,}"
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    lines = [(chart["title"], 18, SOFT, False), (f"{shown} {chart['unit']}".strip(), 60, DEEP, True),
             (chart["delta"], 18, ACCENT, False)]
    first = True
    for text, size, colour, bold in lines:
        if not text:
            continue
        paragraph = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        paragraph.alignment = PP_ALIGN.CENTER
        run = paragraph.add_run()
        run.text = text
        run.font.size, run.font.bold = Pt(size), bold
        run.font.color.rgb = _rgb(colour)


def _picture_bytes(found):
    """Bytes PowerPoint can show (PNG or JPEG) and (width, height), from bytes or a data: URL."""
    if isinstance(found, str):
        found = base64.b64decode(found.split(",", 1)[1]) if found.startswith("data:") else None
    if not found:
        return None
    from PIL import Image
    try:
        picture = Image.open(io.BytesIO(found))
        size = picture.size
        if picture.format in ("PNG", "JPEG"):
            return found, size
        out = io.BytesIO()
        picture.convert("RGBA" if picture.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
        return out.getvalue(), size
    except Exception as e:
        log.warning("A picture could not be put on a slide: %s", e)
        return None


def _draw_picture(slide, found, x, y, w, h):
    from pptx.util import Inches
    picture = _picture_bytes(found)
    if picture is None:
        return
    data, (pw, ph) = picture
    scale = min(w / pw, h / ph)
    width, height = pw * scale, ph * scale
    slide.shapes.add_picture(io.BytesIO(data), Inches(x + (w - width) / 2), Inches(y + (h - height) / 2),
                             Inches(width), Inches(height))


def _background(slide, colour):
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(colour)


def _words(slide, text, x, y, w, h, size, colour, bold=False, anchor=None):
    from pptx.util import Inches, Pt
    from pptx.enum.text import MSO_ANCHOR
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor or MSO_ANCHOR.TOP
    run = frame.paragraphs[0].add_run()
    run.text = text
    run.font.size, run.font.bold = Pt(size), bold
    run.font.color.rgb = _rgb(colour)
    return box


def _title_slide(deck, slide_info):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    _background(slide, DEEP)
    title = slide_info["title"] or "Presentation"
    size = 44 if len(title) <= 40 else 36 if len(title) <= 70 else 30
    _words(slide, title, LEFT + 0.2, 1.6, WIDE - 2 * LEFT - 0.4, 2.6, size, "FFFFFF", bold=True, anchor=MSO_ANCHOR.BOTTOM)
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LEFT + 0.25), Inches(4.35), Inches(1.6), Inches(0.08))
    _fill(bar, "A5B4FC")
    if slide_info["body"]:
        _words(slide, slide_info["body"], LEFT + 0.2, 4.6, WIDE - 2 * LEFT - 0.4, 1.6, 22, "C7D2FE")
    return slide


def _section_slide(deck, title):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    _background(slide, PALE)
    _words(slide, title, LEFT + 0.2, 2.2, WIDE - 2 * LEFT - 0.4, 2.0, 40, DEEP, bold=True, anchor=MSO_ANCHOR.BOTTOM)
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LEFT + 0.25), Inches(4.4), Inches(1.4), Inches(0.08))
    _fill(bar, ACCENT)
    return slide


def _content_slide(deck, slide_info, images, footer, number):
    from pptx.util import Inches
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    _background(slide, "FFFFFF")
    title = slide_info["title"]
    if title:
        size = 32 if len(title) <= 45 else 26 if len(title) <= 80 else 22
        _words(slide, title, LEFT, 0.35, WIDE - LEFT - RIGHT, 0.95, size, INK, bold=True, anchor=MSO_ANCHOR.BOTTOM)
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LEFT + 0.05), Inches(1.36), Inches(1.1), Inches(0.06))
        _fill(bar, ACCENT)
    top = 1.62 if title else 0.6
    bottom = 6.85
    found = parts(slide_info["body"])
    texts = [p for p in found if p[0] == "text"]
    visuals = [p for p in found if p[0] != "text"]
    width = WIDE - LEFT - RIGHT
    if texts and visuals:
        # Words on the left, what is to be looked at on the right
        words_w = width * (0.42 if any(v[0] in ("chart", "picture") for v in visuals) else 0.38)
        paragraphs = [p for _, ps in texts for p in ps]
        _draw_text(slide, paragraphs, LEFT, top, words_w, bottom - top)
        _draw_visuals(slide, visuals, images, LEFT + words_w + 0.3, top, width - words_w - 0.3, bottom - top)
    elif visuals:
        _draw_visuals(slide, visuals, images, LEFT, top, width, bottom - top)
    elif texts:
        _draw_text(slide, [p for _, ps in texts for p in ps], LEFT, top, width, bottom - top)
    # The deck's name and the slide's number, small along the foot
    _words(slide, footer, LEFT, 7.0, width - 1.2, 0.35, 10, SOFT)
    number_box = _words(slide, str(number), WIDE - RIGHT - 1.0, 7.0, 1.0, 0.35, 10, SOFT)
    number_box.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT
    return slide


def _draw_visuals(slide, visuals, images, x, y, w, h, themed=False):
    gap = 0.25
    each = (h - gap * (len(visuals) - 1)) / len(visuals)
    for n, visual in enumerate(visuals):
        top = y + n * (each + gap)
        if visual[0] == "table":
            _draw_table(slide, visual[1], x, top, w, each, themed)
        elif visual[0] == "code":
            _draw_code(slide, visual[1], x, top, w, each)
        elif visual[0] == "chart":
            _draw_chart(slide, visual[1], x, top, w, each, themed)
        elif visual[0] == "picture" and visual[1] in (images or {}):
            _draw_picture(slide, images[visual[1]], x, top, w, each)


# --- a template --------------------------------------------------------------------------------
# A deck the person attached (.pptx or .potx): its slides are dropped and its layouts used - the
# title slide's, a title-and-content one, a section header - so the new deck has its background,
# fonts, colours and logo. Text goes into the layouts' placeholders; a table, chart or picture goes
# where the body placeholder was.

def opened(data, keep_slides=False):
    """A python-pptx Presentation of a .pptx or .potx, without its slides unless `keep_slides`."""
    import zipfile
    from pptx import Presentation
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        types = archive.read("[Content_Types].xml")
        if b"presentationml.template.main+xml" in types:
            # A .potx is a .pptx whose main part says "template"; python-pptx opens only the other
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as copy:
                for item in archive.infolist():
                    part = archive.read(item.filename)
                    if item.filename == "[Content_Types].xml":
                        part = part.replace(b"presentationml.template.main+xml",
                                            b"presentationml.presentation.main+xml")
                    copy.writestr(item, part)
            data = out.getvalue()
        deck = Presentation(io.BytesIO(data))
    except Exception as e:
        log.info("A PowerPoint file could not be opened: %s", e)
        raise SlidesError("That PowerPoint file could not be opened; save it again as .pptx and attach that.")
    if not keep_slides:
        listed = deck.slides._sldIdLst
        for entry in list(listed):
            deck.part.drop_rel(entry.rId)
            listed.remove(entry)
    return deck


def _types(layout):
    return {ph.placeholder_format.type for ph in layout.placeholders}


def _layouts(deck):
    """{"title", "content", "section"}: the template's layouts for each kind of slide."""
    from pptx.enum.shapes import PP_PLACEHOLDER as P
    layouts = list(deck.slide_layouts)
    if not layouts:
        raise SlidesError("That template has no slide layouts to build on.")
    body = {P.BODY, P.OBJECT}

    def named(word):
        return next((layout for layout in layouts if word in (layout.name or "").lower()), None)

    title = named("title slide") or next(
        (layout for layout in layouts if _types(layout) & {P.CENTER_TITLE, P.SUBTITLE}), layouts[0])
    content = (next((layout for layout in layouts if "content" in (layout.name or "").lower()
                     and P.TITLE in _types(layout) and _types(layout) & body), None)
               or next((layout for layout in layouts if P.TITLE in _types(layout) and _types(layout) & body
                        and P.SUBTITLE not in _types(layout) and layout is not title), None)
               or title)
    section = (named("section")
               or next((layout for layout in layouts if _types(layout) & {P.TITLE, P.CENTER_TITLE}
                        and not _types(layout) & body and layout is not title), None)
               or content)
    return {"title": title, "content": content, "section": section}


def _placeholder(slide, *kinds):
    return next((ph for ph in slide.placeholders if ph.placeholder_format.type in kinds), None)


def _drop(shape):
    if shape is not None:
        shape._element.getparent().remove(shape._element)


def _drop_empty(slide):
    """Placeholders left unused, which PowerPoint would show as "Click to add text"."""
    for ph in list(slide.placeholders):
        if ph.has_text_frame and not ph.text_frame.text.strip():
            _drop(ph)


def _themed_slide(deck, layouts, info, images):
    from pptx.enum.shapes import PP_PLACEHOLDER as P
    emu = 914400
    if info["kind"] == "title" or not info["body"]:
        slide = deck.slides.add_slide(layouts["title" if info["kind"] == "title" else "section"])
        title = _placeholder(slide, P.CENTER_TITLE, P.TITLE)
        if title is not None:
            title.text_frame.text = info["title"] or ("Presentation" if info["kind"] == "title" else "")
        under = _placeholder(slide, P.SUBTITLE, P.BODY)
        if under is not None and info["body"]:
            under.text_frame.text = info["body"]
        _drop_empty(slide)
        return slide
    slide = deck.slides.add_slide(layouts["content"])
    title = _placeholder(slide, P.TITLE, P.CENTER_TITLE)
    if title is not None:
        title.text_frame.text = info["title"]
    body = _placeholder(slide, P.BODY, P.OBJECT)
    if body is not None:
        x, y, w, h = (v / emu for v in (body.left, body.top, body.width, body.height))
    else:
        # A layout without a body: below its title, or the middle of the slide
        width, height = deck.slide_width / emu, deck.slide_height / emu
        y = (title.top + title.height) / emu + 0.2 if title is not None else height * 0.2
        x, w, h = width * 0.06, width * 0.88, height * 0.92 - y
    found = parts(info["body"])
    paragraphs = [p for part in found if part[0] == "text" for p in part[1]]
    visuals = [part for part in found if part[0] != "text"]
    if paragraphs and visuals:
        words_w = w * 0.42
        if body is not None:
            # All four, not only the width: a placeholder that inherits its place from the layout
            # is given one of its own, and the parts not set would be 0 - the top-left corner
            body.left, body.top, body.width, body.height = (int(v * emu) for v in (x, y, words_w, h))
        _draw_text(slide, paragraphs, x, y, words_w, h, placeholder=body)
        _draw_visuals(slide, visuals, images, x + words_w + 0.25, y, w - words_w - 0.25, h, themed=True)
    elif visuals:
        _drop(body)
        _draw_visuals(slide, visuals, images, x, y, w, h, themed=True)
    else:
        _draw_text(slide, paragraphs, x, y, w, h, placeholder=body)
    _drop_empty(slide)
    return slide


def make(text, images=None, template=None):
    """(title, .pptx bytes) for the deck in `text`. `images` {file id: bytes or data URL} are the
    person's own pictures it shows (/chat/files/<id>); `template` the bytes of a .pptx or .potx whose
    design it takes."""
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except Exception as e:
        log.warning("python-pptx is not usable: %s", e)
        raise SlidesError("PowerPoint files cannot be made on this server; ask for a PDF or Markdown instead.")
    deck_slides = split(text)
    footer = deck_slides[0]["title"][:90]
    if template:
        deck = opened(template)
        layouts = _layouts(deck)
        made = [_themed_slide(deck, layouts, info, images) for info in deck_slides]
    else:
        deck = Presentation()
        deck.slide_width, deck.slide_height = Inches(WIDE), Inches(HIGH)
        made = []
        for number, info in enumerate(deck_slides, start=1):
            if info["kind"] == "title":
                made.append(_title_slide(deck, info))
            elif not info["body"]:
                made.append(_section_slide(deck, info["title"]))
            else:
                made.append(_content_slide(deck, info, images, footer, number))
    for slide, info in zip(made, deck_slides):
        if info["notes"]:
            slide.notes_slide.notes_text_frame.text = info["notes"]
    deck.core_properties.title = footer
    out = io.BytesIO()
    try:
        deck.save(out)
    except Exception as e:
        log.warning("Could not save a deck: %s", e)
        raise SlidesError("The PowerPoint file could not be made; the slides are in the answer above.")
    return footer or "Presentation", out.getvalue()


def preview(text):
    """The slides as the side panel shows them: [{"title", "kind", "html", "notes"}]."""
    from app.tools import markdown
    return [{"title": s["title"], "kind": s["kind"], "notes": s["notes"],
             "html": (markdown.render(s["body"]) or "") if s["kind"] != "title" else "",
             "subtitle": s["body"] if s["kind"] == "title" else ""} for s in split(text)]
