"""Segment a PDF's blocks into structured ParaRecords.

Reading order is geometric (page, then top-to-bottom, then left-to-right).
Para boundaries are the strong heading candidates. The Part (IIA/IIB) of each
heading is resolved by a state machine seeded from the Excel index's Part order
(numbering restarts between Part IIA and Part IIB). Each para's blocks are then
split into Observation vs Reply by the geometric column side.
"""
from __future__ import annotations

import difflib

from ingest import headings, textrepair
from ingest.excel_index import ExcelPara
from ingest.layout import Block, Table
from ingest.models import ParaRecord

_COL_HEADERS = {"audit observation", "management reply", "reply", "audit reply"}


def _reading_key(b: Block):
    # bottom-left origin: larger top = higher on page -> descending top.
    return (b.page, -b.top, b.l)


def _split_embedded_headings(ordered: list[Block]) -> list[Block]:
    """Split any block that merged a previous para's tail with the next para's
    heading (a Docling artifact) into separate blocks, preserving order."""
    out: list[Block] = []
    for b in ordered:
        cuts = headings.embedded_cut_points(b.text)
        if not cuts:
            out.append(b)
            continue
        idxs = [0] + cuts + [len(b.text)]
        for k in range(len(idxs) - 1):
            seg = b.text[idxs[k]:idxs[k + 1]].strip()
            if not seg:
                continue
            out.append(Block(page=b.page, x_center=b.x_center, l=b.l, r=b.r,
                             top=b.top - k * 0.001, side=b.side, text=seg,
                             table_idx=b.table_idx, is_header=False))
    return out


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower()[:120], b.lower()[:120]).ratio()


def _part_order(expected: list[ExcelPara]) -> list[str]:
    order = []
    for e in expected:
        if e.para_type and e.para_type not in order:
            order.append(e.para_type)
    return order or ["IIB"]


