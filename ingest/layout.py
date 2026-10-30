"""Docling layout parsing with x-coordinate column clustering.

The CAG IR pages are landscape two-column: left = Audit Observation,
right = Management Reply. Docling captures most content as tables; we also
pick up free text items. Every text-bearing block is assigned to a column by
clustering block x-centres per page (falling back to the page midpoint), which
is the geometric column split CLAUDE.md mandates (never naive pdftotext).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ingest import textrepair


@dataclass
class Block:
    page: int
    x_center: float
    l: float
    r: float
    top: float          # y of top edge (for reading order)
    side: str           # "obs" | "reply"
    text: str
    table_idx: int | None   # docling table this came from, else None
    is_header: bool         # "Audit Observation"/"Management Reply" column header


@dataclass
class Table:
    table_idx: int
    page: int
    side: str
    ncols: int
    markdown: str
    rows_json: list[list[str]]


@dataclass
class PageInfo:
    page: int
    width: float
    height: float
    split_x: float      # the obs|reply column boundary used


def _compact_markdown(md: str | None) -> str | None:
    if not md:
        return md
    lines = []
    for line in md.splitlines():
        line = re.sub(r" {2,}", " ", line).strip()
        if line:
            lines.append(line)
    out, prev_sep = [], False
    for ln in lines:
        is_sep = bool(re.fullmatch(r"[|\-:\s]+", ln))
        if is_sep and prev_sep:
            continue
        out.append(ln)
        prev_sep = is_sep
    return "\n".join(out)


def _rows_json(grid) -> list[list[str]]:
    if not grid:
        return []
    return [[textrepair.collapse_ws(getattr(c, "text", "") or "") for c in row]
            for row in grid]


def _column_split(centers: list[float], page_width: float) -> float:
    """Cluster x-centres into left/right and return the boundary. Finds the
    widest gap between consecutive centres in the central band of the page;
    falls back to page_width/2 when there is no clear bimodal split."""
    mid = page_width / 2.0
    band = sorted(c for c in centers if 0.25 * page_width <= c <= 0.75 * page_width)
    if len(band) < 2:
        return mid
    best_gap, best_x = 0.0, mid
    for a, b in zip(band, band[1:]):
        gap = b - a
        if gap > best_gap:
            best_gap, best_x = gap, (a + b) / 2.0
    # only trust the clustered split if the gap is meaningful (> 6% of width)
    return best_x if best_gap >= 0.06 * page_width else mid


def parse_pdf(pdf_path: str, ocr: bool = False):
    """Return (blocks, tables, pages). ocr=True enables Docling's OCR fallback
    for scanned pages (unused for the digital IRs, but available)."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    opts = PdfPipelineOptions()
    opts.do_ocr = ocr
    conv = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    doc = conv.convert(pdf_path).document

    pages: dict[int, PageInfo] = {}
    page_w: dict[int, float] = {}
    for pno, page in doc.pages.items():
        sz = getattr(page, "size", None)
        w = getattr(sz, "width", 0.0) if sz else 0.0
        h = getattr(sz, "height", 0.0) if sz else 0.0
        page_w[pno] = w
        pages[pno] = PageInfo(page=pno, width=w, height=h, split_x=w / 2.0)

    raw_blocks: list[Block] = []
    tables: list[Table] = []

    # 1) table cells
    for ti, t in enumerate(doc.tables):
        prov = getattr(t, "prov", None)
        tpage = prov[0].page_no if prov else None
        data = t.data
        ncols = getattr(data, "num_cols", 0) or 0
        for c in (getattr(data, "table_cells", None) or []):
            txt = textrepair.collapse_ws(c.text or "")
            if not txt:
                continue
            bbox = getattr(c, "bbox", None)
            if bbox is None or tpage is None:
                continue
            cx = (bbox.l + bbox.r) / 2
            is_hdr = txt.lower() in ("audit observation", "management reply")
            raw_blocks.append(Block(page=tpage, x_center=cx, l=bbox.l, r=bbox.r,
                                    top=bbox.t, side="obs", text=txt,
                                    table_idx=ti, is_header=is_hdr))
        if ncols >= 3:  # data table (not the 2-col layout container)
            md = None
            try:
                md = t.export_to_markdown(doc)
            except Exception:
                try:
                    md = t.export_to_markdown()
                except Exception:
                    md = None
            tables.append(Table(table_idx=ti, page=tpage or 0, side="obs",
                                ncols=ncols, markdown=_compact_markdown(md) or "",
                                rows_json=_rows_json(getattr(data, "grid", None))))

    # 2) free text items (paragraphs Docling did not fold into a table)
    for item, _lvl in doc.iterate_items():
        if item in (t for t in doc.tables):
            continue
        prov = getattr(item, "prov", None)
        txt = getattr(item, "text", None)
        if not prov or not txt:
            continue
        p = prov[0]
        bbox = getattr(p, "bbox", None)
        if bbox is None:
            continue
        txt = textrepair.collapse_ws(txt)
        if not txt:
            continue
        cx = (bbox.l + bbox.r) / 2
        raw_blocks.append(Block(page=p.page_no, x_center=cx, l=bbox.l, r=bbox.r,
                                top=bbox.t, side="obs", text=txt,
                                table_idx=None, is_header=False))

    # 3) per-page column split + side assignment
    per_page_centers: dict[int, list[float]] = {}
    for b in raw_blocks:
        per_page_centers.setdefault(b.page, []).append(b.x_center)
    for pno, centers in per_page_centers.items():
        w = page_w.get(pno, 0.0) or 841.0
        split = _column_split(centers, w)
        if pno in pages:
            pages[pno].split_x = split
    for b in raw_blocks:
        split = pages[b.page].split_x if b.page in pages else (page_w.get(b.page, 841.0) / 2)
        b.side = "reply" if b.x_center >= split else "obs"
    for tb in tables:
        split = pages[tb.page].split_x if tb.page in pages else 420.0
        # a data table's side = side of its cells' median center (approx via idx)
        cells = [b for b in raw_blocks if b.table_idx == tb.table_idx]
        if cells:
            med = sorted(b.x_center for b in cells)[len(cells) // 2]
            tb.side = "reply" if med >= split else "obs"

    return raw_blocks, tables, pages
