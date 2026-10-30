"""Phase 1 ingestion for exactly ONE PDF (Corporate Office IR 2019-20).

Pipeline:
  1. Read the Excel index rows for TARGET_PERIOD (the 7 IIB paras).
  2. Parse the PDF with Docling. The two-column Observation | Reply layout is
     captured by Docling as tables; we classify every table cell into the
     Observation or Reply column by its x-coordinate (page midpoint), which is
     the column-aware approach CLAUDE.md mandates (never naive pdftotext).
  3. Segment into paras by the inline "Para N, Part IIB ..." heading cells.
  4. Normalize broken drop-caps ("1. T he" -> "1. The", "3. N TPC" -> "NTPC").
  5. Preserve financial tables (Docling tables with >=3 columns) as ATOMIC
     units: markdown + JSON, never split across chunks.
  6. Build structured para records {para_key, title, observation_text,
     reply_text, tables_markdown[], pages, source_pdf, column_confidence}.
  7. Reconcile PDF paras against Excel rows and report every mismatch.
  8. Write a side-by-side diff report (column-aware parsed vs naive interleaved)
     so parse quality can be eyeballed per para; flag low-confidence paras.
  9. Store records in Postgres (idempotent UPSERT). If Postgres is unreachable
     (e.g. Docker not up yet), records are written to structured_paras.json and
     the DB step is reported as deferred — never silently skipped, never faked.
 10. Chunk observation/reply separately (~CHUNK_TOKENS, CHUNK_OVERLAP; tables
     atomic), embed with BGE-M3 (dense+sparse), upsert to Qdrant. Deterministic
     point ids make re-runs idempotent (no duplicate vectors).

Usage:
  python scripts/ingest_one.py                 # full run
  python scripts/ingest_one.py --report-only   # parse + reconcile + diff only
  python scripts/ingest_one.py --recreate       # recreate Qdrant collection
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid

# Ensure repo root on path so `import app...` works when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl  # noqa: E402

from app.config import get_settings  # noqa: E402

PARA_HEADING_RE = re.compile(
    r"Para\s*(?:No\.?\s*)?(\d+)\s*,?\s*Part\s*[-–—\s]*?(II[AB])", re.IGNORECASE
)
NAMESPACE = uuid.UUID("6f9b7c2e-1a2b-4c3d-8e9f-0a1b2c3d4e5f")
NOT_FOUND = "not found in the indexed reports"


# --------------------------------------------------------------------------- #
# Text normalization
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    if not text:
        return ""
    t = text.replace(" ", " ")
    t = re.sub(r"\s+", " ", t).strip()
    # Drop-cap after a section number: "1. T he" -> "1. The"; "3. N TPC" -> "NTPC"
    t = re.sub(r"(\b\d+\.\s+)([A-Z]) ([A-Za-z])", r"\1\2\3", t)
    # Drop-cap at the very start of a cell/segment: "T he ..." -> "The ..."
    t = re.sub(r"^([A-Z]) ([A-Za-z])", r"\1\2", t)
    return t.strip()


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def load_excel_rows(path: str, period: str) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    out = []
    for r in rows[1:]:
        if r[5] is not None and str(r[5]).strip() == period:
            out.append(
                {
                    "sr_no": r[0],
                    "project": str(r[4]).strip() if r[4] else "",
                    "period": str(r[5]).strip(),
                    "addition_year": str(r[6]).strip() if r[6] else "",
                    "para_type": str(r[7]).strip() if r[7] else "",
                    "para_no": str(r[8]).strip() if r[8] is not None else "",
                    "description": str(r[9]).strip() if r[9] else "",
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Docling cell extraction
# --------------------------------------------------------------------------- #
class Cell:
    __slots__ = ("table_idx", "row", "col", "page", "cx", "side", "text",
                 "ncols", "is_header")

    def __init__(self, table_idx, row, col, page, cx, side, text, ncols, is_header):
        self.table_idx = table_idx
        self.row = row
        self.col = col
        self.page = page
        self.cx = cx
        self.side = side
        self.text = text
        self.ncols = ncols
        self.is_header = is_header


def parse_pdf(pdf_path: str):
    from docling.document_converter import DocumentConverter

    conv = DocumentConverter()
    doc = conv.convert(pdf_path).document

    page_w = {}
    for pno, page in doc.pages.items():
        sz = getattr(page, "size", None)
        page_w[pno] = getattr(sz, "width", None) if sz else None

    cells: list[Cell] = []
    tables_meta = []  # per docling table: {idx, page, ncols, markdown, grid}
    for ti, t in enumerate(doc.tables):
        prov = getattr(t, "prov", None)
        tpage = prov[0].page_no if prov else None
        data = t.data
        ncols = getattr(data, "num_cols", 0) or 0
        # atomic markdown + json for real financial tables (>=3 cols)
        md, grid = None, None
        if ncols >= 3:
            try:
                md = t.export_to_markdown(doc)
            except Exception:
                try:
                    md = t.export_to_markdown()
                except Exception:
                    md = None
            md = compact_markdown(md)
            grid = grid_to_json(getattr(data, "grid", None))
        tables_meta.append({"idx": ti, "page": tpage, "ncols": ncols,
                            "markdown": md, "grid": grid})

        for c in (getattr(data, "table_cells", None) or []):
            raw = c.text or ""
            txt = normalize(raw)
            if not txt:
                continue
            bbox = getattr(c, "bbox", None)
            page = tpage
            w = page_w.get(page)
            cx = (bbox.l + bbox.r) / 2 if bbox else None
            side = "obs"
            if w and cx is not None and cx >= w / 2:
                side = "reply"
            is_header = txt.strip().lower() in ("audit observation", "management reply")
            cells.append(
                Cell(ti, getattr(c, "start_row_offset_idx", 0),
                     getattr(c, "start_col_offset_idx", 0), page, cx, side,
                     txt, ncols, is_header)
            )
    return cells, tables_meta, page_w


def compact_markdown(md: str | None) -> str | None:
    """Docling renders table cells with huge whitespace padding (a single page
    table can be 20k+ chars of mostly spaces). Collapse the padding so tables
    are compact and don't flood the retrieval context."""
    if not md:
        return md
    lines = []
    for line in md.splitlines():
        line = re.sub(r" {2,}", " ", line).strip()
        # drop markdown separator rows that are only |, -, : and spaces
        if line and not re.fullmatch(r"[|\-:\s]+", line):
            lines.append(line)
        elif line.startswith("|") and set(line) <= set("|-: "):
            lines.append(line)  # keep one separator marker
    out, prev_sep = [], False
    for ln in lines:
        is_sep = bool(re.fullmatch(r"[|\-:\s]+", ln))
        if is_sep and prev_sep:
            continue
        out.append(ln)
        prev_sep = is_sep
    return "\n".join(out)