def segment(blocks: list[Block], tables: list[Table], expected: list[ExcelPara],
            project: str, period: str, source_pdf: str) -> list[ParaRecord]:
    ordered = sorted((b for b in blocks), key=_reading_key)
    ordered = _split_embedded_headings(ordered)
    cands = headings.detect_candidates(ordered)
    strong = [c for c in cands if c.strength >= 1]

    part_order = _part_order(expected)
    # resolve part per candidate via state machine
    resolved = []  # (pos, num, part, residual, strength)
    cur_idx = 0
    last_num = -1
    for c in strong:
        part = c.part
        if part:
            if part in part_order:
                cur_idx = part_order.index(part)
            cur_part = part
        else:
            # numbering reset -> advance to next Part section
            try:
                n = int("".join(ch for ch in c.num if ch.isdigit()))
            except ValueError:
                n = last_num + 1
            if n <= last_num and cur_idx + 1 < len(part_order):
                cur_idx += 1
            cur_part = part_order[cur_idx]
        try:
            last_num = int("".join(ch for ch in c.num if ch.isdigit()))
        except ValueError:
            pass
        resolved.append((c.block_index, c.num, cur_part, c.residual, c.strength))

    if not resolved:
        return []

    # Assign every block to a para. Default assignment is by reading-order
    # position between consecutive headings. BUT the two columns flow
    # independently: a long reply can continue past the NEXT para's observation
    # heading, and a new para's reply intro can lead its heading. So we apply a
    # continuation rule: a block that starts LOWERCASE is a mid-sentence
    # continuation and inherits the previous same-column block's para (a new
    # para's text always starts with a capitalized sentence). This repairs the
    # column mis-alignment at para boundaries without touching clean paras.
    head_pos = [r[0] for r in resolved]
    head_set = {r[0]: k for k, r in enumerate(resolved)}
    # (page, top) anchor of each heading, for column-aware boundary assignment
    head_coord = [(ordered[p].page, ordered[p].top) for p in head_pos]

    _TOL = 16.0  # pts: a block within TOL of a heading's baseline shares its line

    def _para_for(b: Block) -> int:
        """Para that owns block b by document position, with a small vertical
        tolerance. A para's reply intro often sits a few points ABOVE its
        observation heading (same physical line); without tolerance it sorts
        before the heading and bleeds into the PREVIOUS para. The tolerance
        keeps that same-line reply with its own para."""
        p = 0
        for k, (hpage, htop) in enumerate(head_coord):
            if hpage < b.page or (hpage == b.page and htop >= b.top - _TOL):
                p = k
            else:
                break
        return p

    excel_by_key = {(e.para_type, e.para_no): e for e in expected}
    n = len(resolved)
    para_obs: list[list[str]] = [[] for _ in range(n)]
    para_reply: list[list[str]] = [[] for _ in range(n)]
    para_pages: list[set] = [set() for _ in range(n)]
    prev_para = {"obs": None, "reply": None}

    for idx, b in enumerate(ordered):
        if b.is_header or b.text.strip().lower() in _COL_HEADERS:
            continue
        if idx in head_set:  # a heading block starts a para
            pi = head_set[idx]
            para_pages[pi].add(b.page)
            residual = resolved[pi][3]
            if residual:
                para_obs[pi].append(residual)
            prev_para["obs"] = pi
            continue
        side = b.side
        if b.text[:1].islower() and prev_para[side] is not None:
            pi = prev_para[side]              # continuation -> stay with antecedent
        else:
            pi = _para_for(b)
        prev_para[side] = pi
        para_pages[pi].add(b.page)
        (para_reply[pi] if side == "reply" else para_obs[pi]).append(b.text)

    records: list[ParaRecord] = []
    for i, (pos, num, part, residual, strength) in enumerate(resolved):
        obs_blocks, reply_blocks = para_obs[i], para_reply[i]
        pages = para_pages[i]

        # tables whose page falls in this para's page range and side
        p_lo, p_hi = (min(pages), max(pages)) if pages else (None, None)
        para_tables = []
        for t in tables:
            if p_lo is not None and p_lo <= t.page <= p_hi:
                para_tables.append({"section": t.side, "markdown": t.markdown,
                                    "rows_json": t.rows_json, "page": t.page})

        ex = excel_by_key.get((part, num))
        title = ex.description if ex else (residual[:200] if residual else f"Para {num}")

        obs_text = textrepair.repair_join(obs_blocks)
        reply_text = textrepair.repair_join(reply_blocks)

        rec = ParaRecord(
            para_key=f"{project}|{period}|{part}|{num}",
            project=project, period=period, para_type=part, para_no=num,
            title=title,
            observation_text=obs_text, reply_text=reply_text,
            raw_observation_blocks=obs_blocks, raw_reply_blocks=reply_blocks,
            tables=para_tables,
            page_start=p_lo, page_end=p_hi, source_pdf=source_pdf,
            blocks_obs=len(obs_blocks), blocks_reply=len(reply_blocks),
            heading_strength=strength, matched_excel=ex is not None,
        )
        _score(rec, ex)
        records.append(rec)

    return records


def _score(rec: ParaRecord, ex: ExcelPara | None) -> None:
    reasons = []
    if not rec.reply_text:
        reasons.append("no-reply-column")
    elif len(rec.reply_text) < 120:
        reasons.append("short-reply")
    if len(rec.observation_text) < 150:
        reasons.append("short-observation")
    if ex is None:
        reasons.append("not-in-excel")
    elif _similar(rec.title, ex.description) < 0.35 and rec.observation_text:
        # heading residual vs excel description sanity (title came from excel if
        # matched, so compare the observation start instead)
        if _similar(rec.observation_text[:120], ex.description) < 0.3:
            reasons.append("low-description-match")
    if rec.heading_strength < 2:
        reasons.append("weak-heading")

    # quality score: presence + balance of both columns, capped 0..1
    score = 0.0
    if rec.observation_text:
        score += 0.5
    if rec.reply_text:
        score += 0.3
    if rec.matched_excel:
        score += 0.2
    rec.quality_score = round(score, 2)
    rec.needs_review = bool(reasons) or rec.quality_score < 0.8
    rec.review_reasons = reasons
    rec.column_confidence = "low" if rec.needs_review else "high"
