"""Unified per-turn analysis: ONE structured 8B call that merges intent
detection, topic-change detection, and query rewriting.

Output JSON:
  {intent: NEW_SEARCH|FOLLOW_UP|SUMMARY|OPEN_PDF|COMPARE|AGGREGATION|CHITCHAT,
   topic_changed: bool,
   standalone_query: str,               # fully self-contained (pronouns resolved)
   filters: {period?, para_type?, para_no?, project?},
   compare_entities: [str]}             # only for COMPARE
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app import llm
from app.metadata import get_vocabulary
from app.router import (
    _clean_filters,
    _is_bare_para_ref,
    _is_metadata_only,
    _regex_extract,
)

INTENTS = {"NEW_SEARCH", "FOLLOW_UP", "SUMMARY", "OPEN_PDF", "COMPARE",
           "AGGREGATION", "CHITCHAT", "SELECT", "NOT_INDEXED"}

# --- ordinal selection of a previously-listed para ("show the second one") ---
_ORD_WORDS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
              "sixth": 5, "seventh": 6, "eighth": 7, "ninth": 8, "tenth": 9,
              "last": -1}
_ORD_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last"
    r"|(\d+)\s*(?:st|nd|rd|th))\b", re.I)
_NUMSEL_RE = re.compile(r"\b(?:number|no\.?|option|item|result|#)\s*(\d+)\b", re.I)
_SELECT_CONTEXT = re.compile(
    r"\b(one|para|paras|result|results|item|option|record|instance|finding|"
    r"that)\b", re.I)

_FOLLOWUP_CUES = re.compile(
    r"\b(it|its|it's|that|this|they|them|those|these|the para|same|"
    r"why|who|when|how much|what was|the bg|the amount|approved|reply)\b", re.I)
# Counting/listing cues only — NOT "how much"/"total" (those ask for a figure).
_AGG_CUE = re.compile(r"\b(how many|number of paras|count of|list|show all|which paras)\b", re.I)
_COMPARE_CUE = re.compile(r"\b(compare|versus|vs\.?|difference between|both)\b", re.I)
_SUMMARY_CUE = re.compile(
    r"\b(summar\w*|tl;?dr|in short|gist|brief|overview|synopsis|recap)\b", re.I)
# A request for a specific output shape/detail. Even a "summary … in points"
# should be answered via QA (which honours the format) not the 3-line template.
_FORMAT_CUE = re.compile(
    r"\b(points?|bullets?|bulleted|list|listed|table|tabular|itemi[sz]e[d]?|"
    r"elaborate|detailed?|break ?down)\b", re.I)
_OPENPDF_CUE = re.compile(r"\b(open|pdf|source|which page|page number|document)\b", re.I)
_NEW_CUE = re.compile(r"\b(show|find|search|about|para|now show|instead)\b", re.I)
# Whole-message greetings / small-talk / meta — answered with a canned reply,
# so recognise them deterministically (no ~30s model call just to say hello).
_CHITCHAT_RE = re.compile(
    r"^[\s]*("
    r"hi+|hey+|hello+|helo|yo|hola|namaste|greetings|salaam|"
    r"good\s*(?:morning|afternoon|evening|day)|"
    r"thanks?(?:\s*(?:you|a\s*lot))?|thank\s*you|thx|ty|cheers|"
    r"bye+|goodbye|see\s*(?:you|ya)|"
    r"who\s*are\s*you|what\s*(?:can|do)\s*you\s*do|what\s*can\s*i\s*ask|"
    r"what\s*is\s*this|help|how\s*are\s*you|"
    r"good|ok(?:ay)?|k|cool|nice|great|awesome|fine"
    r")[\s!.,?]*$", re.I)


@dataclass
class TurnDecision:
    intent: str
    topic_changed: bool
    standalone_query: str
    filters: dict = field(default_factory=dict)
    compare_entities: list = field(default_factory=list)
    selected_para_key: str | None = None   # set only for intent == SELECT
    notice: str | None = None              # canned reply, set for NOT_INDEXED
    source: str = "llm"
    raw: dict = field(default_factory=dict)


def ordinal_index(message: str) -> int | None:
    """Map a short selection phrase ('the second one', 'number 3', 'the last
    para') to a 0-based index (-1 == last). Returns None when the message is not
    clearly an ordinal pick — so real questions are never hijacked."""
    # an explicit report/para reference is a genuine lookup, not an ordinal pick
    ex = _regex_extract(message)
    if ex.get("period") or ex.get("para_no"):
        return None
    words = re.findall(r"[a-z0-9']+", message.lower())
    if not words or len(words) > 8:
        return None
    nm = _NUMSEL_RE.search(message)          # "number 2", "option 3", "#2"
    if nm:
        return int(nm.group(1)) - 1
    om = _ORD_RE.search(message)
    if not om:
        return None
    # require a near-bare phrase, or a selection noun ('one'/'para'/'result'…)
    if len(words) > 3 and not _SELECT_CONTEXT.search(message):
        return None
    tok = (om.group(1) or "").lower()
    if tok in _ORD_WORDS:
        return _ORD_WORDS[tok]
    if om.group(2):                          # numeric ordinal like "2nd"
        return int(om.group(2)) - 1
    return None


def resolve_ordinal(message: str, last_para_keys) -> str | None:
    """Return the para_key the ordinal refers to, or None."""
    if not last_para_keys:
        return None
    idx = ordinal_index(message)
    if idx is None:
        return None
    if idx == -1:
        return last_para_keys[-1]
    if 0 <= idx < len(last_para_keys):
        return last_para_keys[idx]
    return None


def _system_prompt() -> str:
    vocab = get_vocabulary()
    periods = ", ".join(vocab["periods"]) or "(none)"
    return (
        "You analyze one turn of a conversation with an audit-para assistant "
        "over CAG Inspection Reports on NTPC. Using the CONVERSATION STATE and "
        "the USER MESSAGE, return ONE JSON object.\n\n"
        "intent (choose one):\n"
        "- NEW_SEARCH: the user asks about a new para/topic (may name a report/"
        "para).\n"
        "- FOLLOW_UP: a question about the CURRENTLY ACTIVE para (pronouns like "
        "'it', 'the BG', 'why was ... forfeited').\n"
        "- SUMMARY: asks to summarize the active para.\n"
        "- OPEN_PDF: asks to open the source PDF / which page.\n"
        "- COMPARE: asks to compare two or more paras/entities.\n"
        "- AGGREGATION: counting or listing over metadata (how many, list).\n"
        "- CHITCHAT: greetings / small talk / meta questions.\n\n"
        "topic_changed: true when the user switches away from the active para "
        "to a different para/topic (e.g. 'now show the Loharinagpala para'). "
        "FOLLOW_UP/SUMMARY/OPEN_PDF about the active para are topic_changed=false.\n\n"
        "standalone_query: rewrite the user message into a FULLY SELF-CONTAINED "
        "question, resolving every pronoun using the active para. Example: active "
        "para = 'Para 2, Part IIB, IR 2019-20 — undue advantage to M/s HCC (Tapovan "
        "HRT)'. User: 'Who approved it?' -> standalone_query: 'Who approved the "
        "special advances to M/s HCC in Para 2, Part IIB, IR 2019-20?'.\n"
        "For COMPARE, put each compared entity's self-contained query in "
        "compare_entities.\n\n"
        f"Valid report periods: {periods}. Para types: IIA, IIB.\n"
        "JSON schema:\n"
        '{"intent": "...", "topic_changed": true|false, "standalone_query": "...", '
        '"filters": {"period": null, "para_type": null, "para_no": null, '
        '"project": null}, "compare_entities": []}'
    )


def analyze(message: str, state_summary: str, has_active: bool,
            last_para_keys=None) -> TurnDecision:
    # Deterministic ordinal selection of a para the user was just shown
    # ("the second one") — resolved without an LLM call, so it's fast and exact.
    sel = resolve_ordinal(message, last_para_keys)
    if sel:
        return TurnDecision(intent="SELECT", topic_changed=False,
                            standalone_query=message, filters={},
                            selected_para_key=sel, source="ordinal")

    # Deterministic greetings / small talk -> canned reply, no LLM call.
    if _CHITCHAT_RE.match(message):
        return TurnDecision(intent="CHITCHAT", topic_changed=False,
                            standalone_query=message, source="chitchat")

    # Deterministic browse handling for a bare metadata query ("2017-18",
    # "IIA paras 2020-23", "2022-23") — resolved WITHOUT any LLM call, so it
    # returns in milliseconds instead of minutes.
    raw_f = _regex_extract(message)
    if _is_metadata_only(message, raw_f):
        vocab = get_vocabulary()
        clean = _clean_filters(raw_f, vocab)
        if clean.get("period") or clean.get("para_type"):
            # a bare period/part that IS an indexed report -> instant listing
            return TurnDecision(intent="AGGREGATION", topic_changed=False,
                                standalone_query=message, filters=clean,
                                source="metadata")
        # looks like a report reference but it is NOT indexed -> instant notice,
        # never a slow "search" that finds nothing.
        want = raw_f.get("period") or raw_f.get("para_type") or "that report"
        avail = ", ".join(vocab.get("periods", [])) or "(none)"
        return TurnDecision(
            intent="NOT_INDEXED", topic_changed=False, standalone_query=message,
            filters={}, source="metadata",
            notice=(f"I don't have report {want} in the indexed reports. "
                    f"Indexed reports: {avail}."))

    vocab = get_vocabulary()
    user = f"CONVERSATION STATE:\n{state_summary}\n\nUSER MESSAGE: {message}"
    raw = {}
    try:
        raw = llm.complete_json(_system_prompt(), user)
    except Exception:
        raw = {}

    intent = str(raw.get("intent", "")).upper().strip()
    topic_changed = bool(raw.get("topic_changed", False))
    standalone = (raw.get("standalone_query") or "").strip()
    filters = _clean_filters(raw.get("filters") or {}, vocab)
    entities = [e for e in (raw.get("compare_entities") or []) if isinstance(e, str)]
    source = "llm"

    if intent not in INTENTS:
        # deterministic fallback
        source = "fallback"
        if _COMPARE_CUE.search(message):
            intent = "COMPARE"
        elif _AGG_CUE.search(message):
            intent = "AGGREGATION"
        elif _SUMMARY_CUE.search(message) and has_active:
            intent = "SUMMARY"
        elif has_active and _FOLLOWUP_CUE.search(message) and not _NEW_CUE.search(message):
            intent = "FOLLOW_UP"
        else:
            intent = "NEW_SEARCH"

    # guardrails
    if intent == "FOLLOW_UP" and not has_active:
        intent = "NEW_SEARCH"
    if intent in ("SUMMARY", "OPEN_PDF") and not has_active:
        intent = "NEW_SEARCH"
    if intent == "SUMMARY" and not _SUMMARY_CUE.search(message):
        # SUMMARY is the fixed 3-line gist (Observation/Reply/Status). A message
        # that isn't actually asking to summarize ("give its observation in
        # points", "explain the reply", "list the audit points") is a content
        # follow-up -> answer it normally so the user's own formatting/detail
        # request is honoured, instead of re-emitting the rigid template.
        intent = "FOLLOW_UP" if has_active else "NEW_SEARCH"
    elif intent == "SUMMARY" and _FORMAT_CUE.search(message):
        # a summary that ALSO asks for a shape/detail ("summary in points",
        # "summarise as a table") wants a fuller, formatted answer -> QA honours
        # the format (answer rule 8); the fixed 3-line template cannot.
        intent = "FOLLOW_UP" if has_active else "NEW_SEARCH"
    if _COMPARE_CUE.search(message) and intent != "COMPARE":
        intent = "COMPARE"
    if intent == "AGGREGATION" and not _AGG_CUE.search(message):
        # aggregation needs a counting/listing cue; "how much"/amount questions
        # are factual, not aggregations. Downgrade to a follow-up/search.
        intent = "FOLLOW_UP" if (has_active and _FOLLOWUP_CUE.search(message)) \
            else "NEW_SEARCH"

    # topic_changed heuristics
    regex = _regex_extract(message)
    # An explicit, fully-specified para reference (para number + report period,
    # e.g. "IIB 7 of 2017-18") is a DIRECT lookup, not a follow-up about the
    # active para -> route to a fresh search so EXACT_LOOKUP returns that whole
    # para verbatim, instead of a scoped retrieval that can surface the wrong
    # section (e.g. reply chunks narrated as the observation).
    if _is_bare_para_ref(message, regex) and \
            intent in ("FOLLOW_UP", "SUMMARY", "OPEN_PDF"):
        intent = "NEW_SEARCH"
        topic_changed = True
    if regex.get("period") or regex.get("para_no"):
        # explicit new reference -> topic switch unless it's the active para
        if intent in ("NEW_SEARCH",):
            topic_changed = True
    if intent in ("FOLLOW_UP", "SUMMARY", "OPEN_PDF"):
        topic_changed = False
    if intent == "NEW_SEARCH" and not has_active:
        topic_changed = False  # nothing to switch from

    for k, v in regex.items():
        filters.setdefault(k, v)
    if not standalone:
        standalone = message
    if intent == "COMPARE" and not entities:
        entities = _split_compare(message)

    return TurnDecision(intent=intent, topic_changed=topic_changed,
                        standalone_query=standalone, filters=filters,
                        compare_entities=entities, source=source, raw=raw)


def _split_compare(message: str) -> list:
    parts = re.split(r"\b(?:compare|versus|vs\.?|and|with|between)\b", message, flags=re.I)
    return [p.strip(" ?.,") for p in parts if len(p.strip()) > 3][:4]
