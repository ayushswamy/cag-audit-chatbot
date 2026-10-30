"""EXACT_LOOKUP path: resolve a specific para from Postgres by its composite
identity. If a bare para number matches several reports, return ALL candidates
for disambiguation — never guess one.
"""
from __future__ import annotations

from sqlalchemy import text

from app import db


def _rows(filters: dict) -> list[dict]:
    where, params = ["para_no = :para_no"], {"para_no": filters.get("para_no")}
    if filters.get("period"):
        where.append("period = :period"); params["period"] = filters["period"]
    if filters.get("para_type"):
        where.append("para_type = :para_type"); params["para_type"] = filters["para_type"]
    if filters.get("project"):
        where.append("project = :project"); params["project"] = filters["project"]
    sql = (
        "select para_key, project, period, para_type, para_no, title, "
        "page_start, page_end, observation_text, reply_text, "
        "observation_reconstructed, reply_reconstructed, tables_markdown "
        "from paras where " + " and ".join(where) +
        " order by period, para_type, para_no"
    )
    engine = db.get_engine()
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _candidate(row: dict) -> dict:
    lo, hi = row.get("page_start"), row.get("page_end")
    page = f"p.{lo}" if lo == hi else f"p.{lo}-{hi}"
    return {"para_key": row["para_key"], "project": row["project"],
            "period": row["period"], "para_type": row["para_type"],
            "para_no": row["para_no"], "title": row["title"], "page": page}


def lookup(filters: dict) -> dict:
    """Return one of:
      {"kind": "exact", "para": <row>, "candidates": [1]}
      {"kind": "disambiguation", "candidates": [<many>]}
      {"kind": "not_found", "candidates": []}
    """
    if not filters.get("para_no"):
        return {"kind": "not_found", "candidates": []}
    rows = _rows(filters)
    if not rows:
        return {"kind": "not_found", "candidates": []}
    if len(rows) == 1:
        return {"kind": "exact", "para": rows[0],
                "candidates": [_candidate(rows[0])]}
    return {"kind": "disambiguation",
            "candidates": [_candidate(r) for r in rows]}


def para_to_chunks(row: dict) -> list[dict]:
    """Turn a stored para into answer-ready chunks (obs, reply, tables)."""
    lo, hi = row.get("page_start"), row.get("page_end")
    page = f"p.{lo}" if lo == hi else f"p.{lo}-{hi}"
    obs = row.get("observation_reconstructed") or row.get("observation_text") or ""
    reply = row.get("reply_reconstructed") or row.get("reply_text") or ""
    chunks = []
    if obs.strip():
        chunks.append({"chunk_id": f"{row['para_key']}::observation::0",
                       "para_key": row["para_key"], "section": "observation",
                       "page": page, "text": obs, "rerank_score": 1.0})
    if reply.strip():
        chunks.append({"chunk_id": f"{row['para_key']}::reply::0",
                       "para_key": row["para_key"], "section": "reply",
                       "page": page, "text": reply, "rerank_score": 1.0})
    for i, md in enumerate(row.get("tables_markdown") or []):
        if md:
            chunks.append({"chunk_id": f"{row['para_key']}::table::{i}",
                           "para_key": row["para_key"], "section": "table",
                           "page": page, "text": md, "rerank_score": 1.0})
    return chunks
