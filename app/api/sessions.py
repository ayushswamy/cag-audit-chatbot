"""Session endpoints: create, ask (SSE stream), history, reset."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import session
from app.api.deps import require_api_key, sse_event
from app.api.schemas import (
    AskBody,
    HistoryResponse,
    HistoryTurn,
    ResetResponse,
    SessionCreateResponse,
)
from app.api.streaming import stream_turn

router = APIRouter(prefix="/sessions", tags=["sessions"],
                   dependencies=[Depends(require_api_key)])


def _load_or_404(session_id: str) -> dict:
    st = session.load(session_id)
    if st is None:
        raise HTTPException(status_code=404,
                            detail=("session_not_found",
                                    f"No session with id {session_id!r}."))
    return st


@router.post("", response_model=SessionCreateResponse, status_code=201,
             summary="Create a conversation session")
def create_session() -> SessionCreateResponse:
    state = session.get_or_create(None)
    created = state.get("created_at")
    return SessionCreateResponse(
        session_id=state["session_id"],
        created_at=created.isoformat() if hasattr(created, "isoformat") else created,
    )


@router.post("/{session_id}/ask",
             summary="Ask a question (Server-Sent Events stream)",
             response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {}},
                              "description": "SSE stream: intent, retrieval, "
                              "token*, suggestions, done (or disambiguation / error)."}})
def ask(session_id: str, body: AskBody, request: Request) -> StreamingResponse:
    _load_or_404(session_id)

    def gen():
        for event, data in stream_turn(session_id, body.message):
            yield sse_event(event, data)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"})  # disable proxy buffering


@router.get("/{session_id}/history", response_model=HistoryResponse,
            summary="Full turn history for a session")
def history(session_id: str) -> HistoryResponse:
    st = _load_or_404(session_id)
    turns = [HistoryTurn(user=t.get("user", ""), assistant=t.get("assistant", ""),
                         intent=t.get("intent"), para_key=t.get("para_key"))
             for t in (st.get("history") or [])]
    return HistoryResponse(
        session_id=st["session_id"], active_para_key=st.get("active_para_key"),
        active_filters=st.get("active_filters") or {},
        last_intent=st.get("last_intent"), turns=turns)


@router.post("/{session_id}/reset", response_model=ResetResponse,
             summary="Reset conversation context (keep the session id)")
def reset(session_id: str) -> ResetResponse:
    st = _load_or_404(session_id)
    session.reset_context(st)
    session.save(st)
    return ResetResponse(session_id=st["session_id"], reset=True,
                         active_para_key=None)
