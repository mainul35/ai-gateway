"""Documents the playground writes for you: the model writes Markdown, and this makes the file.

Asked for "a document", a model answers in chat with the same words it would have used anyway, and
there is nothing to keep. So the request says which file it wants - Markdown or PDF - the model is
told to write the document itself and nothing around it, and the answer is turned into that file:
Markdown as it was written, a PDF drawn from the same HTML the playground shows (WeasyPrint).

A PDF is laid out with the fonts this server has. Bangla needs a font that has it (Noto Sans
Bengali, from the fonts-noto-core package); without one its letters come out as empty boxes.
"""
import logging
import os
import re
import unicodedata

from app.tools import markdown

log = logging.getLogger("tools.written")

FORMATS = {"markdown": ("md", "text/markdown; charset=utf-8"), "pdf": ("pdf", "application/pdf")}

KIND_NAMES = {"markdown": "Markdown", "pdf": "PDF"}

# Told twice: as the instruction for the turn, and again right after the request. Asked to "generate
# a pdf" with this only among the system messages, a small model read the request literally, said it
# could not make files, and wrote a Python script for making one - which was then saved as the PDF.
WRITING_PROMPT = """The user has asked for a document, and this system makes the file for them: \
whatever you write in this reply is saved as a {kind} file and handed to them. You do not make the \
file and need no tool, library or code for it. Never say you cannot create files, and never explain \
how to make a {kind}.

Write the whole document now, in Markdown, from their request and everything in the conversation \
that it is about:
- Begin with one "# " title naming what the document is about.
- Use headings, short paragraphs, lists, tables and code blocks wherever they help a reader.
- Write only the document. No preface such as "here is your document", no offer at the end, no \
questions back: what you write is saved exactly as it is.
- Write in the language the user writes in."""

REMINDER = """

(Write the document itself now, in Markdown, starting with its "# " title. This system saves your \
reply as the {kind} file for me, so do not say you cannot make files and do not give code or steps \
for making one.)"""


def writing_prompt(fmt):
    return WRITING_PROMPT.format(kind=KIND_NAMES.get(fmt, "document"))


def reminder(fmt):
    return REMINDER.format(kind=KIND_NAMES.get(fmt, "document"))


# A reply that turns the request down rather than writing anything: saved as a file it would be a
# PDF of an apology, so no file is made from it
REFUSAL = re.compile(r"^\W*(?:\S+\s+){0,12}?(?:I\s+(?:cannot|can't|can\s*not|am\s+unable\s+to|am\s+not\s+able\s+to|"
                     r"don't\s+have\s+the\s+ability)|as\s+an\s+AI\b)", re.I)


def is_refusal(text):
    text = (text or "").strip()
    # A document begins with its title; one that is about not being able to do something is still one
    return not text.startswith("#") and bool(REFUSAL.match(text[:300]))


def document_part(text):
    """The document without a line or two of preface before its title, if the model wrote one."""
    lines = (text or "").strip().splitlines()
    for i, line in enumerate(lines[:6]):
        if re.match(r"#\s+\S", line.strip()):
            return "\n".join(lines[i:]).strip()
    return (text or "").strip()


