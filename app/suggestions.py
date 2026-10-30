"""Suggestion chips — rule-based on conversation state. Deterministic and
cheap: NO LLM call. Chips change sensibly with context (para open vs
disambiguation vs aggregation vs cold start).
"""
from __future__ import annotations

COLD_START = [
    "List the paras in IR 2019-20",
    "How many IIA paras in 2020-23?",
    "Show the Tapovan HRT para",
    "Show the Noida land parcels para",
]


def suggest(*, kind: str, mode: str | None, active_para_key: str | None,
            candidates: list[dict] | None = None,
            has_tables: bool = False) -> list[str]:
    candidates = candidates or []

    if kind == "disambiguation":
        chips = [f"{c['period']} {c['para_type']} para {c['para_no']}"
                 for c in candidates[:4]]
        return chips or COLD_START

    if kind == "aggregation":
        return ["List those paras", "Break it down by report and type",
                "Open a specific para"]

    if kind in ("answer", "open_pdf") and active_para_key:
        chips: list[str] = []
        if mode != "summary":
            chips.append("Summarize this para")
        if mode != "full_para":
            chips.append("Show the full para (both sides)")
        chips += ["Show management reply", "Show audit observations"]
        if has_tables:
            chips.append("Show the tables")
        chips += ["Any similar paras?", "Download source PDF"]
        # de-dup, keep order, cap at 6
        seen, out = set(), []
        for c in chips:
            if c not in seen:
                seen.add(c); out.append(c)
        return out[:6]

    return COLD_START
