"""Postgres access: structured `paras` table + a `chunks` bookkeeping table.

The `paras` row is the atomic structured record the router's exact-lookup path
(future phase) will return directly. Ingestion is idempotent: the composite
para_key is the primary key, so re-running UPSERTs instead of duplicating.

An immutable audit-log table is deliberately NOT added in Phase 1 (per phase
scope), but nothing here makes it hard to add later.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import get_settings

metadata = MetaData()

# One structured record per audit para. para_key is the composite unique key:
#   (project, period, para_type, para_no) rendered as a single slug.
paras = Table(
    "paras",
    metadata,
    Column("para_key", String, primary_key=True),
    Column("project", String, nullable=False),
    Column("period", String, nullable=False),
    Column("para_type", String, nullable=False),   # IIA / IIB
    Column("para_no", String, nullable=False),
    Column("title", Text, nullable=False),          # para description / heading
    Column("observation_text", Text, nullable=False),
    Column("reply_text", Text, nullable=False),
    Column("tables_markdown", JSON, nullable=False, default=list),  # list[str]
    Column("page_start", Integer, nullable=True),
    Column("page_end", Integer, nullable=True),
    Column("source_pdf", String, nullable=False),
    Column("column_confidence", String, nullable=True),  # high / low
    # --- Phase 2 additions (auditable ingestion) ---
    Column("raw_observation_blocks", JSON, nullable=True),   # list[str]
    Column("raw_reply_blocks", JSON, nullable=True),         # list[str]
    Column("observation_reconstructed", Text, nullable=True),
    Column("reply_reconstructed", Text, nullable=True),
    Column("reconstruction_model", String, nullable=True),
    Column("tables_json", JSON, nullable=True),              # list[{...}]
    Column("quality_score", Float, nullable=True),
    Column("needs_review", Boolean, nullable=True),
    Column("review_reasons", JSON, nullable=True),           # list[str]
    Column("blocks_obs", Integer, nullable=True),
    Column("blocks_reply", Integer, nullable=True),
    Column("matched_excel", Boolean, nullable=True),
)

# Columns added after Phase 1 — ALTERed in idempotently for existing DBs.
_PHASE2_COLUMNS = {
    "raw_observation_blocks": "JSONB",
    "raw_reply_blocks": "JSONB",
    "observation_reconstructed": "TEXT",
    "reply_reconstructed": "TEXT",
    "reconstruction_model": "VARCHAR",
    "tables_json": "JSONB",
    "quality_score": "DOUBLE PRECISION",
    "needs_review": "BOOLEAN",
    "review_reasons": "JSONB",
    "blocks_obs": "INTEGER",
    "blocks_reply": "INTEGER",
    "matched_excel": "BOOLEAN",
}


def ensure_phase2_columns(engine=None) -> None:
    """Idempotently add Phase 2 columns to an existing paras table."""
    engine = engine or get_engine()
    with engine.begin() as conn:
        for col, sqltype in _PHASE2_COLUMNS.items():
            conn.execute(text(
                f'ALTER TABLE paras ADD COLUMN IF NOT EXISTS "{col}" {sqltype}'
            ))

# Chunk bookkeeping (source of truth for what got embedded). Qdrant holds the
# vectors; this lets us reconcile / re-embed without re-parsing the PDF.
chunks = Table(
    "chunks",
    metadata,
    Column("chunk_id", String, primary_key=True),
    Column("para_key", String, nullable=False),
    Column("section", String, nullable=False),   # observation / reply / table
    Column("ordinal", Integer, nullable=False),
    Column("page_start", Integer, nullable=True),
    Column("page_end", Integer, nullable=True),
    Column("text", Text, nullable=False),
)

# Phase 4: conversation session state.
sessions = Table(
    "sessions",
    metadata,
    Column("session_id", String, primary_key=True),
    Column("active_para_key", String, nullable=True),
    Column("active_filters", JSON, nullable=True),      # dict
    Column("last_route", String, nullable=True),
    Column("last_intent", String, nullable=True),
    Column("history", JSON, nullable=True),             # list, last 10 turns
    # ordered, distinct para_keys the last answer/disambiguation presented, so a
    # follow-up like "show me the second one" can select from that exact list.
    Column("last_para_keys", JSON, nullable=True),      # list[str]
    Column("created_at", DateTime, nullable=True),
    Column("updated_at", DateTime, nullable=True),
)

# Columns added to sessions after its first release — ALTERed in idempotently.
_SESSION_COLUMNS = {"last_para_keys": "JSONB"}


def ensure_session_columns(engine=None) -> None:
    """Idempotently add later-added columns to an existing sessions table."""
    engine = engine or get_engine()
    with engine.begin() as conn:
        for col, sqltype in _SESSION_COLUMNS.items():
            conn.execute(text(
                f'ALTER TABLE sessions ADD COLUMN IF NOT EXISTS "{col}" {sqltype}'
            ))


def get_engine():
    s = get_settings()
    return create_engine(s.sqlalchemy_url, future=True, pool_pre_ping=True)


def init_db(engine=None) -> None:
    engine = engine or get_engine()
    metadata.create_all(engine)


@contextmanager
def connection(engine=None):
    engine = engine or get_engine()
    with engine.begin() as conn:
        yield conn


def upsert_para(conn, record: dict) -> None:
    """Idempotent insert-or-update keyed on para_key."""
    payload = dict(record)
    payload["tables_markdown"] = payload.get("tables_markdown") or []
    stmt = pg_insert(paras).values(**payload)
    update_cols = {c.name: stmt.excluded[c.name] for c in paras.columns
                   if c.name != "para_key"}
    stmt = stmt.on_conflict_do_update(index_elements=["para_key"], set_=update_cols)
    conn.execute(stmt)


def replace_chunks(conn, para_key: str, chunk_records: list[dict]) -> None:
    """Delete + reinsert this para's chunks so re-ingest never duplicates."""
    conn.execute(chunks.delete().where(chunks.c.para_key == para_key))
    if chunk_records:
        conn.execute(chunks.insert(), chunk_records)


def get_para(conn, para_key: str) -> dict | None:
    row = conn.execute(paras.select().where(paras.c.para_key == para_key)).mappings().first()
    return dict(row) if row else None
