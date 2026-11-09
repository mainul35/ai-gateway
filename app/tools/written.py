"""Documents the playground writes for you: the model writes Markdown, and this makes the file.

Asked for "a document", a model answers in chat with the same words it would have used anyway, and
there is nothing to keep. So the request says which file it wants - Markdown or PDF - the model is
told to write the document itself and nothing around it, and the answer is turned into that file:
Markdown as it was written, a PDF drawn from the same HTML the playground shows (WeasyPrint).

A PDF is laid out with the fonts this server has. Bangla needs a font that has it (Noto Sans
Bengali, from the fonts-noto-core package); without one its letters come out as empty boxes.
"""
import logging
import re
import unicodedata

from app.tools import markdown

log = logging.getLogger("tools.written")

FORMATS = {"markdown": ("md", "text/markdown; charset=utf-8"), "pdf": ("pdf", "application/pdf")}

WRITING_PROMPT = """The user has asked for a document. Write the whole document now, in Markdown, from \
their request and everything in the conversation that it is about.

- Begin with one "# " title naming what the document is about.
- Use headings, short paragraphs, lists, tables and code blocks wherever they help a reader.
- Write only the document. No preface such as "here is your document", no offer at the end, no \
questions back: what you write is saved as the file exactly as it is.
- Write in the language the user writes in."""

# Only what a document needs, so a PDF reads like one rather than like a web page
PDF_STYLE = """
@page { size: A4; margin: 20mm 18mm 22mm;
        @bottom-center { content: counter(page) " / " counter(pages); font-size: 8.5pt; color: #777; } }
html { font-family: "Noto Sans", "Noto Sans Bengali", "DejaVu Sans", sans-serif; font-size: 10.5pt;
       line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 20pt; margin: 0 0 10pt; padding-bottom: 6pt; border-bottom: 1.5pt solid #4f46e5; }
h2 { font-size: 14.5pt; margin: 16pt 0 6pt; color: #312e81; }
h3 { font-size: 12pt; margin: 12pt 0 4pt; }
h1, h2, h3, h4 { break-after: avoid; }
p, li { orphans: 3; widows: 3; }
a { color: #4338ca; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; background: #f1f1f4;
       padding: 0 2pt; border-radius: 2pt; }
pre { background: #f5f5f8; border: 0.5pt solid #ddd; border-radius: 3pt; padding: 7pt 9pt;
      white-space: pre-wrap; overflow-wrap: anywhere; break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 8pt 0; font-size: 9.5pt; break-inside: auto; }
th, td { border: 0.5pt solid #c8c8d0; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #eeeef6; }
tr { break-inside: avoid; }
blockquote { margin: 8pt 0; padding: 2pt 10pt; border-left: 2.5pt solid #c7c7e0; color: #444; }
img { max-width: 100%; }
hr { border: 0; border-top: 0.5pt solid #ccc; margin: 12pt 0; }
"""


class WrittenError(Exception):
    """The file could not be made; the message says why, for a person."""


def title_of(text):
    """The document's title: its first heading, or failing that its first line."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        heading = re.match(r"#{1,6}\s+(.*)", line)
        return re.sub(r"[*_`]", "", heading.group(1) if heading else line).strip()[:120] or "Document"
    return "Document"


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
    never fetched, so a document cannot be used to make this server read an address on its network."""
    if url.startswith("data:"):
        from weasyprint import default_url_fetcher
        return default_url_fetcher(url, *args, **kwargs)
    raise ValueError("documents fetch nothing")


def make(text, fmt):
    """(file name, bytes, media type) for the document in `text`, as Markdown or as a PDF."""
    if fmt not in FORMATS:
        raise WrittenError(f"{fmt} is not a kind of document this server writes.")
    extension, mime = FORMATS[fmt]
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
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{_escape(title)}</title>"
            f"<style>{PDF_STYLE}</style></head><body>{body}</body></html>")
    try:
        pdf = HTML(string=page, url_fetcher=_no_fetching).write_pdf()
    except Exception as e:
        log.warning("Could not draw a PDF: %s", e)
        raise WrittenError("The PDF could not be drawn; the document is in the answer above.")
    return file_name(title, extension), pdf, mime


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