def grid_to_json(grid) -> list[list[str]] | None:
    if not grid:
        return None
    out = []
    for row in grid:
        out.append([normalize(getattr(cell, "text", "") or "") for cell in row])
    return out


# --------------------------------------------------------------------------- #
# Para segmentation
# --------------------------------------------------------------------------- #
def is_tabular_noise(cell: Cell) -> bool:
    """Short numeric/token cells inside a >=3-col table — captured in the atomic
    table, so keep them out of the narrative prose to reduce pollution."""
    if cell.ncols < 3:
        return False
    if len(cell.text) >= 60:
        return False
    # mostly digits / punctuation / short tokens
    letters = sum(ch.isalpha() for ch in cell.text)
    return letters <= 3 or bool(re.fullmatch(r"[\d.,%()/:\-\s]+", cell.text))


def segment_paras(cells: list[Cell], tables_meta: list[dict]) -> list[dict]:
    # reading order
    cells_sorted = sorted(cells, key=lambda c: (c.table_idx, c.row, c.col))

    paras: list[dict] = []
    current = None
    pre_heading_buf: list[Cell] = []

    def new_para(para_no, para_type, heading_tail, table_idx):
        return {
            "para_no": para_no,
            "para_type": para_type,
            "obs_parts": ([heading_tail] if heading_tail else []),
            "reply_parts": [],
            "pages": set(),
            "table_idxs": set(),
            "first_table": table_idx,
        }

    for c in cells_sorted:
        if c.is_header:
            continue
        m = PARA_HEADING_RE.search(c.text)
        if m:
            # close previous
            if current:
                paras.append(current)
            para_no = m.group(1)
            para_type = m.group(2).upper()
            tail = normalize(PARA_HEADING_RE.sub("", c.text, count=1))
            # strip leading punctuation left by the heading removal, then a
            # leading "Corporate Office IR <period>" descriptor if present
            tail = re.sub(r"^[\s,;:.\-–—]+", "", tail)
            tail = re.sub(r"^Corporate Office IR\s*\S*\s*", "", tail).strip()
            current = new_para(para_no, para_type, tail, c.table_idx)
            if c.page:
                current["pages"].add(c.page)
            current["table_idxs"].add(c.table_idx)
            # attach any pre-heading buffer (continuation of prior content) to
            # the previous para if one exists; else drop (front matter).
            pre_heading_buf.clear()
            continue

        if current is None:
            pre_heading_buf.append(c)
            continue

        if c.page:
            current["pages"].add(c.page)
        current["table_idxs"].add(c.table_idx)
        if is_tabular_noise(c):
            continue
        if c.side == "reply":
            current["reply_parts"].append(c.text)
        else:
            current["obs_parts"].append(c.text)

    if current:
        paras.append(current)
    return paras


