"""Query router: a single structured-output LLM call (temperature 0) that
classifies each question into one of four routes and extracts metadata filters.

    EXACT_LOOKUP  -> user names a specific para (number/title) they want back
    SEMANTIC      -> thematic / content question answered from retrieved text
    AGGREGATION   -> counting / listing over metadata (how many, list, …)
    AMBIGUOUS     -> a para reference too vague to resolve (bare number, and the
                     number exists in several reports) -> triggers disambiguation

Output JSON: {route, filters:{period?, para_type?, para_no?, project?},
              standalone_query}

The LLM decision is backstopped by deterministic regex extraction of period and
para number, so the small (8B) router stays reliable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import llm
from app.metadata import get_vocabulary

ROUTES = {"EXACT_LOOKUP", "SEMANTIC", "AGGREGATION", "AMBIGUOUS"}

# e.g. 2019-20, 2020-23, 2005-11
_PERIOD_RE = re.compile(r"\b(20\d{2})\s*[-–/]\s*(\d{2})\b")
# e.g. "para 3", "para no. 2", "para IIB 5", "para 4A"
_PARA_RE = re.compile(
    r"\bpara\s*(?:no\.?|number)?\s*(?:(II[AB])\s*)?[-:.]?\s*(\d+[A-Ca-c]?)\b", re.I)
_PARATYPE_RE = re.compile(r"\b(II[AB])\b", re.I)
# "IIB 7", "IIA 4A" — a part+number reference WITHOUT the word "para".
_TYPE_NUM_RE = re.compile(r"\b(II[AB])\s*[-:.]?\s*(\d+[A-Ca-c]?)\b", re.I)
# Aggregation = COUNTING/LISTING paras over metadata. Deliberately excludes
# "how much"/"total" — those ask for a monetary figure (a factual/table answer),
# not a para count.
_AGG_RE = re.compile(r"\b(how many|number of paras|count of|list|show all|"
                     r"which paras|how many paras)\b", re.I)
# Words carrying NO topic meaning — used to decide whether a query is "metadata
# only" (just a period/part, no real question) and should be an instant listing.
_META_STOP = re.compile(
    r"\b(ir|inspection|report|reports|para|paras|part|show|see|browse|list|all|"
    r"me|the|a|an|of|in|on|for|from|please|what|which|are|is|was|were|give|and|"
    r"type|no|number|period|any|do|does|have|about)\b", re.I)


def _is_metadata_only(question: str, filters: dict) -> bool:
    """True when the message is JUST metadata (a report period and/or part) with
    no topic words and no para number — a BROWSE request, not a semantic
    question. e.g. '2017-18', 'IIA 2020-23', 'paras in 2019-20'. Such queries
    should list that report's paras via fast SQL, never a multi-minute LLM
    answer. 'coal 2017-18' keeps a topic word -> NOT metadata-only."""
    if filters.get("para_no"):
        return False
    if not (filters.get("period") or filters.get("para_type")):
        return False
    residual = _PERIOD_RE.sub(" ", question)
    residual = _PARATYPE_RE.sub(" ", residual)
    residual = _META_STOP.sub(" ", residual)
    residual = re.sub(r"[^A-Za-z0-9]+", " ", residual).strip()
    return residual == ""


def _is_bare_para_ref(question: str, filters: dict) -> bool:
    """True when the message is JUST a specific para reference (para number +
    report period) with no question around it — e.g. 'IIB 7 of 2017-18',
    'para 2 2019-20', 'show iib 9 of 2023-24'. A bare reference should return
    that whole para (exact lookup), not be treated as a scoped follow-up.
    'who approved IIB 7 of 2017-18?' keeps question words -> NOT bare."""
    if not (filters.get("para_no") and filters.get("period")):
        return False
    residual = _PERIOD_RE.sub(" ", question)
    residual = _PARA_RE.sub(" ", residual)
    residual = _TYPE_NUM_RE.sub(" ", residual)
    residual = _PARATYPE_RE.sub(" ", residual)
    residual = _META_STOP.sub(" ", residual)
    residual = re.sub(r"[^A-Za-z0-9]+", " ", residual).strip()
    return residual == ""


@dataclass
class RouteDecision:
    route: str
    filters: dict = field(default_factory=dict)
    standalone_query: str = ""
    raw: dict = field(default_factory=dict)
    source: str = "llm"  # llm | fallback


def _system_prompt() -> str:
    vocab = get_vocabulary()
    periods = ", ".join(vocab["periods"]) or "(none)"
    ptypes = ", ".join(vocab["para_types"]) or "IIA, IIB"
    return (
        "You are the query router for an audit-para retrieval system over CAG "
        "Inspection Reports on NTPC. Classify the user's question and extract "
        "metadata filters. Respond with ONE JSON object only.\n\n"
        "Routes:\n"
        "- EXACT_LOOKUP: the user asks for a specific para identified by its "
        "number (and possibly report period and/or part), e.g. 'show para 3 of "
        "IR 2019-20', 'para IIB 2 of 2019-20'.\n"
        "- SEMANTIC: a thematic or content question, e.g. 'paras about coal "
        "block surrender', 'what did audit say about the Tapovan HRT package'.\n"
        "- AGGREGATION: counting or listing over metadata, e.g. 'how many IIA "
        "paras in 2020-23', 'list the paras in IR 2019-20'.\n"
        "- AMBIGUOUS: a para reference by bare number with NO report period, "
        "e.g. 'para 1' (the same number exists in many reports).\n\n"
        f"Valid report periods: {periods}\n"
        f"Valid para types: {ptypes}\n"
        "A report named 'IR 2019-20' or 'inspection report 2019-20' means "
        "period '2019-20'.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "route": "EXACT_LOOKUP|SEMANTIC|AGGREGATION|AMBIGUOUS",\n'
        '  "filters": {"period": string|null, "para_type": "IIA"|"IIB"|null, '
        '"para_no": string|null, "project": string|null},\n'
        '  "standalone_query": string  // self-contained restatement of the question\n'
        "}\n"
        "Only use a period from the valid list; if none is mentioned, use null. "
        "Do not invent filters."
    )


def _regex_extract(question: str) -> dict:
    out = {}
    m = _PERIOD_RE.search(question)
    if m:
        out["period"] = f"{m.group(1)}-{m.group(2)}"
    pm = _PARA_RE.search(question)
    if pm:
        if pm.group(1):
            out["para_type"] = pm.group(1).upper()
        out["para_no"] = pm.group(2).upper()
    if "para_no" not in out:
        # catch "IIB 7" / "IIA 4A" style refs that omit the word "para"
        tn = _TYPE_NUM_RE.search(question)
        if tn:
            out.setdefault("para_type", tn.group(1).upper())
            out["para_no"] = tn.group(2).upper()
    if "para_type" not in out:
        tm = _PARATYPE_RE.search(question)
        if tm:
            out["para_type"] = tm.group(1).upper()
    return out


def _clean_filters(filters: dict, vocab: dict) -> dict:
    out = {}
    period = filters.get("period")
    if period and str(period) in vocab["periods"]:
        out["period"] = str(period)
    pt = filters.get("para_type")
    if pt and str(pt).upper() in {"IIA", "IIB"}:
        out["para_type"] = str(pt).upper()
    pn = filters.get("para_no")
    if pn not in (None, "", "null"):
        out["para_no"] = str(pn).upper().strip()
    proj = filters.get("project")
    if proj and str(proj) in vocab["projects"]:
        out["project"] = str(proj)
    return out


def route(question: str) -> RouteDecision:
    vocab = get_vocabulary()
    regex = _regex_extract(question)

    raw = {}
    try:
        raw = llm.complete_json(_system_prompt(), f"Question: {question}")
    except Exception:
        raw = {}

    llm_route = str(raw.get("route", "")).upper().strip()
    filters = _clean_filters(raw.get("filters") or {}, vocab)
    standalone = (raw.get("standalone_query") or "").strip() or question

    source = "llm"
    if llm_route not in ROUTES:
        # LLM failed — deterministic fallback from regex signals.
        source = "fallback"
        if regex.get("para_no"):
            llm_route = "EXACT_LOOKUP"
        elif _AGG_RE.search(question):
            llm_route = "AGGREGATION"
        else:
            llm_route = "SEMANTIC"

    # Backfill filters from regex (regex is high-precision for period/para_no).
    for k, v in regex.items():
        filters.setdefault(k, v)

    # --- deterministic guardrails on the route ---
    has_num = bool(filters.get("para_no"))
    has_period = bool(filters.get("period"))

    if has_num and has_period:
        # fully specified reference -> exact
        if llm_route == "AMBIGUOUS":
            llm_route = "EXACT_LOOKUP"
    if has_num and not has_period and llm_route == "EXACT_LOOKUP":
        # bare number reference -> let the exact handler disambiguate; but if the
        # number is clearly a lone "para N" query, mark AMBIGUOUS up front.
        llm_route = "AMBIGUOUS"
    if _AGG_RE.search(question) and llm_route == "SEMANTIC" and not has_num:
        # strong aggregation cue overrides a weak semantic guess
        llm_route = "AGGREGATION"
    if llm_route == "AGGREGATION" and not _AGG_RE.search(question):
        # aggregation ALWAYS has a counting/listing cue ('how many', 'list', …).
        # Without one, a bare "show me the X para" is a lookup, not aggregation.
        llm_route = "EXACT_LOOKUP" if has_num else "SEMANTIC"
    if llm_route == "EXACT_LOOKUP" and not has_num:
        # exact needs a number; without one it's really semantic
        llm_route = "SEMANTIC"
    if has_num and not has_period and llm_route != "AGGREGATION":
        # a bare para number with no report period is inherently ambiguous
        # (that number exists in several reports) -> force disambiguation,
        # regardless of what the LLM guessed. Never silently answer one.
        llm_route = "AMBIGUOUS"
    if _is_metadata_only(question, filters):
        # a bare period/part browse -> fast listing, never a slow semantic answer
        llm_route = "AGGREGATION"

    return RouteDecision(route=llm_route, filters=filters,
                         standalone_query=standalone, raw=raw, source=source)
