"""API contract models (Phase 6A). These shapes are a CONTRACT — breaking
changes require a new version prefix (/api/v2)."""
from __future__ import annotations

from pydantic import BaseModel, Field


# --- errors ---
class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


# --- sessions ---
class SessionCreateResponse(BaseModel):
    session_id: str
    created_at: str | None = None


class AskBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000,
                         description="The user's turn.")


class HistoryTurn(BaseModel):
    user: str
    assistant: str
    intent: str | None = None
    para_key: str | None = None


class HistoryResponse(BaseModel):
    session_id: str
    active_para_key: str | None = None
    active_filters: dict = {}
    last_intent: str | None = None
    turns: list[HistoryTurn] = []


class ResetResponse(BaseModel):
    session_id: str
    reset: bool = True
    active_para_key: str | None = None


# --- paras ---
class ParaListItem(BaseModel):
    para_key: str
    project: str
    period: str
    para_type: str
    para_no: str
    title: str
    page_start: int | None = None
    page_end: int | None = None
    source_pdf: str
    needs_review: bool | None = None


class ParaListResponse(BaseModel):
    items: list[ParaListItem]
    page: int
    page_size: int
    total: int
    total_pages: int


class ParaTable(BaseModel):
    markdown: str | None = None
    rows: list | None = None
    page: int | None = None
    section: str | None = None


class ParaPages(BaseModel):
    start: int | None = None
    end: int | None = None


class ParaDetail(BaseModel):
    para_key: str
    project: str
    period: str
    para_type: str
    para_no: str
    title: str
    observation: str
    reply: str
    tables: list[ParaTable] = []
    pages: ParaPages
    source_pdf: str
    column_confidence: str | None = None
    needs_review: bool | None = None


# --- health ---
class DependencyHealth(BaseModel):
    name: str
    status: str            # ok | down
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str            # ok | degraded
    version: str
    dependencies: list[DependencyHealth]
