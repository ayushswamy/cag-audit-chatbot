"""Routed retriever: dispatch a question to EXACT_LOOKUP / SEMANTIC /
AGGREGATION / AMBIGUOUS and return a uniform result the API layer can render.

This replaces the Phase-1 naive single-path retrieval.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from qdrant_client import models as qm

from app import aggregate, exact
from app.config import get_settings
from app.models_embed import embed_query, rerank
from app.qdrant_store import hybrid_search
from app.router import RouteDecision, route


@dataclass
class RoutedResult:
    route: str
    kind: str                       # answer | disambiguation | aggregation
    decision: RouteDecision
    chunks: list[dict] = field(default_factory=list)          # for answer
    candidates: list[dict] = field(default_factory=list)      # disambiguation
    aggregation: dict | None = None
    aggregation_text: str | None = None


def _qdrant_filter(filters: dict) -> qm.Filter | None:
    must = []
    for key in ("period", "para_type", "para_no", "project"):
        val = filters.get(key)
        if val:
            must.append(qm.FieldCondition(key=key, match=qm.MatchValue(value=val)))
    return qm.Filter(must=must) if must else None


def semantic_retrieve(query: str, filters: dict | None = None) -> list[dict]:
    """Embed -> hybrid search (top_k, with optional payload filters) -> rerank
    -> relevance gate. Returns answer-ready chunk dicts."""
    s = get_settings()
    filters = filters or {}
    qvec = embed_query(query)
    qfilter = _qdrant_filter(filters)
    hits = hybrid_search(qvec, top_k=s.retrieve_top_k, query_filter=qfilter)
    if not hits and qfilter is not None:
        # filters too strict -> fall back to unfiltered semantic search
        hits = hybrid_search(qvec, top_k=s.retrieve_top_k, query_filter=None)
    if not hits:
        return []
    passages = [h.payload.get("text", "") for h in hits]
    if s.disable_rerank:
        # use the hybrid (RRF) score directly — no cross-encoder pass
        scores = [float(h.score) for h in hits]
    else:
        scores = rerank(query, passages)
    scored = []
    for h, sc in zip(hits, scores):
        p = h.payload
        scored.append({"chunk_id": str(h.id), "para_key": p.get("para_key", ""),
                       "section": p.get("section", ""), "page": p.get("page", ""),
                       "text": p.get("text", ""), "rerank_score": float(sc)})
    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    top = scored[: s.rerank_top_k]
    if top:
        cutoff = top[0]["rerank_score"] * s.rerank_keep_ratio
        top = [top[0]] + [c for c in top[1:] if c["rerank_score"] >= cutoff]
    return top


def retrieve(question: str) -> RoutedResult:
    decision = route(question)
    r = decision.route

    if r == "AGGREGATION":
        result = aggregate.run(question, decision.filters)
        return RoutedResult(route=r, kind="aggregation", decision=decision,
                            aggregation=result,
                            aggregation_text=aggregate.format_answer(result))

    if r in ("EXACT_LOOKUP", "AMBIGUOUS"):
        res = exact.lookup(decision.filters)
        if res["kind"] == "exact":
            return RoutedResult(route="EXACT_LOOKUP", kind="answer", decision=decision,
                                chunks=exact.para_to_chunks(res["para"]))
        if res["kind"] == "disambiguation":
            return RoutedResult(route="AMBIGUOUS", kind="disambiguation",
                                decision=decision, candidates=res["candidates"])
        # not_found: fall through to semantic as a best effort
        chunks = semantic_retrieve(decision.standalone_query, decision.filters)
        return RoutedResult(route="SEMANTIC", kind="answer", decision=decision,
                            chunks=chunks)

    # SEMANTIC (default)
    chunks = semantic_retrieve(decision.standalone_query, decision.filters)
    return RoutedResult(route="SEMANTIC", kind="answer", decision=decision,
                        chunks=chunks)
