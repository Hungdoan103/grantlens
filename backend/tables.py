"""tables.py — extract text from PDF/DOCX while PRESERVING TABLE STRUCTURE, before the RAG step.

Problem: grant applications often contain financial tables; reading a PDF raw and chunking by paragraph mixes
rows and columns, so RAG retrieves the wrong facts. Here:
  - PDF  : pdfplumber analyses the layout of each page; table regions are rendered as pipes (| cell | cell |),
           text outside tables keeps its natural flow.
  - DOCX : paragraphs and tables are read in document order from document.xml; tables -> pipes.
  - Tables are wrapped in [TABLE n] ... [/TABLE n] with NO blank line inside
    -> rag.chunk_text (which splits on blank lines) keeps each table intact as ONE chunk.
  - Scanned PDF (page without a text layer): flagged as needing OCR; if pytesseract + Tesseract are installed
    the page is OCR'd automatically (optional, not required for the demo build).
"""
import io, re


class ScannedPDFError(Exception):
    pass


def _table_to_pipe(rows, idx: int) -> str:
    lines = [f"[TABLE {idx}]"]
    for r in rows:
        cells = [re.sub(r"\s+", " ", str(c or "")).strip() for c in r]
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"[/TABLE {idx}]")
    return "\n".join(lines)


def _find_tesseract():
    import shutil, os
    return (shutil.which("tesseract")
            or next((p for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
                     if os.path.exists(p)), None))


def _ocr_page(page):
    """Fallback OCR for a scanned page — runs when Tesseract is installed (auto-detected on Windows)."""
    try:
        import pytesseract
        exe = _find_tesseract()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        img = page.to_image(resolution=200).original
        return pytesseract.image_to_string(img)
    except Exception:
        return None


def pdf_to_text(data: bytes) -> dict:
    """Returns {text, n_tables, n_pages, scanned_pages, ocr_used}."""
    import pdfplumber
    out, n_tables, scanned, ocr_used = [], 0, [], False
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            tables = page.find_tables()
            regions = [t.bbox for t in tables]

            def outside(obj):
                cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in regions)

            body = page.filter(outside).extract_text() or ""
            if not body.strip() and not tables:  # page without a text layer -> scanned
                ocr = _ocr_page(page)
                if ocr and ocr.strip():
                    body, ocr_used = ocr, True
                else:
                    scanned.append(pno)
                    body = f"[PAGE {pno}: scanned image — no text extracted, OCR needed]"
            parts = [body.strip()] if body.strip() else []
            for t in tables:
                n_tables += 1
                parts.append(_table_to_pipe(t.extract(), n_tables))
            out.append("\n\n".join(parts))
    return {"text": "\n\n".join(p for p in out if p.strip()), "n_tables": n_tables,
            "n_pages": len(out), "scanned_pages": scanned, "ocr_used": ocr_used}


def docx_to_text(data: bytes) -> dict:
    """Read a .docx in true paragraph / table order."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(io.BytesIO(data))
    out, n_tables = [], 0
    # walk the body in real order (paragraphs interleaved with tables)
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            t = Paragraph(child, doc).text.strip()
            if t:
                out.append(t)
        elif child.tag.endswith("}tbl"):
            n_tables += 1
            rows = [[cell.text for cell in row.cells] for row in Table(child, doc).rows]
            out.append(_table_to_pipe(rows, n_tables))
    return {"text": "\n\n".join(out), "n_tables": n_tables, "n_pages": None,
            "scanned_pages": [], "ocr_used": False}


def file_to_text(filename: str, data: bytes) -> dict:
    n = filename.lower()
    if n.endswith(".pdf"):
        return pdf_to_text(data)
    if n.endswith(".docx"):
        return docx_to_text(data)
    return {"text": data.decode("utf-8", errors="replace"), "n_tables": 0,
            "n_pages": None, "scanned_pages": [], "ocr_used": False}
