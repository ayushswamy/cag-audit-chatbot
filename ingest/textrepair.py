"""Text repair for CAG PDF extraction artifacts:
  - broken drop-caps:  "1. T he management" -> "1. The",  "3. N TPC" -> "NTPC"
  - hyphenation across line breaks: "expen-\nditure" -> "expenditure"
  - collapse the non-breaking-space padding Docling emits
  - stray single-character splits inside numbers/words from column rendering
"""
from __future__ import annotations

import re

_WS = re.compile(r"[ \t   ]+")
_MULTI_NL = re.compile(r"\n{3,}")


def collapse_ws(text: str) -> str:
    if not text:
        return ""
    text = text.replace(" ", " ")
    text = _WS.sub(" ", text)
    return text.strip()


def fix_hyphenation(text: str) -> str:
    # word- \n word  ->  wordword   (hyphen at end of line before lowercase)
    text = re.sub(r"(\w)[-‐]\s*\n\s*([a-z])", r"\1\2", text)
    # also handle " - " mid hyphenated wraps left by extraction: keep as-is.
    return text


def fix_dropcaps(text: str) -> str:
    if not text:
        return text
    # after a section number: "1. T he" -> "1. The"; "3. N TPC" -> "3. NTPC"
    text = re.sub(r"(\b\d+\.\s+)([A-Z]) ([A-Za-z])", r"\1\2\3", text)
    # at the start of a segment: "T he ..." -> "The ..."
    text = re.sub(r"^([A-Z]) ([A-Za-z])", r"\1\2", text.strip())
    # broken 'Part' marker artifacts: "Part �IIB" / "Part -IIB" tolerated
    return text


def repair(text: str) -> str:
    """Full single-block repair (order matters: hyphenation before ws collapse
    would need the newlines; we keep newlines through hyphenation then collapse)."""
    if not text:
        return ""
    text = text.replace(" ", " ")
    text = fix_hyphenation(text)
    text = collapse_ws(text)
    text = fix_dropcaps(text)
    return text


def repair_join(blocks: list[str]) -> str:
    """Repair each block then join with single spaces, de-duplicating the
    accidental blank fragments."""
    parts = [repair(b) for b in blocks]
    parts = [p for p in parts if p]
    return "\n".join(parts)