# Only what a document needs, so a PDF reads like one rather than like a web page
PDF_STYLE = """
@page { size: A4; margin: 22mm 20mm 24mm;
        @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; color: #8a8a96; } }
html { font-family: "Noto Sans", "Noto Sans Bengali", "DejaVu Sans", sans-serif; font-size: 12pt;
       line-height: 1.65; color: #1f2430; }
body { hyphens: auto; }
h1 { font-size: 23pt; line-height: 1.25; margin: 0 0 14pt; padding-bottom: 8pt;
     border-bottom: 2pt solid #4f46e5; color: #111827; }
h2 { font-size: 16pt; line-height: 1.3; margin: 22pt 0 8pt; color: #312e81; }
h3 { font-size: 13.5pt; line-height: 1.35; margin: 16pt 0 6pt; color: #1f2937; }
h4 { font-size: 12pt; margin: 12pt 0 4pt; }
h1, h2, h3, h4 { break-after: avoid; }
p { margin: 0 0 9pt; }
ul, ol { margin: 0 0 10pt; padding-left: 16pt; }
li { margin: 0 0 4pt; }
p, li { orphans: 3; widows: 3; }
strong { color: #111827; }
a { color: #4338ca; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 10pt; background: #eef0f6;
       padding: 0.5pt 3pt; border-radius: 2pt; }
pre { background: #f6f7fb; border: 0.6pt solid #dcdfea; border-left: 3pt solid #818cf8;
      border-radius: 3pt; padding: 9pt 11pt; margin: 6pt 0 12pt; line-height: 1.45;
      white-space: pre-wrap; overflow-wrap: anywhere; }
pre code { background: none; padding: 0; font-size: 9.5pt; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0 14pt; font-size: 10.5pt; line-height: 1.45; }
th, td { border: 0.6pt solid #cfd3e0; padding: 5pt 7pt; text-align: left; vertical-align: top; }
th { background: #eef0fa; color: #1f2937; }
tr { break-inside: avoid; }
blockquote { margin: 10pt 0; padding: 4pt 12pt; border-left: 3pt solid #c7d2fe; color: #374151;
             background: #f8f9fe; }
img { max-width: 100%; }
hr { border: 0; border-top: 0.6pt solid #d1d5db; margin: 16pt 0; }
"""


class WrittenError(Exception):
    """The file could not be made; the message says why, for a person."""


def title_of(text):
    """The document's title: its first heading, or failing that its first line."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        heading = re.match(r"#{1,6}\s+(.*)", line)
        if heading:
            return re.sub(r"[*_`]", "", heading.group(1)).strip()[:120] or "Document"
    return re.sub(r"[*_`#]", "", lines[0]).strip()[:120] if lines else "Document"


def file_name(title, extension):
    """A file name from the title, keeping letters of any script and dropping what a disk refuses."""
    name = unicodedata.normalize("NFC", title)
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:80].strip(" .")
    return f"{name or 'document'}.{extension}"


def is_pdf_available():
    try:
        import weasyprint  # noqa: F401
        return True
    except Exception:
        return False


def _no_fetching(url, *args, **kwargs):
    """The PDF is drawn from the model's words alone: an image or a stylesheet it points at is
    never fetched, so a document cannot be used to make this server read an address on its network.
    For WeasyPrint before 66, which takes a function; _fetcher() gives the newer ones theirs."""
    if url.startswith("data:"):
        from weasyprint import default_url_fetcher
        return default_url_fetcher(url, *args, **kwargs)
    raise ValueError("documents fetch nothing")


def _fetcher():
    """What WeasyPrint may fetch with: data: URLs only - the pictures put in the page itself. Newer
    WeasyPrint (70 here) wants a URLFetcher, and a plain function failed every PDF that had one."""
    try:
        from weasyprint.urls import URLFetcher
    except ImportError:
        return _no_fetching
    return URLFetcher(allowed_protocols={"data"})


CHART_BLOCK = re.compile(r'<pre><code class="language-chart">.*?</code></pre>', re.S)
CHAT_IMAGE = re.compile(r'<img src="/chat/files/(\d+)"')
CHART_STYLE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "mdchart", "mdchart.css")


def _chart_style():
    """mdchart's stylesheet, without which a chart has no colours; its data table is left out of print."""
    try:
        with open(CHART_STYLE_FILE, encoding="utf-8") as f:
            style = f.read()
    except OSError:
        return ""
    colours = _chart_colours()
    # Written in, as in the charts: inside an SVG WeasyPrint does not resolve var()
    style = re.sub(r"var\((--mdc-[\w-]+)\)", lambda m: colours.get(m.group(1), m.group(0)), style)
    return style + "\n.mdc-table { display: none; }\n.mdc-figure { break-inside: avoid; }\n"


