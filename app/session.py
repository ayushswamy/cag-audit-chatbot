"""Conversation session state in Postgres. One row per session tracks the
active para, active metadata filters, the last route/intent, and a rolling
window of the last 10 turns.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db

HISTORY_LIMIT = 10

_columns_ensured = False


def _ensure_columns_once() -> None:
    """Add newer sessions columns to an existing DB once per process (idempotent).
    Lets the ordinal-selection feature deploy without a manual migration."""
    global _columns_ensured
    if _columns_ensured:
        return
    try:
        db.ensure_session_columns()
    except Exception:  # noqa: BLE001 — never block a turn on a DDL hiccup
        pass
    _columns_ensured = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_session_id() -> str:
    return uuid.uuid4().hex


def load(session_id: str) -> dict | None:
    _ensure_columns_once()
    engine = db.get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            db.sessions.select().where(db.sessions.c.session_id == session_id)
        ).mappings().first()
    return dict(row) if row else None


def get_or_create(session_id: str | None) -> dict:
    _ensure_columns_once()
    if session_id:
        existing = load(session_id)
        if existing:
            return existing
    sid = session_id or new_session_id()
    state = {
        "session_id": sid, "active_para_key": None, "active_filters": {},
        "last_route": None, "last_intent": None, "history": [],
        "last_para_keys": [],
        "created_at": _now(), "updated_at": _now(),
    }
    engine = db.get_engine()
    with engine.begin() as conn:
        stmt = pg_insert(db.sessions).values(**state)
        stmt = stmt.on_conflict_do_nothing(index_elements=["session_id"])
        conn.execute(stmt)
    return state


def save(state: dict) -> None:
    payload = {
        "session_id": state["session_id"],
        "active_para_key": state.get("active_para_key"),
        "active_filters": state.get("active_filters") or {},
        "last_route": state.get("last_route"),
        "last_intent": state.get("last_intent"),
        "history": (state.get("history") or [])[-HISTORY_LIMIT:],
        "last_para_keys": state.get("last_para_keys") or [],
        "created_at": state.get("created_at") or _now(),
        "updated_at": _now(),
    }
    engine = db.get_engine()
    with engine.begin() as conn:
        stmt = pg_insert(db.sessions).values(**payload)
        update_cols = {k: stmt.excluded[k] for k in payload if k != "session_id"}
        stmt = stmt.on_conflict_do_update(index_elements=["session_id"], set_=update_cols)
        conn.execute(stmt)


def append_turn(state: dict, user: str, assistant: str, intent: str,
                para_key: str | None) -> None:
    hist = state.get("history") or []
    hist.append({"user": user, "assistant": assistant[:500],
                 "intent": intent, "para_key": para_key})
    state["history"] = hist[-HISTORY_LIMIT:]


def reset_context(state: dict) -> None:
    """Automatic topic switch — clear the active para and filters."""
    state["active_para_key"] = None
    state["active_filters"] = {}


def state_summary(state: dict, para_title: str | None = None) -> str:
    """Compact 2-line summary fed to the turn analyzer and answer prompt."""
    ap = state.get("active_para_key")
    ap_line = f"{ap}" + (f" — {para_title}" if para_title else "") if ap else "none"
    filt = state.get("active_filters") or {}
    recent = [t["user"] for t in (state.get("history") or [])[-2:]]
    return (
        f"active_para: {ap_line}\n"
        f"active_filters: {filt} | last_intent: {state.get('last_intent')} | "
        f"recent_user_turns: {recent}"
    )
