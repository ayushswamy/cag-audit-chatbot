"""AGGREGATION path: counts and listings over para metadata via a WHITELIST of
parameterized SQL templates with named slots. The LLM never emits SQL — it only
supplies filter values, which fill fixed, safe templates.
"""
from __future__ import annotations

import re

from sqlalchemy import text

from app import db

# Whitelisted templates. Each is a fixed SQL string with named bind params only.
TEMPLATES = {
    # counts
    "count_all": "select count(*) as n from paras",
    "count_by_period": "select count(*) as n from paras where period = :period",
    "count_by_type": "select count(*) as n from paras where para_type = :para_type",
    "count_by_period_type":
        "select count(*) as n from paras where period = :period "
        "and para_type = :para_type",
    # listings
    "list_by_period":
        "select para_key, para_type, para_no, title from paras "
        "where period = :period order by para_type, para_no",
    "list_by_period_type":
        "select para_key, para_type, para_no, title from paras "
        "where period = :period and para_type = :para_type "
        "order by para_no",
    "list_by_type":
        "select para_key, para_type, para_no, title from paras "
        "where para_type = :para_type order by period, para_no",
    # breakdowns
    "count_by_period_grouped":
        "select period, para_type, count(*) as n from paras "
        "group by period, para_type order by period, para_type",
}

_COUNT_RE = re.compile(r"\b(how many|number of|count|how much|total)\b", re.I)
_LIST_RE = re.compile(r"\b(list|show all|which paras|what paras|all paras)\b", re.I)


def select_template(question: str, filters: dict) -> tuple[str, dict]:
    """Deterministically choose a whitelisted template + params from the router
    filters and simple keyword cues. No free-form SQL."""
    period = filters.get("period")
    para_type = filters.get("para_type")
    is_count = bool(_COUNT_RE.search(question))
    is_list = bool(_LIST_RE.search(question))

    # Browse intent: an explicit "list", OR a bare metadata query (period/part)
    # with NO "how many"/"count" cue -> show the paras, not just a number.
    if is_list or (not is_count and (period or para_type)):
        if period and para_type:
            return "list_by_period_type", {"period": period, "para_type": para_type}
        if period:
            return "list_by_period", {"period": period}
        if para_type:
            return "list_by_type", {"para_type": para_type}
        # listing without a period/part is broad -> grouped breakdown
        return "count_by_period_grouped", {}

    # default: counting
    if period and para_type:
        return "count_by_period_type", {"period": period, "para_type": para_type}
    if period:
        return "count_by_period", {"period": period}
    if para_type:
        return "count_by_type", {"para_type": para_type}
    if _COUNT_RE.search(question):
        return "count_all", {}
    return "count_by_period_grouped", {}


def run(question: str, filters: dict) -> dict:
    name, params = select_template(question, filters)
    sql = TEMPLATES[name]
    engine = db.get_engine()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text(sql), params).mappings().all()]
    return {"template": name, "params": params, "rows": rows,
            "sql": sql}


def format_answer(result: dict) -> str:
    """Deterministic natural-language rendering of an aggregation result."""
    name, params, rows = result["template"], result["params"], result["rows"]
    if name.startswith("count") and rows and "n" in rows[0] and len(rows) == 1:
        n = rows[0]["n"]
        scope = []
        if params.get("period"):
            scope.append(f"in report {params['period']}")
        if params.get("para_type"):
            scope.append(f"of type {params['para_type']}")
        return f"There are {n} paras " + (" ".join(scope) if scope else "in total") + "."
    if name == "count_by_period_grouped":
        parts = [f"{r['period']}/{r['para_type']}: {r['n']}" for r in rows]
        return "Para counts by report and type — " + "; ".join(parts) + "."
    if name.startswith("list"):
        if not rows:
            return "No paras match that scope."
        items = [f"{r['para_type']} {r['para_no']} — {r['title']}" for r in rows]
        return f"{len(rows)} paras:\n- " + "\n- ".join(items)
    return "No result."
