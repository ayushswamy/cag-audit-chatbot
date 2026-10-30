"""Health endpoint: checks Postgres, Qdrant, and the LLM endpoint, returning
per-dependency status. NOT behind auth (liveness/readiness probes need it)."""
from __future__ import annotations

import time

import httpx
from fastapi import APIRouter
from sqlalchemy import text

from app import db
from app.api.schemas import DependencyHealth, HealthResponse
from app.config import get_settings
from app.qdrant_store import get_client

router = APIRouter(tags=["health"])


def _timed(fn) -> DependencyHealth:
    t0 = time.time()
    try:
        detail = fn()
        return DependencyHealth(name="", status="ok",
                                latency_ms=round((time.time() - t0) * 1000, 1),
                                detail=detail)
    except Exception as e:  # noqa: BLE001
        return DependencyHealth(name="", status="down",
                                latency_ms=round((time.time() - t0) * 1000, 1),
                                detail=f"{type(e).__name__}: {str(e)[:120]}")


def _check_postgres() -> str:
    with db.get_engine().connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM paras")).scalar_one()
    return f"{n} paras"


def _check_qdrant() -> str:
    s = get_settings()
    info = get_client().get_collection(s.qdrant_collection)
    return f"collection '{s.qdrant_collection}', {info.points_count} points"


def _check_llm() -> str:
    s = get_settings()
    base = s.openai_base_url.rstrip("/")
    if s.llm_api == "ollama":
        url = (base[:-3].rstrip("/") if base.endswith("/v1") else base) + "/api/tags"
    else:
        url = base + "/models"
    r = httpx.get(url, timeout=5)
    r.raise_for_status()
    return f"{s.model_name} reachable"


@router.get("/health", response_model=HealthResponse,
            summary="Service + dependency health")
def health() -> HealthResponse:
    s = get_settings()
    checks = {"postgres": _check_postgres, "qdrant": _check_qdrant, "llm": _check_llm}
    deps = []
    for name, fn in checks.items():
        d = _timed(fn)
        d.name = name
        deps.append(d)
    overall = "ok" if all(d.status == "ok" for d in deps) else "degraded"
    return HealthResponse(status=overall, version=s.api_version, dependencies=deps)