def _chart_colours():
    """mdchart's light colours, {--mdc-name: value}, from its stylesheet's first :root."""
    try:
        with open(CHART_STYLE_FILE, encoding="utf-8") as f:
            root = re.search(r":root\s*\{(.*?)\}", f.read(), re.S)
    except OSError:
        return {}
    found = dict(re.findall(r"(--mdc-[\w-]+)\s*:\s*([^;]+);", root.group(1) if root else ""))
    for _ in range(4):                                   # a colour named after another one
        found = {k: re.sub(r"var\((--mdc-[\w-]+)\)", lambda m: found.get(m.group(1), m.group(0)), v).strip()
                 for k, v in found.items()}
    return found


# What mdchart's stylesheet paints SVG parts with, as attributes: WeasyPrint does not apply a
# stylesheet inside an SVG, and the grid lines came out black. {name} is one of its colours.
SVG_PAINT = {
    "mdc-grid": {"stroke": "{grid}", "stroke-width": "1"},
    "mdc-tick": {"fill": "{ink-soft}", "font-size": "11"},
    "mdc-cat": {"fill": "{ink-soft}", "font-size": "11"},
    "mdc-value": {"fill": "{ink}", "font-size": "11"},
    "mdc-line": {"fill": "none", "stroke-width": "2"},
    "mdc-area": {"opacity": "0.1"},
    "mdc-dot": {"stroke": "{plate}", "stroke-width": "2"},
    "mdc-slice": {"stroke": "none"},
    "mdc-hero": {"fill": "{ink}", "font-size": "20", "font-weight": "600"},
}
SVG_PART = re.compile(r'<([a-z]+)\b([^>]*?\bclass="([^"]*)"[^>]*)>')


def _painted(chart, colours):
    """The chart with SVG_PAINT's attributes on its parts, where a part has none of its own."""
    def paint(found):
        tag, inside, classes = found.groups()
        extra = {}
        for name in classes.split():
            extra.update(SVG_PAINT.get(name, {}))
        missing = [f'{k}="{v.format_map(_ColourNames(colours))}"' for k, v in extra.items()
                   if not re.search(rf'\s{re.escape(k)}=', inside)]
        if not missing:
            return found.group(0)
        closed = inside.rstrip().endswith("/")
        inside = inside.rstrip().rstrip("/").rstrip() if closed else inside
        return f"<{tag}{inside} {' '.join(missing)}{'/' if closed else ''}>"
    return SVG_PART.sub(paint, chart)


class _ColourNames(dict):
    def __init__(self, colours):
        super().__init__({k.removeprefix("--mdc-"): v for k, v in colours.items()})

    def __missing__(self, key):
        return "#5A6875"


def _with_charts(body, charts):
    """Each ```chart block, in order, replaced by the chart the editor drew of it (mdchart's SVG).
    Only drawn into this PDF, which fetches nothing; one that carries a script is left as its text.
    WeasyPrint leaves var() unresolved inside an SVG - every bar came out black - so the colours are
    written in."""
    pending = iter(charts)
    colours = _chart_colours()

    def swap(found):
        chart = next(pending, None)
        if not chart or "<script" in chart.lower() or "<foreignobject" in chart.lower():
            return found.group(0)
        chart = re.sub(r"var\((--mdc-[\w-]+)\)", lambda m: colours.get(m.group(1), m.group(0)), chart)
        return _painted(chart, colours)
    return CHART_BLOCK.sub(swap, body)


def make(text, fmt, images=None, charts=None):
    """(file name, bytes, media type) for the document in `text`, as Markdown or as a PDF.

    For a PDF, `images` {file id: data URL} are the person's own pictures it shows (/chat/files/<id>),
    put in the page so nothing is fetched, and `charts` the drawings of its ```chart blocks."""
    if fmt not in FORMATS:
        raise WrittenError(f"{fmt} is not a kind of document this server writes.")
    extension, mime = FORMATS[fmt]
    text = document_part(text)
    title = title_of(text)
    if fmt == "markdown":
        return file_name(title, extension), text.encode("utf-8"), mime
    try:
        from weasyprint import HTML
    except Exception as e:
        log.warning("WeasyPrint is not usable: %s", e)
        raise WrittenError("PDFs cannot be made on this server; ask for Markdown instead.")
    body = markdown.render(text)
    if body is None:
        body = "<pre>" + (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) + "</pre>"
    style = PDF_STYLE
    if charts:
        body = _with_charts(body, charts)
        style += _chart_style()
    if images:
        body = CHAT_IMAGE.sub(lambda m: f'<img src="{images[int(m.group(1))]}"' if int(m.group(1)) in images
                              else m.group(0), body)
    def drawn(css):
        page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{_escape(title)}</title>"
                f"<style>{css}</style></head><body>{body}</body></html>")
        return HTML(string=page, url_fetcher=_fetcher()).render()

    try:
        if is_resume(text):
            pdf = _fitted(drawn, style)
        else:
            pdf = drawn(style).write_pdf()
    except Exception as e:
        log.warning("Could not draw a PDF: %s", e)
        raise WrittenError("The PDF could not be drawn; the document is in the answer above.")
    return file_name(title, extension), pdf, mime


