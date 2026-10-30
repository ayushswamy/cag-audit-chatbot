"""Final ingestion report + manual-review CSV."""
from __future__ import annotations

import csv
import os

from ingest.models import ParaRecord
from ingest.validate import ValidationResult


def write_manual_review_csv(path: str, records: list[ParaRecord]) -> int:
    rows = [r for r in records if r.needs_review]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["para_key", "source_pdf", "pages", "quality_score",
                    "review_reasons", "obs_len", "reply_len", "matched_excel",
                    "title"])
        for r in sorted(rows, key=lambda x: x.quality_score):
            w.writerow([r.para_key, r.source_pdf,
                        f"{r.page_start}-{r.page_end}", r.quality_score,
                        ";".join(r.review_reasons), len(r.observation_text),
                        len(r.reply_text), r.matched_excel, r.title[:120]])
    return len(rows)


def write_report(path: str, val: ValidationResult, records: list[ParaRecord],
                 stats: dict) -> None:
    lines = []
    lines.append("# Ingestion report — CAG IR corpus\n")
    lines.append("## Summary")
    lines.append(f"- PDFs processed: {stats.get('pdfs', 0)}")
    lines.append(f"- Paras parsed: {len(records)}")
    lines.append(f"- Excel rows expected: {val.total_expected}")
    lines.append(f"- Excel<->PDF matched: {val.total_matched}")
    lines.append(f"- Paras flagged for review: {sum(1 for r in records if r.needs_review)}")
    lines.append(f"- Tables extracted: {stats.get('tables', 0)}")
    lines.append(f"- Chunks embedded: {stats.get('chunks', '(not run)')}")
    lines.append(f"- Reconstructed paras: {stats.get('reconstructed', '(not run)')}")
    lines.append(f"- Composite-key duplicates: {len(val.duplicate_keys)}"
                 + (f" -> {val.duplicate_keys}" if val.duplicate_keys else ""))
    lines.append(f"- FULLY RECONCILED: {'YES' if val.fully_reconciled else 'NO (see exceptions)'}")
    lines.append("")

    if val.unmatched_pdfs:
        lines.append("## PDFs with no Excel rows (period unmatched)")
        for p in val.unmatched_pdfs:
            lines.append(f"- {os.path.basename(p)}")
        lines.append("")

    lines.append("## Per-period reconciliation")
    for pr in val.periods:
        pdfn = os.path.basename(pr.pdf) if pr.pdf else "(no PDF)"
        tag = " [FILENAME/PERIOD MISMATCH]" if pr.filename_mismatch else ""
        lines.append(f"\n### Period {pr.period} — {pdfn}{tag}")
        lines.append(f"- expected={pr.expected} matched={pr.matched}")
        if pr.missing_in_pdf:
            lines.append(f"- MISSING in PDF (Excel rows not located): {pr.missing_in_pdf}")
        if pr.extra_in_pdf:
            lines.append(f"- EXTRA in PDF (not in Excel): {pr.extra_in_pdf}")
        if pr.flagged:
            lines.append(f"- flagged for review: {pr.flagged}")

    lines.append("\n## Reviewed exceptions")
    lines.append("Items below are known/accepted deviations (see manual_review.csv):")
    for pr in val.periods:
        for m in pr.missing_in_pdf:
            lines.append(f"- {pr.period} {m}: Excel row not located in PDF")
        for e in pr.extra_in_pdf:
            lines.append(f"- {pr.period} {e}: para in PDF but not in Excel index")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
