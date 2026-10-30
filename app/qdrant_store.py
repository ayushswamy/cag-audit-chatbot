"""Qdrant collection with a named dense vector + a named sparse vector, and
hybrid (dense + sparse) search fused with RRF via the Query API.
"""
from __future__ import annotations

from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import get_settings
from app.models_embed import DenseSparse

DENSE = "dense"
SPARSE = "sparse"
DENSE_DIM = 1024  # BGE-M3 dense dimension


@lru_cache
def get_client() -> QdrantClient:
    s = get_settings()
    # Embedded local mode (host dev without Docker) if a path is configured.
    if s.qdrant_path:
        return QdrantClient(path=s.qdrant_path)
    return QdrantClient(url=s.qdrant_url, prefer_grpc=False, timeout=60)


def ensure_collection(recreate: bool = False) -> None:
    s = get_settings()
    client = get_client()
    exists = client.collection_exists(s.qdrant_collection)
    if exists and recreate:
        client.delete_collection(s.qdrant_collection)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=s.qdrant_collection,
            vectors_config={DENSE: qm.VectorParams(size=DENSE_DIM, distance=qm.Distance.COSINE)},
            sparse_vectors_config={SPARSE: qm.SparseVectorParams()},
        )


def upsert_chunks(points: list[dict]) -> None:
    """points: [{id, vector: DenseSparse, payload: {...}}]."""
    s = get_settings()
    client = get_client()
    structs = []
    for p in points:
        v: DenseSparse = p["vector"]
        structs.append(
            qm.PointStruct(
                id=p["id"],
                vector={
                    DENSE: v.dense,
                    SPARSE: qm.SparseVector(indices=v.sparse_indices, values=v.sparse_values),
                },
                payload=p["payload"],
            )
        )
    client.upsert(collection_name=s.qdrant_collection, points=structs, wait=True)


def hybrid_search(query_vec: DenseSparse, top_k: int,
                  query_filter: qm.Filter | None = None) -> list[qm.ScoredPoint]:
    """Dense + sparse prefetch fused with Reciprocal Rank Fusion."""
    s = get_settings()
    client = get_client()
    res = client.query_points(
        collection_name=s.qdrant_collection,
        prefetch=[
            qm.Prefetch(query=query_vec.dense, using=DENSE, limit=top_k,
                        filter=query_filter),
            qm.Prefetch(
                query=qm.SparseVector(indices=query_vec.sparse_indices,
                                      values=query_vec.sparse_values),
                using=SPARSE, limit=top_k, filter=query_filter,
            ),
        ],
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        limit=top_k,
        with_payload=True,
    )
    return res.points
