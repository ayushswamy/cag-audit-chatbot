"""Shared data model for a structured para record (Phase 2)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParaRecord:
    para_key: str                 # project|period|para_type|para_no
    project: str
    period: str
    para_type: str                # IIA / IIB
    para_no: str                  # may be alphanumeric, e.g. "4A"
    title: str                    # Excel description if matched, else heading residual

    # geometric column-split text
    observation_text: str = ""
    reply_text: str = ""

    # raw source blocks (auditable — what the geometry produced, pre-repair join)
    raw_observation_blocks: list[str] = field(default_factory=list)
    raw_reply_blocks: list[str] = field(default_factory=list)

    # LLM-reconstructed text (auditable, separate from geometric)
    observation_reconstructed: str | None = None
    reply_reconstructed: str | None = None
    reconstruction_model: str | None = None

    # tables linked to this para
    tables: list[dict] = field(default_factory=list)   # {section, markdown, rows_json, page}

    page_start: int | None = None
    page_end: int | None = None
    source_pdf: str = ""

    # quality / validation
    blocks_obs: int = 0
    blocks_reply: int = 0
    heading_strength: int = 0
    matched_excel: bool = False
    quality_score: float = 0.0
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    column_confidence: str = "high"