# --------------------------------------------------------------------------- #
# Structured records
# --------------------------------------------------------------------------- #
def slug(project, period, para_type, para_no) -> str:
    return f"{project}|{period}|{para_type}|{para_no}"


def page_label(pages: set[int]) -> str:
    if not pages:
        return "p.?"
    lo, hi = min(pages), max(pages)
    return f"p.{lo}" if lo == hi else f"p.{lo}-{hi}"


def build_records(paras, excel_rows, tables_meta, source_pdf) -> tuple[list[dict], list[str]]:
    excel_by_no = {r["para_no"]: r for r in excel_rows}
    project = excel_rows[0]["project"] if excel_rows else "Corporate Office"
    period = excel_rows[0]["period"] if excel_rows else ""

    records = []
    warnings = []
    for p in paras:
        er = excel_by_no.get(p["para_no"])
        para_type = p["para_type"] or (er["para_type"] if er else "")
        title = er["description"] if er else " ".join(p["obs_parts"][:1])[:200]
        obs = "\n".join(x for x in p["obs_parts"] if x).strip()
        reply = "\n".join(x for x in p["reply_parts"] if x).strip()

        # atomic tables owned by this para (>=3-col tables among its table_idxs)
        tabs = [tm["markdown"] for tm in tables_meta
                if tm["idx"] in p["table_idxs"] and tm["markdown"]]
        tabs_json = [tm["grid"] for tm in tables_meta
                     if tm["idx"] in p["table_idxs"] and tm["grid"]]

        # confidence
        low = []
        if not reply:
            low.append("no-reply-column")
        if len(obs) < 120:
            low.append("short-observation")
        if not er:
            low.append("not-in-excel")
        conf = "low" if low else "high"
        if low:
            warnings.append(f"Para {p['para_no']}: LOW confidence ({', '.join(low)})")

        pk = slug(project, period, para_type, p["para_no"])
        records.append(
            {
                "para_key": pk,
                "project": project,
                "period": period,
                "para_type": para_type,
                "para_no": p["para_no"],
                "title": title,
                "observation_text": obs,
                "reply_text": reply,
                "tables_markdown": tabs,
                "tables_json": tabs_json,
                "page_start": min(p["pages"]) if p["pages"] else None,
                "page_end": max(p["pages"]) if p["pages"] else None,
                "page_label": page_label(p["pages"]),
                "source_pdf": os.path.basename(source_pdf),
                "column_confidence": conf,
            }
        )
    return records, warnings


def reconcile(records, excel_rows) -> list[str]:
    found = {r["para_no"] for r in records}
    expected = {r["para_no"] for r in excel_rows}
    msgs = []
    for n in sorted(expected - found, key=lambda x: int(x) if x.isdigit() else 0):
        msgs.append(f"RECONCILE: Excel para {n} NOT found in PDF")
    for n in sorted(found - expected, key=lambda x: int(x) if x.isdigit() else 0):
        msgs.append(f"RECONCILE: PDF para {n} NOT in Excel index")
    return msgs


