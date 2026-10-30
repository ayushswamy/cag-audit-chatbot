"""SSE event generator for POST /api/v1/sessions/{id}/ask.

Event sequence:
  intent        {intent, topic_changed, standalone_query}
  retrieval     {route, citations:[{para_key, section, pages}]}
  disambiguation{candidates:[...]}        (instead of tokens, for a bare para no.)
  token         {text}                    (repeated; chunks of the FINAL,
                                           citation-enforced answer — the client
                                           never renders ungrounded partial text)
  suggestions   {chips:[...]}
  done          {answer_id, latency_ms, ...}
  error         {code, message}           (on failure, then the stream closes)

Reuses the same building blocks as the non-streaming conversation orchestrator
(app.conversation) so behaviour is identical.
"""
from __future__ import annotations

import logging
import time
import uuid

from app import answer_modes, citations, conversation, db, exact, session, suggestions
from app.turn import analyze

logger = logging.getLogger("app.api.streaming")

_TOKEN_CHARS = 72  # size of each streamed token chunk (preserves formatting)


def _chunk_text(text: str):
    for i in range(0, len(text), _TOKEN_CHARS):
        yield text[i:i + _TOKEN_CHARS]


def _citations(chunks: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in chunks:
        key = (c.get("para_key"), c.get("section"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"para_key": c.get("para_key"), "section": c.get("section"),
                    "pages": c.get("page")})
    return out


def stream_turn(session_id: str, message: str):
    """Yield (event_name, data_dict) tuples for the SSE response."""
    t0 = time.time()
    answer_id = uuid.uuid4().hex
    try:
        state = session.get_or_create(session_id)
        active_title = conversation._para_title(state.get("active_para_key"))
        summary = session.state_summary(state, active_title)

        decision = analyze(message, summary, has_active=bool(state.get("active_para_key")),
                           last_para_keys=state.get("last_para_keys"))
        yield "intent", {"intent": decision.intent,
                         "topic_changed": decision.topic_changed,
                         "standalone_query": decision.standalone_query}

        if decision.topic_changed:
            session.reset_context(state)

        result = conversation._dispatch(decision, state)
        kind = result["kind"]
        chunks = result.get("chunks", [])
        active = result.get("active_para_key")
        mode = None
        answer_text = result.get("answer", "")

        if kind == "disambiguation":
            yield "disambiguation", {
                "candidates": result["candidates"],
                "message": ("That para number appears in multiple reports — "
                            "pick which report you mean."),
            }
        else:
            if kind == "answer":
                mode = result.get("mode_hint") or answer_modes.select_mode(
                    decision.intent, decision.standalone_query, chunks, active)
                if mode == answer_modes.TABLE and active:
                    with db.connection(db.get_engine()) as conn:
                        row = db.get_para(conn, active)
                    if row:
                        have = {c["chunk_id"] for c in chunks}
                        for tc in exact.para_to_chunks(row):
                            if tc["section"] == "table" and tc["chunk_id"] not in have:
                                chunks = chunks + [tc]

            yield "retrieval", {"route": result.get("route"),
                                "citations": _citations(chunks)}

            if kind == "answer":
                valid_keys = {c["para_key"] for c in chunks}
                if active:
                    valid_keys.add(active)

                def _gen(strict: bool = False) -> str:
                    return answer_modes.generate(mode, decision.standalone_query,
                                                 chunks, summary, active, strict=strict)

                answer_text = _gen(False)
                regen = None if mode == answer_modes.FULL_PARA else (lambda: _gen(True))
                answer_text = citations.enforce(answer_text, valid_keys,
                                                regenerate_fn=regen).answer

            for piece in _chunk_text(answer_text):
                yield "token", {"text": piece}

        has_tables = any(c.get("section") == "table" for c in chunks)
        # use the para THIS turn resolved to (state isn't updated until below),
        # else a fresh para answer builds chips from a stale/None active para and
        # falls back to the irrelevant cold-start chips.
        active_now = result.get("active_para_key") or state.get("active_para_key")
        chips = suggestions.suggest(kind=kind, mode=mode,
                                    active_para_key=active_now,
                                    candidates=result.get("candidates"),
                                    has_tables=has_tables)
        yield "suggestions", {"chips": chips}

        # persist state
        new_active = result.get("active_para_key")
        if new_active is not None:
            state["active_para_key"] = new_active
        if decision.filters:
            state["active_filters"] = {**(state.get("active_filters") or {}),
                                       **decision.filters}
        state["last_intent"] = decision.intent
        state["last_route"] = result.get("route")
        # remember the freshly presented list so a later "the second one" resolves
        if conversation._presents_new_list(decision, {"kind": kind}):
            remembered = conversation.remembered_para_keys(
                {"kind": kind, "chunks": chunks,
                 "candidates": result.get("candidates"),
                 "aggregation": result.get("aggregation")})
            if remembered:
                state["last_para_keys"] = remembered
        session.append_turn(state, message, answer_text or "", decision.intent,
                            state.get("active_para_key"))
        session.save(state)

        yield "done", {"answer_id": answer_id,
                       "latency_ms": int((time.time() - t0) * 1000),
                       "kind": kind, "intent": decision.intent,
                       "route": result.get("route"),
                       "answer_mode": mode,
                       "active_para_key": state.get("active_para_key")}
    except Exception:  # noqa: BLE001
        # Surface the real cause in the server log (never silently swallow it);
        # the client still gets a generic, safe message.
        logger.exception("stream_turn failed (answer_id=%s, session=%s)",
                         answer_id, session_id)
        yield "error", {"code": "internal_error",
                        "message": "Answer generation failed. Please retry."}
