"""Flexible para-heading detection across the many CAG formats, e.g.:
    "Para No. 3, Part IIB"      "Para No: 1, Part IIA"
    "Para no.- 3, Part IIA"     "Para no. – 7,"      "Para no .8,"
    "Para 2, Part IIB"          "Para No 1 Infructuous ..."
    "Para No. 1: Avoidable ..." "Para 02:"   "Para 04A:"  (2024-25 sub-paras)

Bare "Para 10 above" / "Para 30 of ..." cross-references must NOT be treated as
headings — we score each candidate and disambiguate against the Excel index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The core "Para <num>" token, searched within the leading portion of a block.
PARA_CORE_RE = re.compile(
    r"Para\b\s*(?:N[o0]\b\.?)?\s*[:.\-–—]?\s*"
    r"(?P<num>\d{1,2})\s*(?P<sub>[A-Ca-c])?\b",
    re.IGNORECASE,
)
# Everything that may legitimately precede "Para" in a heading (never real
# sentence words) — used to reject mid-sentence cross-references like "Para 10
# above" (preceded by narrative text).
PREFIX_JUNK_RE = re.compile(
    r"^\s*(?:\d{1,3}[.)]\s+)?"
    r"(?:(?:Corporate\s+Office|Corporate\s+office\s+IR|IR|Part|II[AB]|"
    r"\d{4}-\d{2,4}|[-–—,.:;()\s�])\s*)*$",
    re.IGNORECASE,
)
PART_RE = re.compile(r"Part\s*[-–—\s�]*(II[AB])", re.IGNORECASE)
LOOK = 90  # chars of the block start to inspect

# Retained for the embedded-heading splitter (headings merged mid-block).
HEADING_RE = PARA_CORE_RE

_REF_WORDS = re.compile(r"^\s*(above|below|of|and|read|ibid|ii?bid|\d)", re.IGNORECASE)
_PERIOD_TAG = re.compile(r"Corporate\s+office\s+IR\s+\d{4}-\d{2,4}", re.IGNORECASE)
_PERIOD_ANY = re.compile(r"\d{4}-\d{2,4}")

# A heading that appears MID-block (Docling merged the previous para's tail with
# the next heading). These reliably carry the "Corporate office IR <period>"
# tag, so requiring it avoids matching cross-references. Handles Part before/after.
EMBEDDED_HEADING_RE = re.compile(
    r"(?:Part\s*[-–—\s]*II[AB]\s*[,:]\s*)?"
    r"Para\b\s*(?:N[o0]\b\.?)?\s*[:.\-–—]?\s*\d{1,2}[A-Ca-c]?\b"
    r"[\s,:.\-–—]*(?:Part\s*[-–—\s]*II[AB]\s*)?,?\s*"
    r"Corporate\s+office\s+IR\s+\d{4}-\d{2,4}",
    re.IGNORECASE,
)


def embedded_cut_points(text: str) -> list[int]:
    """Start indices of embedded (mid-block) headings, excluding a heading that
    already sits at the block start."""
    return [m.start() for m in EMBEDDED_HEADING_RE.finditer(text) if m.start() > 3]


@dataclass
class HeadingCandidate:
    block_index: int
    page: int
    num: str            # e.g. "1" or "4A"
    part: str | None    # "IIA"/"IIB" if explicit
    residual: str       # block text after the heading prefix (start of body)
    strength: int       # higher = more clearly a real heading


def _residual(text: str, end_idx: int) -> str:
    tail = text[end_idx:]
    # drop a trailing "Part IIB" and any "Corporate office IR <period>" tag
    tail = re.sub(r"^[\s,:.\-–—]*Part\s*[-–—\s�]*II[AB]", "", tail, flags=re.IGNORECASE)
    tail = _PERIOD_TAG.sub("", tail)
    tail = re.sub(r"^[\s,:.\-–—]+", "", tail)
    return tail.strip()


def detect_candidates(blocks) -> list[HeadingCandidate]:
    cands: list[HeadingCandidate] = []
    for i, b in enumerate(blocks):
        if b.is_header:
            continue
        head = b.text[:LOOK]
        m = PARA_CORE_RE.search(head)
        if not m:
            continue
        # everything before "Para ..." must be heading-junk, not sentence text
        if not PREFIX_JUNK_RE.match(b.text[:m.start()]):
            continue

        num = m.group("num")
        sub = m.group("sub")
        try:
            num = str(int(num))          # "02" -> "2"
        except ValueError:
            pass
        if sub:
            num = f"{num}{sub.upper()}"

        pm = PART_RE.search(head)
        part = pm.group(1).upper() if pm else None
        residual = _residual(b.text, m.end())

        strength = 0
        if part:
            strength += 3
        if _PERIOD_ANY.search(head):
            strength += 2
        after = b.text[m.end():].lstrip()
        if after.startswith(":"):
            strength += 2
        if residual and not _REF_WORDS.match(residual):
            strength += 1
        if _REF_WORDS.match(after) or _REF_WORDS.match(residual):
            strength -= 3
        cands.append(HeadingCandidate(block_index=i, page=b.page, num=num,
                                     part=part, residual=residual, strength=strength))
    return cands
