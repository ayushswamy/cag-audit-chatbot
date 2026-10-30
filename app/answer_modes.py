"""Structured answer modes (Phase 5).

  qa         - default grounded Q&A (the Phase-1..4 behaviour)
  summary    - Observation gist / Management-reply gist / Current status
  full_para  - both sides reproduced verbatim-faithful (deterministic, no LLM)
  table      - numeric questions answered from the stored table JSON, not prose

All LLM-backed modes share the strict grounding + citation rules; full_para is
deterministic so it is verbatim by construction.
"""
from __future__ import annotations

import json
import re

from app import db, llm

QA = "qa"
SUMMARY = "summary"
FULL_PARA = "full_para"
TABLE = "table"

# Table mode is for reading TABULAR data specifically, not every figure (a
# headline amount usually lives in prose and should stay in `qa`).
_TABLE_CUE = re.compile(
    r"\b(credit note|debit note|per (?:plant|unit|year|station)|"
    r"(?:plant|unit|year|station)[- ]wise|breakdown|in the table|table shows|"
    r"each (?:plant|unit|year)|row|column)\b", re.I)
_FULLPARA_CUE = re.compile(
    r"\b(full para|entire para|whole para|both sides|verbatim|complete text|"
    r"full text|read the para|show (?:me )?the (?:full |complete )?para)\b", re.I)
_SUMMARY_CUE = re.compile(r"\b(summar|gist|overview|tl;dr|in short|brief)\b", re.I)


def _tag(para_key: str, section: str, page: str) -> str:
    return f"[{para_key} | {section} | {page}]"


def _para_row(para_key: str) -> dict | None:
    with db.connection(db.get_engine()) as conn:
        return db.get_para(conn, para_key)


def select_mode(intent: str, question: str, chunks: list[dict],
                active_para_key: str | None) -> str:
    """Deterministic mode selection."""
    if intent == "SUMMARY" or _SUMMARY_CUE.search(question):
        return SUMMARY
    if _FULLPARA_CUE.search(question):
        return FULL_PARA
    if _TABLE_CUE.search(question) and _has_tables(chunks, active_para_key):
        return TABLE
    return QA


def _has_tables(chunks: list[dict], active_para_key: str | None) -> bool:
    if any(c.get("section") == "table" for c in chunks):
        return True
    if active_para_key:
        row = _para_row(active_para_key)
        return bool(row and row.get("tables_json"))
    return False


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
def generate(mode: str, question: str, chunks: list[dict],
             state_summary: str | None, active_para_key: str | None,
             strict: bool = False) -> str:
    if mode == FULL_PARA:
        return _full_para(active_para_key, chunks)
    if mode == SUMMARY:
        return _summary(question, chunks, state_summary, strict)
    if mode == TABLE:
        return _table(question, chunks, active_para_key, state_summary, strict)
    return llm.answer(question, chunks, state_summary=state_summary,
                      strict=strict)


_STRICT_SUFFIX = (
    "\n\nSTRICT MODE: your previous answer had statements without a valid "
    "citation. Re-answer so that EVERY sentence ends with the exact CITE AS tag "
    "of a provided chunk. If a claim is not in the chunks, remove it."
)


def _summary(question, chunks, state_summary, strict) -> str:
    sys = (
        "You summarize a single CAG audit para for an auditor. Output EXACTLY "
        "three labelled lines, each ending with the exact CITE AS tag (in square "
        "brackets) of the supporting chunk:\n"
        "Observation: <one-sentence gist of the audit observation> [tag]\n"
        "Reply: <one-sentence gist of NTPC's management reply> [tag]\n"
        "Status: <current status / resolution if stated, else 'Not stated in "
        "the para'> [tag]\n"
        "Use only the chunks. Preserve monetary values verbatim."
        + (_STRICT_SUFFIX if strict else "")
    )
    user = f"{llm.build_context(chunks)}\n\nPara to summarize: {question}"
    return llm._complete_answer(sys, user, state_summary)


def _table(question, chunks, active_para_key, state_summary, strict) -> str:
    row = _para_row(active_para_key) if active_para_key else None
    tables = (row or {}).get("tables_json") or []
    page = _page_label(row) if row else "p.?"
    tag = _tag(active_para_key or "?", "table", page)
    tables_text = json.dumps(tables, ensure_ascii=False, indent=1)[:6000]
    sys = (
        "You answer NUMERIC questions using ONLY the table data (JSON rows) "
        "provided — never from prose or outside knowledge. Quote figures "
        f"verbatim (Rs./crore/lakh). End your answer with the tag {tag}. If the "
        "table does not contain the answer, say 'not found in the indexed "
        "reports'." + (_STRICT_SUFFIX if strict else "")
    )
    user = f"Table JSON:\n{tables_text}\n\nQuestion: {question}"
    return llm._complete_answer(sys, user, state_summary)


def _page_label(row: dict) -> str:
    lo, hi = row.get("page_start"), row.get("page_end")
    return f"p.{lo}" if lo == hi else f"p.{lo}-{hi}"


def _full_para(active_para_key: str | None, chunks: list[dict]) -> str:
    """Deterministic verbatim reproduction of both sides + tables, with a
    citation tag appended to every paragraph (0 uncited by construction)."""
    row = _para_row(active_para_key) if active_para_key else None
    if not row:
        return llm.NOT_FOUND
    page = _page_label(row)
    obs = (row.get("observation_reconstructed") or row.get("observation_text") or "").strip()
    reply = (row.get("reply_reconstructed") or row.get("reply_text") or "").strip()
    parts = [f"**Audit Observation** — {row['para_type']} para {row['para_no']}, "
             f"IR {row['period']}"]
    parts.append(_cite_paragraphs(obs, _tag(row["para_key"], "observation", page)))
    parts.append("**Management Reply**")
    parts.append(_cite_paragraphs(reply, _tag(row["para_key"], "reply", page)))
    for i, md in enumerate(row.get("tables_markdown") or []):
        if md:
            parts.append(f"**Table {i + 1}** {_tag(row['para_key'], 'table', page)}\n{md}")
    return "\n\n".join(p for p in parts if p.strip())


def _cite_paragraphs(text: str, tag: str) -> str:
    out = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if para:
            out.append(f"{para} {tag}")
    return "\n\n".join(out) if out else f"(none) {tag}"
