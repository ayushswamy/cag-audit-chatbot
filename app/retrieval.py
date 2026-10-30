"""Semantic retrieval path: embed question -> hybrid search (top 20) ->
rerank with BGE-reranker-v2-m3 -> top 5.

Phase 1 wires ONLY the semantic path. The exact-lookup and aggregation router
paths described in CLAUDE.md are later phases and are intentionally not built.
"""
from __future__ import annotations

from app.config import get_settings
from app.models_embed import embed_query, rerank
from app.qdrant_store import hybrid_search


def retrieve(question: str) -> list[dict]:
    """Return up to rerank_top_k chunk dicts, most relevant first.

    Each dict: {chunk_id, para_key, section, page, text, rerank_score}.
    """
    s = get_settings()
    qvec = embed_query(question)
    hits = hybrid_search(qvec, top_k=s.retrieve_top_k)
    if not hits:
        return []

    passages = [h.payload.get("text", "") for h in hits]
    scores = rerank(question, passages)

    scored = []
    for h, sc in zip(hits, scores):
        p = h.payload
        scored.append(
            {
                "chunk_id": str(h.id),
                "para_key": p.get("para_key", ""),
                "section": p.get("section", ""),
                "page": p.get("page", ""),
                "text": p.get("text", ""),
                "rerank_score": float(sc),
            }
        )
    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    top = scored[: s.rerank_top_k]

    # Relevance gate: keep the top chunk plus any others within keep_ratio of the
    # top score. Prevents weak, off-topic chunks from misleading the answer model.
    if top:
        cutoff = top[0]["rerank_score"] * s.rerank_keep_ratio
        top = [top[0]] + [c for c in top[1:] if c["rerank_score"] >= cutoff]
    return top
