"""Diagnostic: dump Docling's view of the target PDF so we can calibrate
two-column (Observation | Reply) detection. Not part of the pipeline — a probe.

Prints, per page: page width/height, and each text item's bbox + a guess of
which column it falls in (left/right of the page x-midpoint).

Usage:
    python scripts/inspect_layout.py --pdf "data/Corporate office IR 2019-20.pdf" --pages 1-4
"""
from __future__ import annotations

import argparse

from docling.document_converter import DocumentConverter


def parse_pages(s: str | None):
    if not s:
        return None
    out = set()
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default=None)
    args = ap.parse_args()
    want = parse_pages(args.pages)

    conv = DocumentConverter()
    doc = conv.convert(args.pdf).document

    # Page geometry
    page_dims = {}
    for pno, page in doc.pages.items():
        size = getattr(page, "size", None)
        w = getattr(size, "width", None) if size else None
        h = getattr(size, "height", None) if size else None
        page_dims[pno] = (w, h)
        if want is None or pno in want:
            print(f"PAGE {pno}: width={w} height={h}")

    # Text items with provenance (bbox + page)
    for item, _level in doc.iterate_items():
        prov = getattr(item, "prov", None)
        text = getattr(item, "text", None)
        label = getattr(item, "label", None)
        if not prov or text is None:
            continue
        p = prov[0]
        pno = p.page_no
        if want is not None and pno not in want:
            continue
        bbox = p.bbox
        w = page_dims.get(pno, (None, None))[0]
        cx = (bbox.l + bbox.r) / 2 if bbox else None
        col = "?"
        if w and cx is not None:
            col = "L" if cx < w / 2 else "R"
        snippet = " ".join(text.split())[:70]
        print(f"  p{pno} [{col}] label={label} "
              f"l={bbox.l:.0f} r={bbox.r:.0f} t={bbox.t:.0f} b={bbox.b:.0f} :: {snippet}")

    print("\n--- tables ---")
    for t in getattr(doc, "tables", []):
        prov = getattr(t, "prov", None)
        pno = prov[0].page_no if prov else "?"
        try:
            md = t.export_to_markdown(doc)
        except Exception:
            md = t.export_to_markdown()
        print(f"TABLE on p{pno}, {len(md)} md chars")
        print(md[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
