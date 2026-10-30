"""Load the Excel index and map each period to its PDF file.

The Excel is the ground truth for WHICH paras exist. Ingestion is Excel-guided:
we locate each expected (period, para_type, para_no) inside its PDF, which is
exactly what the reconciliation report checks.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field

import openpyxl

PERIOD_RE = re.compile(r"(\d{4}-\d{2,4})")

# Known filename<->Excel-period mismatches (pdf_period -> excel_period).
# The 2024-25 PDF is the 2023-24 report (audit period 2023-24, issued 2025-26).
PERIOD_OVERRIDES = {"2024-25": "2023-24"}


@dataclass
class ExcelPara:
    sr_no: str
    project: str
    period: str
    addition_year: str
    para_type: str          # IIA / IIB
    para_no: str            # may be alphanumeric e.g. "4A"
    description: str


@dataclass
class PeriodGroup:
    period: str
    pdf_path: str | None
    paras: list[ExcelPara] = field(default_factory=list)
    filename_mismatch: bool = False


def _norm_period(s) -> str:
    return str(s).strip() if s is not None else ""


def load_excel(excel_path: str) -> list[ExcelPara]:
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    out: list[ExcelPara] = []
    for r in rows[1:]:
        if r[8] is None and r[9] is None:
            continue
        out.append(
            ExcelPara(
                sr_no=str(r[0]).strip() if r[0] is not None else "",
                project=str(r[4]).strip() if r[4] else "",
                period=_norm_period(r[5]),
                addition_year=str(r[6]).strip() if r[6] else "",
                para_type=str(r[7]).strip() if r[7] else "",
                para_no=str(r[8]).strip() if r[8] is not None else "",
                description=str(r[9]).strip() if r[9] else "",
            )
        )
    return out


def _pdf_period(path: str) -> str | None:
    m = PERIOD_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def map_periods_to_pdfs(excel_paras: list[ExcelPara], pdf_dir: str) -> list[PeriodGroup]:
    """Group Excel paras by period and attach the matching PDF, applying known
    filename/period overrides and flagging mismatches."""
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
    pdf_by_period: dict[str, str] = {}
    for p in pdfs:
        per = _pdf_period(p)
        if per:
            pdf_by_period[per] = p

    # group excel by period preserving order
    periods: dict[str, list[ExcelPara]] = {}
    for ep in excel_paras:
        periods.setdefault(ep.period, []).append(ep)

    groups: list[PeriodGroup] = []
    for period, paras in periods.items():
        pdf = pdf_by_period.get(period)
        mismatch = False
        if pdf is None:
            # try override: some pdf_period maps to this excel period
            for pdf_per, exc_per in PERIOD_OVERRIDES.items():
                if exc_per == period and pdf_per in pdf_by_period:
                    pdf = pdf_by_period[pdf_per]
                    mismatch = True
                    break
        groups.append(PeriodGroup(period=period, pdf_path=pdf, paras=paras,
                                  filename_mismatch=mismatch))
    return groups


def unmatched_pdfs(excel_paras: list[ExcelPara], pdf_dir: str) -> list[str]:
    """PDFs on disk whose period has no Excel rows (and no override)."""
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))
    excel_periods = {ep.period for ep in excel_paras}
    override_pdf_periods = {k for k, v in PERIOD_OVERRIDES.items() if v in excel_periods}
    out = []
    for p in pdfs:
        per = _pdf_period(p)
        if per and per not in excel_periods and per not in override_pdf_periods:
            out.append(p)
    return out
