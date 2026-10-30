"""Request/response models for the API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    session_id: str | None = None   # omit to start a new conversation


class Citation(BaseModel):
    para_key: str
    section: str          # observation / reply / table
    page: str             # e.g. "p.3" or "p.3-4"


class ChunkUsed(BaseModel):
    chunk_id: str
    para_key: str
    section: str
    page: str
    rerank_score: float
    text: str


class RouteInfo(BaseModel):
    route: str            # EXACT_LOOKUP | SEMANTIC | AGGREGATION | AMBIGUOUS
    filters: dict
    source: str           # llm | fallback
    standalone_query: str


class DisambiguationCandidate(BaseModel):
    para_key: str
    project: str
    period: str
    para_type: str
    para_no: str
    title: str
    page: str


class ComparedEntity(BaseModel):
    entity: str
    para_key: str | None = None


class CitationEnforcement(BaseModel):
    unsupported_paragraphs: int
    regenerated: bool
    low_confidence: bool


class AskResponse(BaseModel):
    session_id: str
    route: str | None = None
    kind: str                       # answer | disambiguation | aggregation | chitchat | open_pdf
    answer: str
    # conversation state (Phase 4)
    intent: str
    topic_changed: bool
    standalone_query: str
    active_para_key: str | None = None
    # answer quality (Phase 5)
    answer_mode: str | None = None  # qa | summary | full_para | table
    suggestions: list[str] = []
    enforcement: CitationEnforcement | None = None
    citations: list[Citation] = []
    chunks_used: list[ChunkUsed] = []
    candidates: list[DisambiguationCandidate] = []
    aggregation: dict | None = None
    compared: list[ComparedEntity] = []
