"""app/tools/documents.py: a document's clickable words keep the address behind them. A CV's
"LinkedIn | GitHub" are only words in its text; without the addresses the model guessed a profile
link. Nothing is called.

    python -m pytest -q tests/test_document_links.py
"""
import io

from app.tools import documents


def a_pdf_with_a_link(address):
    from pypdf import PdfWriter
    from pypdf.annotations import Link

    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    link = Link(rect=(10, 10, 90, 30), url=address)
    writer.add_annotation(0, link)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_a_pdf_link_address_is_kept():
    from pypdf import PdfReader
    page = PdfReader(io.BytesIO(a_pdf_with_a_link("https://example.com/in/someone"))).pages[0]
    assert documents._pdf_links(page) == ["https://example.com/in/someone"]


def test_only_web_and_mail_links_count():
    from pypdf import PdfReader
    page = PdfReader(io.BytesIO(a_pdf_with_a_link("javascript:alert(1)"))).pages[0]
    assert documents._pdf_links(page) == []


def test_a_word_link_address_is_kept():
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    document = docx.Document()
    paragraph = document.add_paragraph("Profiles: ")
    rel = document.part.relate_to("https://example.com/in/someone", RELATIONSHIP_TYPE.HYPERLINK,
                                  is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel)
    run = OxmlElement("w:r")
    words = OxmlElement("w:t")
    words.text = "LinkedIn"
    run.append(words)
    link.append(run)
    paragraph._p.append(link)
    out = io.BytesIO()
    document.save(out)
    text = documents.extract("cv.docx", out.getvalue())
    assert "LinkedIn: https://example.com/in/someone" in text


def test_the_model_is_told_not_to_make_up_an_address():
    assert "ask the user" in documents.context_block([("cv.pdf", "words")])
