"""FastAPI app — Phase 6A REST contract for the frontend (React / MS Teams).

Public surface is under /api/v1 (see app/api/*). Everything returns the
consistent error envelope {"error": {code, message, request_id}} except the SSE
stream. The legacy root POST /ask (non-streaming) is retained for internal
tooling/tests.
"""
from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app import conversation
from app.api import deps
from app.api.health import router as health_router
from app.api.paras import router as paras_router
from app.api.sessions import router as sessions_router
from app.config import get_settings
from app.schemas import (
    AskRequest,
    AskResponse,
    ChunkUsed,
    Citation,
    CitationEnforcement,
    ComparedEntity,
    DisambiguationCandidate,
)

_s = get_settings()

app = FastAPI(
    title=_s.api_title,
    version=_s.api_version,
    description=(
        "Conversational retrieval over NTPC CAG Inspection Report audit paras.\n\n"
        "**Auth:** every `/api/v1` call requires header `X-API-Key` (placeholder "
        "until Phase 7 IdP).\n\n"
        "**Errors:** all non-SSE responses use `{\"error\": {code, message, "
        "request_id}}`.\n\n"
        "**Streaming:** `POST /api/v1/sessions/{id}/ask` is a `text/event-stream` "
        "(SSE) with events: intent, retrieval, token*, suggestions, done "
        "(or disambiguation / error)."
    ),
)

# request-id first (inner), CORS last (outer, handles preflight before auth)
app.add_middleware(BaseHTTPMiddleware, dispatch=deps.request_id_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_s.cors_origin_list,          # explicit origins, never "*"
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)

app.add_exception_handler(RequestValidationError, deps.validation_exception_handler)
app.add_exception_handler(StarletteHTTPException, deps.http_exception_handler)
app.add_exception_handler(Exception, deps.unhandled_exception_handler)

# --- /api/v1 surface ---
_v1 = _s.api_v1_prefix
app.include_router(health_router, prefix=_v1)
app.include_router(sessions_router, prefix=_v1)
app.include_router(paras_router, prefix=_v1)


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": _s.api_title, "version": _s.api_version,
            "docs": "/docs", "api": _v1}


# =========================================================================== #
# Legacy internal endpoints (non-streaming) — kept for tests/tooling.
# =========================================================================== #
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SECTIONS = {"observation", "reply", "table"}


def _extract_citations(answer_text: str) -> list[Citation]:
    seen, cites = set(), []
    for m in _BRACKET_RE.finditer(answer_text):
        parts = [p.strip() for p in m.group(1).split(" | ")]
        if len(parts) != 3:
            continue
        para_key, section, page = parts
        if section.lower() not in _SECTIONS or (para_key, section, page) in seen:
            continue
        seen.add((para_key, section, page))
        cites.append(Citation(para_key=para_key, section=section, page=page))
    return cites


@app.get("/health", include_in_schema=False)
def health_legacy() -> dict:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse, include_in_schema=False)
def ask_legacy(req: AskRequest) -> AskResponse:
    r = conversation.respond(req.session_id, req.question)
    citations = _extract_citations(r["answer"]) if r["kind"] == "answer" else []
    enf = CitationEnforcement(**r["enforcement"]) if r.get("enforcement") else None
    return AskResponse(
        session_id=r["session_id"], route=r.get("route"), kind=r["kind"],
        answer=r["answer"], intent=r["intent"], topic_changed=r["topic_changed"],
        standalone_query=r["standalone_query"],
        active_para_key=r.get("active_para_key"),
        answer_mode=r.get("answer_mode"), suggestions=r.get("suggestions", []),
        enforcement=enf, citations=citations,
        chunks_used=[ChunkUsed(chunk_id=c["chunk_id"], para_key=c["para_key"],
                               section=c["section"], page=c["page"],
                               rerank_score=c.get("rerank_score", 0.0), text=c["text"])
                     for c in r.get("chunks", [])],
        candidates=[DisambiguationCandidate(**c) for c in r.get("candidates", [])],
        aggregation=r.get("aggregation"),
        compared=[ComparedEntity(**c) for c in r.get("compared", [])])
