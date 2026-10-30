"""BGE-M3 embedder (dense + sparse) and BGE-reranker-v2-m3, loaded OFFLINE
from local paths. Models are lazy singletons so importing this module is cheap
and never touches the network.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_settings


@dataclass
class DenseSparse:
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]


def _assert_local(path: str, what: str) -> None:
    if not os.path.isdir(path):
        raise FileNotFoundError(
            f"{what} weights not found at {path!r}. Weights must be present on "
            f"local disk (HF_HUB_OFFLINE=1). Download them at build time into "
            f"MODELS_DIR — see scripts/download_models.py."
        )


@lru_cache
def get_embedder():
    from FlagEmbedding import BGEM3FlagModel

    s = get_settings()
    s.apply_offline_env()
    _assert_local(s.bge_m3_path, "BGE-M3")
    use_fp16 = s.embed_device != "cpu"
    return BGEM3FlagModel(s.bge_m3_path, use_fp16=use_fp16, devices=s.embed_device)


@lru_cache
def get_reranker():
    """BGE-reranker-v2-m3 via transformers directly (fast tokenizer +
    sequence-classification head). We avoid FlagReranker here because it can
    load the SLOW XLMRoberta tokenizer, which lacks `prepare_for_model` and
    crashes at scoring time.
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    s = get_settings()
    s.apply_offline_env()
    _assert_local(s.reranker_path, "BGE-reranker-v2-m3")
    tok = AutoTokenizer.from_pretrained(s.reranker_path, use_fast=True,
                                        local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        s.reranker_path, local_files_only=True)
    model = model.to(s.embed_device).eval()
    return tok, model, torch


def _to_dense_sparse(dense_vec, lexical_weights) -> DenseSparse:
    dense = [float(x) for x in dense_vec]
    indices, values = [], []
    for tok, w in lexical_weights.items():
        indices.append(int(tok))
        values.append(float(w))
    return DenseSparse(dense=dense, sparse_indices=indices, sparse_values=values)


def embed_texts(texts: list[str]) -> list[DenseSparse]:
    """Embed a batch, returning dense + sparse (lexical) for each text."""
    model = get_embedder()
    out = model.encode(texts, return_dense=True, return_sparse=True,
                       return_colbert_vecs=False)
    dense_all = out["dense_vecs"]
    lex_all = out["lexical_weights"]
    results = []
    for i in range(len(texts)):
        results.append(_to_dense_sparse(dense_all[i], lex_all[i]))
    return results


def embed_query(text: str) -> DenseSparse:
    return embed_texts([text])[0]


def rerank(query: str, passages: list[str], max_length: int = 512) -> list[float]:
    """Return a sigmoid-normalized relevance score per passage (0..1)."""
    if not passages:
        return []
    tok, model, torch = get_reranker()
    pairs = [[query, p] for p in passages]
    scores: list[float] = []
    batch = 16
    with torch.no_grad():
        for i in range(0, len(pairs), batch):
            chunk = pairs[i:i + batch]
            inputs = tok(chunk, padding=True, truncation=True,
                         max_length=max_length, return_tensors="pt").to(model.device)
            logits = model(**inputs).logits.view(-1).float()
            probs = torch.sigmoid(logits)
            scores.extend(probs.cpu().tolist())
    return scores