# --------------------------------------------------------------------------- #
# Diff report (column-aware parsed vs naive interleaved)
# --------------------------------------------------------------------------- #
def naive_interleaved(cells: list[Cell], para_no: str, table_idxs: set[int]) -> str:
    """Approximate naive pdftotext: all this para's cells in pure top-of-page
    reading order, ignoring columns -> interleaves observation & reply."""
    sub = [c for c in cells if c.table_idx in table_idxs and not c.is_header]
    sub.sort(key=lambda c: (c.table_idx, c.row, c.col))
    return " ".join(c.text for c in sub)


def write_diff_report(path, records, paras_by_no, cells, warnings, reconcile_msgs):
    lines = []
    lines.append("# Ingestion diff report — Corporate Office IR 2019-20\n")
    lines.append("Column-aware parse (Observation / Reply split by x-coordinate) "
                 "vs naive interleaved extraction.\n")
    if reconcile_msgs:
        lines.append("## Reconciliation\n")
        lines.extend(f"- {m}" for m in reconcile_msgs)
        lines.append("")
    if warnings:
        lines.append("## Low-confidence flags\n")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")
    for r in records:
        p = paras_by_no.get(r["para_no"])
        lines.append("=" * 100)
        lines.append(f"## Para {r['para_no']} ({r['para_type']}) — {r['page_label']} "
                     f"— confidence={r['column_confidence']}")
        lines.append(f"para_key: {r['para_key']}")
        lines.append(f"title (from Excel): {r['title']}")
        lines.append(f"atomic tables: {len(r['tables_markdown'])}")
        lines.append("")
        lines.append("--- COLUMN-AWARE: OBSERVATION ---")
        lines.append(r["observation_text"][:1600] or "(empty)")
        lines.append("")
        lines.append("--- COLUMN-AWARE: MANAGEMENT REPLY ---")
        lines.append(r["reply_text"][:1600] or "(empty)")
        lines.append("")
        if p:
            lines.append("--- NAIVE INTERLEAVED (what pdftotext would give) ---")
            lines.append(naive_interleaved(cells, r["para_no"], p["table_idxs"])[:1600])
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_words(text: str, size_tokens: int, overlap_tokens: int):
    words = text.split()
    if not words:
        return []
    size = max(50, int(size_tokens * 0.75))
    overlap = max(0, int(overlap_tokens * 0.75))
    step = max(1, size - overlap)
    out, i = [], 0
    while i < len(words):
        out.append(" ".join(words[i:i + size]))
        i += step
    return out


def build_chunks(record: dict, s) -> list[dict]:
    chunks = []
    ordinal = 0

    def add(section, text):
        nonlocal ordinal
        cid = f"{record['para_key']}::{section}::{ordinal}"
        chunks.append(
            {
                "chunk_id": cid,
                "para_key": record["para_key"],
                "section": section,
                "ordinal": ordinal,
                "page": record["page_label"],
                "text": text,
            }
        )
        ordinal += 1

    for seg, section in ((record["observation_text"], "observation"),
                         (record["reply_text"], "reply")):
        for piece in chunk_words(seg, s.chunk_tokens, s.chunk_overlap):
            add(section, piece)
    # tables: one atomic chunk each, but skip oversized full-page layout tables
    # (they duplicate the narrative already chunked above and only add noise).
    for md in record["tables_markdown"]:
        if md and len(md) <= s.table_chunk_max_chars:
            add("table", md)
    return chunks


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def store_postgres(records) -> tuple[bool, str]:
    try:
        from app import db
        engine = db.get_engine()
        db.init_db(engine)
        with db.connection(engine) as conn:
            for r in records:
                db.upsert_para(conn, {
                    "para_key": r["para_key"], "project": r["project"],
                    "period": r["period"], "para_type": r["para_type"],
                    "para_no": r["para_no"], "title": r["title"],
                    "observation_text": r["observation_text"],
                    "reply_text": r["reply_text"],
                    "tables_markdown": r["tables_markdown"],
                    "page_start": r["page_start"], "page_end": r["page_end"],
                    "source_pdf": r["source_pdf"],
                    "column_confidence": r["column_confidence"],
                })
        return True, f"Postgres: upserted {len(records)} paras."
    except Exception as e:  # noqa: BLE001
        return False, f"Postgres UNREACHABLE ({type(e).__name__}: {e})."


