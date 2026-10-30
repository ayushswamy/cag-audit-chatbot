"""Multi-turn conversation orchestrator.

Per turn: load session -> ONE analysis call (intent + topic-change + rewrite) ->
reset context if the topic changed -> dispatch by intent -> generate answer ->
persist updated session state.
"""
from __future__ import annotations

from app import aggregate, answer_modes, citations, db, exact, llm, session, suggestions
from app.routed_retrieval import retrieve as routed_retrieve
from app.routed_retrieval import semantic_retrieve
from app.turn import TurnDecision, analyze

CHITCHAT_REPLY = (
    "I answer questions about NTPC CAG Inspection Report audit paras. Ask me to "
    "show a para, explain a finding or reply, compare paras, or count/list paras."
)


def _key_to_filters(para_key: str) -> dict:
    parts = para_key.split("|")
    if len(parts) != 4:
        return {}
    project, period, para_type, para_no = parts
    return {"project": project, "period": period,
            "para_type": para_type, "para_no": para_no}


def _para_title(para_key: str | None) -> str | None:
    if not para_key:
        return None
    with db.connection(db.get_engine()) as conn:
        row = db.get_para(conn, para_key)
    return row["title"] if row else None


def _top_para_key(chunks: list[dict]) -> str | None:
    return chunks[0]["para_key"] if chunks else None


def ordered_para_keys(chunks: list[dict]) -> list[str]:
    """Distinct para_keys in presentation order (the order the user sees)."""
    seen, out = set(), []
    for c in chunks or []:
        pk = c.get("para_key")
        if pk and pk not in seen:
            seen.add(pk)
            out.append(pk)
    return out


def remembered_para_keys(result: dict) -> list[str]:
    """The ordered paras the user was just shown, so a later 'the second one'
    can select from that exact list. Empty when there is nothing to remember."""
    kind = result.get("kind")
    if kind == "answer":
        return ordered_para_keys(result.get("chunks", []))
    if kind == "disambiguation":
        return [c["para_key"] for c in (result.get("candidates") or [])
                if c.get("para_key")]
    if kind == "aggregation":
        rows = (result.get("aggregation") or {}).get("rows") or []
        return [r["para_key"] for r in rows
                if isinstance(r, dict) and r.get("para_key")]
    return []


def _presents_new_list(decision: TurnDecision, result: dict) -> bool:
    """A fresh set the user can pick from (so we refresh the remembered list).
    Drilling into one item (SELECT / FOLLOW_UP / SUMMARY) must NOT overwrite it."""
    return (decision.intent in ("NEW_SEARCH", "COMPARE", "AGGREGATION")
            or result.get("kind") == "disambiguation")


def _dispatch(decision: TurnDecision, state: dict) -> dict:
    """Return an internal result dict; the answer LLM is invoked later."""
    intent = decision.intent
    active = state.get("active_para_key")

    if intent == "SELECT" and decision.selected_para_key:
        # user picked an item from the previously shown list ("the second one")
        with db.connection(db.get_engine()) as conn:
            row = db.get_para(conn, decision.selected_para_key)
        if not row:
            return {"kind": "answer", "route": "SELECT", "chunks": [],
                    "answer": llm.NOT_FOUND,
                    "active_para_key": decision.selected_para_key}
        return {"kind": "answer", "route": "SELECT",
                "chunks": exact.para_to_chunks(row),
                "active_para_key": decision.selected_para_key,
                "needs_answer": True, "mode_hint": answer_modes.FULL_PARA}

    if intent == "AGGREGATION":
        res = aggregate.run(decision.standalone_query, decision.filters)
        return {"kind": "aggregation", "route": "AGGREGATION",
                "aggregation": res, "answer": aggregate.format_answer(res),
                "chunks": [], "active_para_key": active}

    if intent == "CHITCHAT":
        return {"kind": "chitchat", "route": "CHITCHAT", "answer": CHITCHAT_REPLY,
                "chunks": [], "active_para_key": active}

    if intent == "NOT_INDEXED":
        # a report reference we don't have -> instant canned reply, no LLM
        return {"kind": "notice", "route": "NOT_INDEXED",
                "answer": decision.notice or llm.NOT_FOUND,
                "chunks": [], "active_para_key": active}

    if intent == "COMPARE":
        compared, union = [], []
        for ent in (decision.compare_entities or [decision.standalone_query]):
            chunks = semantic_retrieve(ent, {})
            compared.append({"entity": ent, "para_key": _top_para_key(chunks)})
            union.extend(chunks)
        # de-dup by chunk_id, keep best rerank order
        seen, merged = set(), []
        for c in sorted(union, key=lambda x: x["rerank_score"], reverse=True):
            if c["chunk_id"] not in seen:
                seen.add(c["chunk_id"]); merged.append(c)
        return {"kind": "answer", "route": "COMPARE", "chunks": merged[:8],
                "compared": compared, "active_para_key": active,
                "needs_answer": True}

    if intent in ("SUMMARY", "OPEN_PDF") and active:
        with db.connection(db.get_engine()) as conn:
            row = db.get_para(conn, active)
        if not row:
            return {"kind": "answer", "route": intent, "chunks": [],
                    "answer": llm.NOT_FOUND, "active_para_key": active}
        if intent == "OPEN_PDF":
            lo, hi = row.get("page_start"), row.get("page_end")
            pages = f"p.{lo}" if lo == hi else f"p.{lo}-{hi}"
            ans = (f"'{row['title']}' — {row['para_type']} para {row['para_no']} of "
                   f"IR {row['period']}. Source: {row['source_pdf']}, {pages}.")
            return {"kind": "open_pdf", "route": "OPEN_PDF", "answer": ans,
                    "chunks": [], "active_para_key": active,
                    "source_pdf": row["source_pdf"], "pages": pages}
        # SUMMARY
        return {"kind": "answer", "route": "SUMMARY",
                "chunks": exact.para_to_chunks(row), "active_para_key": active,
                "needs_answer": True, "mode_hint": answer_modes.SUMMARY}

    if intent == "FOLLOW_UP" and active:
        # scope retrieval to the active para
        filters = _key_to_filters(active)
        filters.pop("project", None)
        chunks = semantic_retrieve(decision.standalone_query, filters)
        if not chunks:  # fallback: whole para
            with db.connection(db.get_engine()) as conn:
                row = db.get_para(conn, active)
            chunks = exact.para_to_chunks(row) if row else []
        return {"kind": "answer", "route": "FOLLOW_UP", "chunks": chunks,
                "active_para_key": active, "needs_answer": True}

    # NEW_SEARCH (default) -> Phase-3 routed retriever
    rr = routed_retrieve(decision.standalone_query)
    if rr.kind == "disambiguation":
        return {"kind": "disambiguation", "route": rr.route,
                "candidates": rr.candidates, "chunks": [],
                "active_para_key": None}
    if rr.kind == "aggregation":
        return {"kind": "aggregation", "route": rr.route,
                "aggregation": rr.aggregation,
                "answer": rr.aggregation_text, "chunks": [],
                "active_para_key": state.get("active_para_key")}
    return {"kind": "answer", "route": rr.route, "chunks": rr.chunks,
            "active_para_key": _top_para_key(rr.chunks), "needs_answer": True,
            # a resolved exact lookup -> show the whole para verbatim
            "mode_hint": answer_modes.FULL_PARA if rr.route == "EXACT_LOOKUP" else None}


