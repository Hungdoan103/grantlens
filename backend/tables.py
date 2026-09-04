"""tables.py — trích văn bản GIỮ CẤU TRÚC BẢNG từ PDF/DOCX trước bước RAG.

Vấn đề: hồ sơ xin ngân sách hay chứa bảng tài chính; nếu đọc PDF thô rồi chunk theo đoạn,
hàng/cột bị trộn lẫn -> RAG lấy sai dữ kiện. Ở đây:
  - PDF  : pdfplumber phân tích layout từng trang; vùng bảng -> render dạng pipe (| ô | ô |),
           phần chữ ngoài bảng giữ theo dòng chảy tự nhiên.
  - DOCX : đọc đúng thứ tự đoạn/bảng trong document.xml; bảng -> pipe.
  - Bảng được bọc [BẢNG n] ... [/BẢNG n] và KHÔNG có dòng trống bên trong
    -> rag.chunk_text (tách theo dòng trống) giữ mỗi bảng nguyên vẹn thành MỘT chunk.
  - PDF scan (trang không có lớp chữ): gắn cảnh báo cần OCR; nếu cài pytesseract + Tesseract
    thì tự OCR trang đó (tùy chọn, không bắt buộc cho bản demo).
"""
import io, re


class ScannedPDFError(Exception):
    pass


def _table_to_pipe(rows, idx: int) -> str:
    lines = [f"[BẢNG {idx}]"]
    for r in rows:
        cells = [re.sub(r"\s+", " ", str(c or "")).strip() for c in r]
        if any(cells):
            lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"[/BẢNG {idx}]")
    return "\n".join(lines)


def _ocr_page(page):
    """OCR dự phòng cho trang scan — chỉ chạy nếu đã cài pytesseract + Tesseract."""
    try:
        import pytesseract
        img = page.to_image(resolution=200).original
        return pytesseract.image_to_string(img)
    except Exception:
        return None


def pdf_to_text(data: bytes) -> dict:
    """Trả {text, n_tables, n_pages, scanned_pages, ocr_used}."""
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
            if not body.strip() and not tables:  # trang không có lớp chữ -> scan
                ocr = _ocr_page(page)
                if ocr and ocr.strip():
                    body, ocr_used = ocr, True
                else:
                    scanned.append(pno)
                    body = f"[TRANG {pno}: ảnh scan — chưa trích được chữ, cần OCR]"
            parts = [body.strip()] if body.strip() else []
            for t in tables:
                n_tables += 1
                parts.append(_table_to_pipe(t.extract(), n_tables))
            out.append("\n\n".join(parts))
    return {"text": "\n\n".join(p for p in out if p.strip()), "n_tables": n_tables,
            "n_pages": len(out), "scanned_pages": scanned, "ocr_used": ocr_used}


def docx_to_text(data: bytes) -> dict:
    """Đọc .docx theo đúng thứ tự đoạn văn / bảng."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(io.BytesIO(data))
    out, n_tables = [], 0
    # duyệt body theo thứ tự thật (đoạn xen kẽ bảng)
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
