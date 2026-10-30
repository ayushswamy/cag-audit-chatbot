"""Persistence: upsert ParaRecords into Postgres (idempotent) and chunk+embed
into Qdrant. Reuses the Phase-1 app modules so runtime and ingestion agree.
"""
from __future__ import annotations

import uuid

from app import db, qdrant_store
from app.config import get_settings
from app.models_embed import embed_texts
from ingest.models import ParaRecord

NAMESPACE = uuid.UUID("6f9b7c2e-1a2b-4c3d-8e9f-0a1b2c3d4e5f")


def init_storage() -> None:
    engine = db.get_engine()
    db.init_db(engine)
    db.ensure_phase2_columns(engine)


def _best_text(rec: ParaRecord, which: str) -> str:
    """Prefer reconstructed text if present, else geometric."""
    if which == "obs":
        return rec.observation_reconstructed or rec.observation_text
    return rec.reply_reconstructed or rec.reply_text


def upsert_paras(records: list[ParaRecord]) -> int:
    engine = db.get_engine()
    with db.connection(engine) as conn:
        for r in records:
            db.upsert_para(conn, {
                "para_key": r.para_key, "project": r.project, "period": r.period,
                "para_type": r.para_type, "para_no": r.para_no, "title": r.title,
                "observation_text": r.observation_text, "reply_text": r.reply_text,
                "tables_markdown": [t["markdown"] for t in r.tables],
                "page_start": r.page_start, "page_end": r.page_end,
                "source_pdf": r.source_pdf, "column_confidence": r.column_confidence,
                "raw_observation_blocks": r.raw_observation_blocks,
                "raw_reply_blocks": r.raw_reply_blocks,
                "observation_reconstructed": r.observation_reconstructed,
                "reply_reconstructed": r.reply_reconstructed,
                "reconstruction_model": r.reconstruction_model,
                "tables_json": r.tables,
                "quality_score": r.quality_score, "needs_review": r.needs_review,
                "review_reasons": r.review_reasons,
                "blocks_obs": r.blocks_obs, "blocks_reply": r.blocks_reply,
                "matched_excel": r.matched_excel,
            })
    return len(records)


def _chunk_words(text: str, size_tokens: int, overlap_tokens: int):
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


def build_chunks(rec: ParaRecord, s) -> list[dict]:
    chunks, ordinal = [], 0
    page = f"p.{rec.page_start}" if rec.page_start == rec.page_end else f"p.{rec.page_start}-{rec.page_end}"

    def add(section, text):
        nonlocal ordinal
        chunks.append({
            "chunk_id": f"{rec.para_key}::{section}::{ordinal}",
            "para_key": rec.para_key, "section": section, "page": page, "text": text,
            # filterable metadata (used by the SEMANTIC router path)
            "period": rec.period, "para_type": rec.para_type,
            "para_no": rec.para_no, "project": rec.project,
        })
        ordinal += 1

    for seg, section in ((_best_text(rec, "obs"), "observation"),
                        (_best_text(rec, "reply"), "reply")):
        for piece in _chunk_words(seg, s.chunk_tokens, s.chunk_overlap):
            add(section, piece)
    for t in rec.tables:
        md = t.get("markdown") or ""
        if md and len(md) <= s.table_chunk_max_chars:
            add("table", md)
    return chunks


def embed_and_upsert(records: list[ParaRecord], recreate: bool = False) -> int:
    s = get_settings()
    qdrant_store.ensure_collection(recreate=recreate)
    all_chunks = []
    for r in records:
        all_chunks.extend(build_chunks(r, s))
    if not all_chunks:
        return 0
    vectors = embed_texts([c["text"] for c in all_chunks])
    points = []
    for c, v in zip(all_chunks, vectors):
        points.append({
            "id": str(uuid.uuid5(NAMESPACE, c["chunk_id"])),
            "vector": v,
            "payload": {"chunk_id": c["chunk_id"], "para_key": c["para_key"],
                        "section": c["section"], "page": c["page"], "text": c["text"],
                        "period": c["period"], "para_type": c["para_type"],
                        "para_no": c["para_no"], "project": c["project"]},
        })
    qdrant_store.upsert_chunks(points)
    return len(points)
