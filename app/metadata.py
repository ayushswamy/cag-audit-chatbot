"""Metadata vocabulary derived from Postgres — the set of valid periods,
para_types and projects. Used to ground the router (so it maps "IR 2019-20" to
period "2019-20") and to validate router-produced filters.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import text

from app import db


@lru_cache(maxsize=1)
def get_vocabulary() -> dict:
    engine = db.get_engine()
    with engine.connect() as conn:
        periods = [r[0] for r in conn.execute(
            text("select distinct period from paras order by period"))]
        para_types = [r[0] for r in conn.execute(
            text("select distinct para_type from paras order by para_type"))]
        projects = [r[0] for r in conn.execute(
            text("select distinct project from paras order by project"))]
    return {"periods": periods, "para_types": para_types, "projects": projects}


def clear_cache() -> None:
    get_vocabulary.cache_clear()
