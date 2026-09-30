"""Real document-text extraction for read_file's document-extension branch.

Used by both the old and new read paths via mocks.extract_document_bytes.
Kept as its own module (not folded into mocks.py) so python-docx/openpyxl/
python-pptx are only imported when a matching extension is actually read,
not on every mocks.py import.

.pdf is intentionally NOT handled here yet - EXTRACTABLE_EXTENSIONS (mocks.py)
still lists it, so it reaches `extract()` below and gets a clean "not
supported yet" from the caller (extract() returns None for it) rather than a
crash - pending a library choice (pdfplumber: real table extraction, pulls in
a compiled pypdfium2 binary; pypdf: tiny/pure-Python, text only, no tables).
"""

from __future__ import annotations

import io
import json
from pathlib import Path


def extract(data: bytes, path: str):
    """Return plain text for a supported document extension, or None."""
    ext = Path(path).suffix.lower()
    if ext == ".ipynb":
        return _extract_ipynb(data)
    if ext == ".docx":
        return _extract_docx(data)
    if ext == ".xlsx":
        return _extract_xlsx(data)
    if ext == ".pptx":
        return _extract_pptx(data)
    return None


def _extract_ipynb(data: bytes):
    try:
        nb = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    texts = []
    for cell in nb.get("cells", []):
        source = cell.get("source", [])
        if isinstance(source, list):
            texts.append("".join(source))
        elif isinstance(source, str):
            texts.append(source)
    return "\n\n".join(texts).strip()


def _render_grid(rows) -> str:
    """Plain pipe-separated grid - not a markdown table (no alignment dashes):
    nothing renders this output as markdown, and the dashes would just be
    noise a model has to parse back out."""
    return "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)


def _extract_docx(data: bytes) -> str:
    from docx import Document
    from docx.document import Document as _DocumentObj
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    def iter_block_items(parent):
        """Walk body children in real document order (standard python-docx
        recipe) - paragraphs and tables interleaved as they actually appear,
        not all paragraphs then all tables."""
        if isinstance(parent, _DocumentObj):
            parent_elm = parent.element.body
        elif isinstance(parent, _Cell):
            parent_elm = parent._tc
        else:
            raise ValueError(f"unsupported docx parent: {type(parent)}")
        for child in parent_elm.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, parent)
            elif isinstance(child, CT_Tbl):
                yield Table(child, parent)

    doc = Document(io.BytesIO(data))
    parts = []
    for block in iter_block_items(doc):
        if isinstance(block, Paragraph):
            if block.text.strip():
                parts.append(block.text)
        else:  # Table
            rows = [[cell.text for cell in row.cells] for row in block.rows]
            parts.append(_render_grid(rows))
    return "\n\n".join(parts)


def _extract_xlsx(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        rows = [row for row in ws.iter_rows(values_only=True)
                if row and any(c is not None for c in row)]
        if rows:
            parts.append(f"[Sheet: {ws.title}]\n" + _render_grid(rows))
        else:
            parts.append(f"[Sheet: {ws.title}] (empty)")
    return "\n\n".join(parts)


def _extract_pptx(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    parts = []
    for i, slide in enumerate(prs.slides, start=1):
        slide_parts = [f"[Slide {i}]"]
        for shape in slide.shapes:
            if shape.has_table:
                rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
                slide_parts.append(_render_grid(rows))
            elif shape.has_text_frame and shape.text_frame.text.strip():
                slide_parts.append(shape.text_frame.text)
        parts.append("\n".join(slide_parts))
    return "\n\n".join(parts)