def store_chunks_bookkeeping(all_chunks):
    try:
        from app import db
        engine = db.get_engine()
        with db.connection(engine) as conn:
            by_para = {}
            for ch in all_chunks:
                by_para.setdefault(ch["para_key"], []).append(ch)
            for pk, chs in by_para.items():
                db.replace_chunks(conn, pk, [
                    {"chunk_id": c["chunk_id"], "para_key": c["para_key"],
                     "section": c["section"], "ordinal": c["ordinal"],
                     "page_start": None, "page_end": None, "text": c["text"]}
                    for c in chs
                ])
        return True
    except Exception:
        return False


def embed_and_upsert(all_chunks, recreate: bool) -> str:
    from app.models_embed import embed_texts
    from app import qdrant_store

    qdrant_store.ensure_collection(recreate=recreate)
    texts = [c["text"] for c in all_chunks]
    vectors = embed_texts(texts)
    points = []
    for c, v in zip(all_chunks, vectors):
        pid = str(uuid.uuid5(NAMESPACE, c["chunk_id"]))
        points.append({
            "id": pid,
            "vector": v,
            "payload": {
                "chunk_id": c["chunk_id"], "para_key": c["para_key"],
                "section": c["section"], "page": c["page"], "text": c["text"],
            },
        })
    qdrant_store.upsert_chunks(points)
    return f"Qdrant: upserted {len(points)} chunk vectors (dense+sparse)."


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true",
                    help="parse + reconcile + diff report only (no DB/embed)")
    ap.add_argument("--recreate", action="store_true",
                    help="recreate the Qdrant collection before upsert")
    args = ap.parse_args()

    s = get_settings()
    print(f"[ingest] period={s.target_period} pdf={s.target_pdf}")
    excel_rows = load_excel_rows(s.excel_index, s.target_period)
    print(f"[ingest] Excel rows for period: {len(excel_rows)} "
          f"(paras {', '.join(r['para_no'] for r in excel_rows)})")

    cells, tables_meta, page_w = parse_pdf(s.target_pdf)
    print(f"[ingest] Docling: {len(cells)} cells across "
          f"{len(tables_meta)} tables, {len(page_w)} pages")

    paras = segment_paras(cells, tables_meta)
    paras_by_no = {p["para_no"]: p for p in paras}
    print(f"[ingest] segmented paras from PDF: "
          f"{', '.join(sorted(paras_by_no, key=lambda x: int(x) if x.isdigit() else 0))}")

    records, warnings = build_records(paras, excel_rows, tables_meta, s.target_pdf)
    reconcile_msgs = reconcile(records, excel_rows)

    report_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "ingest_diff_report.txt")
    write_diff_report(report_path, records, paras_by_no, cells, warnings, reconcile_msgs)
    print(f"[ingest] diff report -> {report_path}")
    for m in reconcile_msgs:
        print("  " + m)
    for w in warnings:
        print("  " + w)

    # always dump structured records to JSON (audit trail + Postgres fallback)
    json_path = os.path.join(os.path.dirname(report_path), "structured_paras.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"[ingest] structured records -> {json_path}")

    if args.report_only:
        print("[ingest] --report-only: skipping DB + embedding.")
        return 0

    ok, msg = store_postgres(records)
    print("  " + msg)
    if not ok:
        print("  -> records preserved in structured_paras.json; "
              "re-run after Postgres/Docker is up to persist them (idempotent).")

    all_chunks = []
    for r in records:
        all_chunks.extend(build_chunks(r, s))
    print(f"[ingest] built {len(all_chunks)} chunks")
    store_chunks_bookkeeping(all_chunks)

    msg = embed_and_upsert(all_chunks, recreate=args.recreate)
    print("  " + msg)
    print("[ingest] DONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
