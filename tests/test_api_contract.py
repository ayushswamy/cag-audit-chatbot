"""Contract tests (Phase 6A). Assert the response SHAPE of every endpoint so a
future change that breaks the frontend contract fails CI.

Requires the Postgres + Qdrant stack up (docker compose up postgres qdrant) and
the corpus ingested. The SSE-shape tests monkeypatch the LLM-dependent bits so
they run fast and deterministically (no model needed).

Run:  pytest tests/test_api_contract.py -q
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.turn import TurnDecision

client = TestClient(app)
H = {"X-API-Key": "dev-key-123"}
TAPOVAN = "Corporate Office|2019-20|IIB|2"


def _sse_events(resp):
    events, cur = [], None
    for line in resp.iter_lines():
        line = line if isinstance(line, str) else line.decode()
        if line.startswith("event:"):
            cur = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            events.append((cur, json.loads(line.split(":", 1)[1].strip())))
    return events


# --------------------------------------------------------------------------- #
# OpenAPI + health + auth
# --------------------------------------------------------------------------- #
def test_openapi_has_all_endpoints():
    paths = client.get("/openapi.json").json()["paths"]
    for p in ["/api/v1/health", "/api/v1/sessions",
              "/api/v1/sessions/{session_id}/ask",
              "/api/v1/sessions/{session_id}/history",
              "/api/v1/sessions/{session_id}/reset",
              "/api/v1/paras", "/api/v1/paras/{para_key}",
              "/api/v1/paras/{para_key}/pdf"]:
        assert p in paths, p


def test_health_shape():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"status", "version", "dependencies"}
    names = {d["name"] for d in b["dependencies"]}
    assert names == {"postgres", "qdrant", "llm"}
    for d in b["dependencies"]:
        assert set(d) >= {"name", "status", "latency_ms"}


def test_auth_required_and_envelope():
    r = client.post("/api/v1/sessions")           # no key
    assert r.status_code == 401
    err = r.json()["error"]
    assert set(err) == {"code", "message", "request_id"}
    assert err["code"] == "missing_api_key"
    assert r.headers.get("X-Request-ID")


def test_invalid_key():
    r = client.post("/api/v1/sessions", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #
def test_create_session_shape():
    r = client.post("/api/v1/sessions", headers=H)
    assert r.status_code == 201
    b = r.json()
    assert "session_id" in b and isinstance(b["session_id"], str)


def test_history_and_reset_shape():
    sid = client.post("/api/v1/sessions", headers=H).json()["session_id"]
    h = client.get(f"/api/v1/sessions/{sid}/history", headers=H)
    assert h.status_code == 200
    hb = h.json()
    assert set(hb) >= {"session_id", "active_para_key", "active_filters", "turns"}
    assert isinstance(hb["turns"], list)
    rr = client.post(f"/api/v1/sessions/{sid}/reset", headers=H)
    assert rr.status_code == 200 and rr.json()["reset"] is True


def test_history_unknown_session_404():
    r = client.get("/api/v1/sessions/does-not-exist/history", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


# --------------------------------------------------------------------------- #
# paras
# --------------------------------------------------------------------------- #
def test_paras_list_shape():
    r = client.get("/api/v1/paras", params={"period": "2019-20", "page_size": 3},
                   headers=H)
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"items", "page", "page_size", "total", "total_pages"}
    if b["items"]:
        it = b["items"][0]
        assert set(it) >= {"para_key", "period", "para_type", "para_no", "title",
                           "source_pdf"}


def test_paras_list_validation_422():
    r = client.get("/api/v1/paras", params={"para_type": "ZZ"}, headers=H)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_para_detail_shape():
    r = client.get(f"/api/v1/paras/{TAPOVAN}", headers=H)
    assert r.status_code == 200
    b = r.json()
    assert set(b) >= {"para_key", "observation", "reply", "tables", "pages",
                      "source_pdf"}
    assert b["para_key"] == TAPOVAN
    assert set(b["pages"]) == {"start", "end"}


def test_para_not_found_404():
    r = client.get("/api/v1/paras/Corporate Office|1999-99|IIB|9", headers=H)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "para_not_found"


def test_para_pdf_stream():
    r = client.get(f"/api/v1/paras/{TAPOVAN}/pdf", headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_para_pdf_page_slice():
    r = client.get(f"/api/v1/paras/{TAPOVAN}/pdf", params={"pages": "true"}, headers=H)
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"


# --------------------------------------------------------------------------- #
# SSE shape (LLM-independent via monkeypatch)
# --------------------------------------------------------------------------- #
def _decision(intent="NEW_SEARCH"):
    return TurnDecision(intent=intent, topic_changed=False,
                        standalone_query="q", filters={}, source="llm")


def test_ask_sse_answer_flow(monkeypatch):
    from app.api import streaming
    from app import answer_modes, conversation

    monkeypatch.setattr(streaming, "analyze", lambda *a, **k: _decision())
    monkeypatch.setattr(conversation, "_dispatch", lambda d, s: {
        "kind": "answer", "route": "SEMANTIC", "needs_answer": True,
        "active_para_key": TAPOVAN, "mode_hint": None,
        "chunks": [{"chunk_id": "c0", "para_key": TAPOVAN, "section": "observation",
                    "page": "p.3-7", "text": "HRT ...", "rerank_score": 1.0}]})
    monkeypatch.setattr(answer_modes, "generate",
                        lambda *a, **k: f"Rs. 634.04 crore [{TAPOVAN} | observation | p.3-7].")

    sid = client.post("/api/v1/sessions", headers=H).json()["session_id"]
    with client.stream("POST", f"/api/v1/sessions/{sid}/ask", headers=H,
                       json={"message": "tell me about it"}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(resp)
    names = [e for e, _ in events]
    assert names[0] == "intent"
    assert "retrieval" in names and "token" in names
    assert names[-2] == "suggestions" and names[-1] == "done"
    done = events[-1][1]
    assert set(done) >= {"answer_id", "latency_ms"}
    # retrieval citations shape
    retr = next(d for e, d in events if e == "retrieval")
    assert "citations" in retr and "route" in retr
    if retr["citations"]:
        assert set(retr["citations"][0]) == {"para_key", "section", "pages"}


def test_ask_sse_disambiguation(monkeypatch):
    from app.api import streaming
    from app import conversation

    monkeypatch.setattr(streaming, "analyze", lambda *a, **k: _decision())
    monkeypatch.setattr(conversation, "_dispatch", lambda d, s: {
        "kind": "disambiguation", "route": "AMBIGUOUS", "chunks": [],
        "active_para_key": None,
        "candidates": [
            {"para_key": "Corporate Office|2019-20|IIB|1", "project": "Corporate Office",
             "period": "2019-20", "para_type": "IIB", "para_no": "1",
             "title": "t", "page": "p.1"},
            {"para_key": "Corporate Office|2020-23|IIA|1", "project": "Corporate Office",
             "period": "2020-23", "para_type": "IIA", "para_no": "1",
             "title": "t", "page": "p.1"}]})

    sid = client.post("/api/v1/sessions", headers=H).json()["session_id"]
    with client.stream("POST", f"/api/v1/sessions/{sid}/ask", headers=H,
                       json={"message": "para 1"}) as resp:
        events = _sse_events(resp)
    names = [e for e, _ in events]
    assert names[0] == "intent"
    assert "disambiguation" in names
    assert "token" not in names
    disamb = next(d for e, d in events if e == "disambiguation")
    assert len(disamb["candidates"]) == 2


def test_ask_body_validation_422():
    sid = client.post("/api/v1/sessions", headers=H).json()["session_id"]
    r = client.post(f"/api/v1/sessions/{sid}/ask", headers=H, json={"message": ""})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