def respond(session_id: str | None, message: str,
            generate_answer: bool = True) -> dict:
    state = session.get_or_create(session_id)
    active_title = _para_title(state.get("active_para_key"))
    summary = session.state_summary(state, active_title)

    decision = analyze(message, summary, has_active=bool(state.get("active_para_key")),
                       last_para_keys=state.get("last_para_keys"))

    if decision.topic_changed:
        session.reset_context(state)

    result = _dispatch(decision, state)

    # generate the grounded answer (structured mode + citation enforcement)
    answer_text = result.get("answer", "")
    mode = None
    enforcement = None
    if result.get("needs_answer") and generate_answer:
        chunks = result["chunks"]
        active = result.get("active_para_key")
        mode = result.get("mode_hint") or answer_modes.select_mode(
            decision.intent, decision.standalone_query, chunks, active)
        # table mode answers from the para's table JSON -> surface the table
        # chunk as evidence so citations resolve and the answer is auditable.
        if mode == answer_modes.TABLE and active:
            with db.connection(db.get_engine()) as conn:
                row = db.get_para(conn, active)
            if row:
                have = {c["chunk_id"] for c in chunks}
                for tc in exact.para_to_chunks(row):
                    if tc["section"] == "table" and tc["chunk_id"] not in have:
                        chunks = chunks + [tc]
            result["chunks"] = chunks
        valid_keys = {c["para_key"] for c in chunks}
        if active:
            valid_keys.add(active)

        def _gen(strict: bool = False) -> str:
            return answer_modes.generate(mode, decision.standalone_query, chunks,
                                         summary, active, strict=strict)

        answer_text = _gen(strict=False)
        regen = None if mode == answer_modes.FULL_PARA else (lambda: _gen(True))
        enforcement = citations.enforce(answer_text, valid_keys, regenerate_fn=regen)
        answer_text = enforcement.answer
    elif result.get("needs_answer") and not generate_answer:
        answer_text = ""  # eval mode: skip the slow answer LLM

    # update state
    new_active = result.get("active_para_key")
    if new_active is not None:
        state["active_para_key"] = new_active
    if decision.filters:
        merged = {**(state.get("active_filters") or {}), **decision.filters}
        state["active_filters"] = merged
    state["last_intent"] = decision.intent
    state["last_route"] = result.get("route")
    # remember the freshly presented list so a later "the second one" resolves;
    # drilling into one item does not overwrite it.
    if _presents_new_list(decision, result):
        remembered = remembered_para_keys(result)
        if remembered:
            state["last_para_keys"] = remembered
    session.append_turn(state, message, answer_text or "", decision.intent,
                        state.get("active_para_key"))
    session.save(state)

    # deterministic suggestion chips (no LLM)
    has_tables = any(c.get("section") == "table" for c in result.get("chunks", []))
    chips = suggestions.suggest(
        kind=result["kind"], mode=mode, active_para_key=state.get("active_para_key"),
        candidates=result.get("candidates"), has_tables=has_tables)

    enf = None
    if enforcement is not None:
        enf = {"unsupported_paragraphs": enforcement.unsupported_count,
               "regenerated": enforcement.regenerated,
               "low_confidence": enforcement.low_confidence}

    return {
        "session_id": state["session_id"],
        "intent": decision.intent,
        "topic_changed": decision.topic_changed,
        "standalone_query": decision.standalone_query,
        "route": result.get("route"),
        "kind": result["kind"],
        "answer_mode": mode,
        "answer": answer_text,
        "chunks": result.get("chunks", []),
        "candidates": result.get("candidates", []),
        "aggregation": result.get("aggregation"),
        "compared": result.get("compared", []),
        "active_para_key": state.get("active_para_key"),
        "suggestions": chips,
        "enforcement": enf,
        "turn_source": decision.source,
    }
