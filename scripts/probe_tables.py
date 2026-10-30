"""Deep probe of Docling table structure for the target PDF, to design the
column-aware parser. Prints, per table: page, grid shape, and each cell's
(row, col, bbox-x-center, column-guess-by-x, text-snippet). Also locates the
7 known para descriptions across pages.
"""
from __future__ import annotations

import sys

from docling.document_converter import DocumentConverter

PDF = r"d:\CAG Chatbot\data\Corporate office IR 2019-20.pdf"

PARA_DESCS = {
    1: "two parcels of land in Noida",
    2: "HRT package in Tapovan Vishnugad",
    3: "Salami Charges",
    4: "Kudgi",
    5: "short supplies of coal",
    6: "Grade Slippage",
    7: "trade receivables",
}


def main() -> int:
    conv = DocumentConverter()
    doc = conv.convert(PDF).document

    page_w = {}
    for pno, page in doc.pages.items():
        sz = getattr(page, "size", None)
        page_w[pno] = getattr(sz, "width", None) if sz else None

    print(f"n_pages={len(doc.pages)} n_tables={len(doc.tables)}")
    for ti, t in enumerate(doc.tables):
        prov = getattr(t, "prov", None)
        pno = prov[0].page_no if prov else None
        data = t.data
        ncols = getattr(data, "num_cols", None)
        nrows = getattr(data, "num_rows", None)
        w = page_w.get(pno)
        print(f"\n==== TABLE {ti} page={pno} rows={nrows} cols={ncols} page_w={w}")
        cells = getattr(data, "table_cells", None) or []
        for c in cells:
            bbox = getattr(c, "bbox", None)
            cx = (bbox.l + bbox.r) / 2 if bbox else None
            colguess = "?"
            if w and cx is not None:
                colguess = "OBS" if cx < w / 2 else "REPLY"
            txt = " ".join((c.text or "").split())[:55]
            r0 = getattr(c, "start_row_offset_idx", None)
            c0 = getattr(c, "start_col_offset_idx", None)
            xrange = f"l={bbox.l:.0f} r={bbox.r:.0f}" if bbox else "no-bbox"
            print(f"   r{r0} c{c0} {xrange} cx={cx} [{colguess}] :: {txt}")

    print("\n==== PARA HEADING LOCATIONS (search all item text) ====")
    for item, _ in doc.iterate_items():
        txt = getattr(item, "text", None)
        if not txt:
            continue
        for pno_desc, needle in PARA_DESCS.items():
            if needle.lower() in txt.lower():
                prov = getattr(item, "prov", None)
                pg = prov[0].page_no if prov else "?"
                print(f"  para{pno_desc} needle={needle!r} on page {pg}: "
                      f"{' '.join(txt.split())[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
