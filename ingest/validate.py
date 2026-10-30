"""Validation layer: Excel<->PDF reconciliation and composite-key uniqueness.

Every Excel row must resolve to a para heading in its PDF; every para heading
found must (ideally) exist in Excel. Mismatches are reported, never skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ingest.excel_index import ExcelPara, PeriodGroup
from ingest.models import ParaRecord


@dataclass
class PeriodReconciliation:
    period: str
    pdf: str | None
    filename_mismatch: bool
    expected: int
    matched: int
    missing_in_pdf: list[str] = field(default_factory=list)      # Excel rows not found
    extra_in_pdf: list[str] = field(default_factory=list)        # PDF paras not in Excel
    flagged: list[str] = field(default_factory=list)             # needs_review para_keys


@dataclass
class ValidationResult:
    periods: list[PeriodReconciliation] = field(default_factory=list)
    duplicate_keys: list[str] = field(default_factory=list)
    unmatched_pdfs: list[str] = field(default_factory=list)

    @property
    def total_expected(self) -> int:
        return sum(p.expected for p in self.periods)

    @property
    def total_matched(self) -> int:
        return sum(p.matched for p in self.periods)

    @property
    def fully_reconciled(self) -> bool:
        return (all(not p.missing_in_pdf for p in self.periods)
                and not self.duplicate_keys)


def reconcile_period(group: PeriodGroup, records: list[ParaRecord]) -> PeriodReconciliation:
    expected_keys = {(e.para_type, e.para_no) for e in group.paras}
    found_keys = {(r.para_type, r.para_no) for r in records}

    missing = sorted(f"{t}-{n}" for (t, n) in expected_keys - found_keys)
    extra = sorted(f"{t}-{n}" for (t, n) in found_keys - expected_keys)
    matched = len(expected_keys & found_keys)
    flagged = [r.para_key for r in records if r.needs_review]

    return PeriodReconciliation(
        period=group.period,
        pdf=group.pdf_path,
        filename_mismatch=group.filename_mismatch,
        expected=len(expected_keys),
        matched=matched,
        missing_in_pdf=missing,
        extra_in_pdf=extra,
        flagged=flagged,
    )


def check_key_uniqueness(all_records: list[ParaRecord]) -> list[str]:
    seen, dups = set(), []
    for r in all_records:
        if r.para_key in seen:
            dups.append(r.para_key)
        seen.add(r.para_key)
    return sorted(set(dups))