# A resume, read from the document's own headings rather than the request: a heading that is a
# whole career-history section, and another that is education, skills or the like. Headings only, and
# the whole heading, so a report with a "Summary" and "Projects", or an "Education budget", keeps
# the standard layout and size every other document has.
_SECTION = r"^#{2,3}\s+\**\s*(?:professional\s+|work\s+|technical\s+|core\s+|key\s+|relevant\s+)?(?:%s)\s*\**\s*$"
CAREER_SECTION = re.compile(_SECTION % r"experience|employment(?:\s+history)?|work\s+history|career(?:\s+history)?",
                            re.I | re.M)
OTHER_SECTION = re.compile(_SECTION % r"education|skills|competencies|(?:personal\s+)?projects|certifications?|"
                                       r"achievements|publications|languages|awards|profile|summary",
                           re.I | re.M)


def is_resume(text):
    return bool(CAREER_SECTION.search(text or "")) and bool(OTHER_SECTION.search(text or ""))


def resume_style(size):
    """A resume's layout at a body size of `size` pt: tight spacing, and a page broken wherever the
    text reaches - a section heading keeps only its next line with it, and a bullet may go on over
    the page - so no page ends in a gap."""
    return f"""
@page {{ size: A4; margin: 13mm 15mm 14mm;
        @bottom-center {{ content: counter(page) " / " counter(pages); font-size: 8pt; color: #8a8a96; }} }}
html {{ font-size: {size}pt; line-height: 1.38; }}
h1 {{ font-size: {size * 2.05:.2f}pt; margin: 0 0 5pt; padding-bottom: 4pt; border-bottom-width: 1.6pt; }}
h2 {{ font-size: {size * 1.2:.2f}pt; margin: 10pt 0 4pt; padding-bottom: 2pt; border-bottom: 0.6pt solid #c7d2fe;
      letter-spacing: .02em; }}
h3 {{ font-size: {size * 1.04:.2f}pt; margin: 7pt 0 1pt; }}
h1 + p {{ margin-bottom: 4pt; }}
p {{ margin: 0 0 3pt; }}
ul, ol {{ margin: 1pt 0 4pt; padding-left: 13pt; }}
li {{ margin: 0 0 1.5pt; }}
p, li {{ orphans: 2; widows: 2; }}
h2, h3 {{ break-after: avoid; }}
table {{ font-size: {size * 0.92:.2f}pt; margin: 4pt 0 6pt; }}
th, td {{ padding: 3pt 5pt; }}
"""


# pt, largest first: below 9.5 a resume stops being comfortable to read, above 11.5 it looks padded
RESUME_SIZES = (11.5, 11.25, 11, 10.75, 10.5, 10.25, 10, 9.75, 9.5)


def _fitted(drawn, style):
    """A resume drawn at the largest size in RESUME_SIZES that still needs as few pages as the
    smallest: no page more for a few lines, and the last page as full as the text can make it.
    About a quarter of a second a try."""
    smallest = drawn(style + resume_style(RESUME_SIZES[-1]))
    fewest = len(smallest.pages)
    for size in RESUME_SIZES[:-1]:
        tried = drawn(style + resume_style(size))
        if len(tried.pages) <= fewest:
            return tried.write_pdf()
    return smallest.write_pdf()


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
