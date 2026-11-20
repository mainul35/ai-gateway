"""app/tools/written.py make_page_pdf: a page the model wrote (a CV in a chosen design) as a PDF in
its own design, from the "Save as PDF" over its preview. Nothing is fetched, and a page that only a
script fills is refused rather than printed blank.

    python -m pytest -q tests/test_page_pdf.py
"""
import io

import pytest
from pypdf import PdfReader

from app.tools import written

PAGE = """<!doctype html><html><head><title>A Person - CV</title>
<link href="https://fonts.example.com/serif.css" rel="stylesheet">
<style>body { font-family: Georgia, serif; } h1 { letter-spacing: 4px; }</style></head>
<body><h1>A PERSON</h1><p><a href="https://example.com/in/someone">LinkedIn</a></p>
<h2>Experience</h2><ul><li>Built things</li></ul></body></html>"""


def test_a_page_becomes_a_pdf_named_by_its_title_with_its_links():
    name, data, mime = written.make_page_pdf(PAGE)
    assert name == "A Person - CV.pdf" and mime == "application/pdf" and data.startswith(b"%PDF")
    page = PdfReader(io.BytesIO(data)).pages[0]
    assert "Built things" in page.extract_text()
    links = [a.get_object()["/A"]["/URI"] for a in page.get("/Annots") or []]
    assert links == ["https://example.com/in/someone"]


def test_a_page_only_a_script_fills_is_refused():
    with pytest.raises(written.WrittenError, match="script"):
        written.make_page_pdf("<html><body><div id=app></div><script>app.innerHTML='hi'</script></body></html>")


def test_its_title_falls_back_to_the_first_heading():
    assert written.page_title("<body><h1>Jane <b>Doe</b></h1></body>") == "Jane Doe"
    assert written.page_title("<p>no heading</p>") == "Page"
