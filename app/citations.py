"""Citation enforcement (Phase 5).

Every content paragraph of an answer must contain at least one
[para_key | section | p.X] marker that resolves to a RETRIEVED chunk. If any
paragraph is unsupported, the caller regenerates once with a stricter prompt; if
paragraphs are still unsupported, a low-confidence warning is prepended.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_SECTIONS = {"observation", "reply", "table"}

LOW_CONFIDENCE_WARNING = (
    "⚠ Low confidence: one or more statements below could not be linked to a "
    "cited source in the indexed reports. Treat unmarked claims with caution."
)

NOT_FOUND = "not found in the indexed reports"


@dataclass
class Enforcement:
    answer: str
    unsupported_count: int
    regenerated: bool = False
    low_confidence: bool = False
    unsupported_paragraphs: list[str] = field(default_factory=list)


def valid_citations_in(text: str, valid_keys: set[str]) -> list[tuple]:
    out = []
    for m in _BRACKET_RE.finditer(text):
        parts = [p.strip() for p in m.group(1).split(" | ")]
        if len(parts) != 3:
            continue
        para_key, section, page = parts
        if section.lower() in _SECTIONS and para_key in valid_keys:
            out.append((para_key, section, page))
    return out


def _is_exempt(paragraph: str) -> bool:
    """Structural lines that don't assert a fact and need no citation."""
    p = paragraph.strip()
    if not p:
        return True
    if NOT_FOUND in p.lower():
        return True
    if p.startswith(LOW_CONFIDENCE_WARNING[:20]):
        return True
    lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
    # a block whose every line is a header / label / table row / bare tag
    def structural(ln: str) -> bool:
        if ln.startswith("#") or ln.startswith("|") or ln.startswith("**"):
            return True  # markdown heading / table row / bold section header
        if len(ln.split()) <= 3 and ln.endswith(":"):  # short label:
            return True
        if re.fullmatch(r"[\[\]\|\w .:\-]+", ln) and _BRACKET_RE.fullmatch(ln.strip()):
            return True
        return False
    return all(structural(ln) for ln in lines)


def unsupported_paragraphs(text: str, valid_keys: set[str]) -> list[str]:
    paras = re.split(r"\n\s*\n", text)
    bad = []
    for para in paras:
        if _is_exempt(para):
            continue
        if not valid_citations_in(para, valid_keys):
            bad.append(para.strip())
    return bad


def enforce(answer_text: str, valid_keys: set[str], regenerate_fn=None) -> Enforcement:
    """Validate; regenerate once (via regenerate_fn -> str) if unsupported;
    prepend a low-confidence warning if still unsupported."""
    if answer_text.strip().lower().startswith(NOT_FOUND):
        return Enforcement(answer=answer_text, unsupported_count=0)

    bad = unsupported_paragraphs(answer_text, valid_keys)
    regenerated = False
    if bad and regenerate_fn is not None:
        new_text = regenerate_fn()
        if new_text and new_text.strip():
            answer_text = new_text
            regenerated = True
            bad = unsupported_paragraphs(answer_text, valid_keys)

    low_conf = bool(bad)
    if low_conf:
        answer_text = f"{LOW_CONFIDENCE_WARNING}\n\n{answer_text}"
    return Enforcement(answer=answer_text, unsupported_count=len(bad),
                       regenerated=regenerated, low_confidence=low_conf,
                       unsupported_paragraphs=bad)
