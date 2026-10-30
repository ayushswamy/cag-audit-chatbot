"""Idempotent, resumable ingestion pipeline over the whole corpus.

Per-PDF parsing is cached to ingest_cache/<period>.json so re-runs (and the
slow LLM reconstruction) resume without re-parsing. All logs are structured
JSON with a run id.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from datetime import datetime, timezone

from ingest import excel_index, layout, report, segmenter, validate
from ingest.models import ParaRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JsonLogger:
    def __init__(self, path: str, run_id: str):
        self.f = open(path, "a", encoding="utf-8")
        self.run_id = run_id

    def log(self, event: str, **kw):
        rec = {"ts": _now(), "run_id": self.run_id, "event": event, **kw}
        line = json.dumps(rec, ensure_ascii=False)
        self.f.write(line + "\n")
        self.f.flush()
        print(line)

    def close(self):
        self.f.close()


def _rec_to_dict(r: ParaRecord) -> dict:
    return dataclasses.asdict(r)


def _rec_from_dict(d: dict) -> ParaRecord:
    return ParaRecord(**d)


def _cache_path(cache_dir: str, period: str) -> str:
    safe = period.replace("/", "_")
    return os.path.join(cache_dir, f"{safe}.json")


def _load_cache(path: str) -> list[ParaRecord] | None:
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return [_rec_from_dict(d) for d in json.load(f)]


def _save_cache(path: str, records: list[ParaRecord]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_rec_to_dict(r) for r in records], f, ensure_ascii=False, indent=2)


def run(pdf_dir: str, excel_path: str, out_dir: str, *,
        only_period: str | None = None, do_reconstruct: bool = False,
        do_store: bool = False, do_embed: bool = False, recreate: bool = False,
        resume: bool = True, ocr: bool = False) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    cache_dir = os.path.join(out_dir, "cache")
    log_dir = os.path.join(out_dir, "logs")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    logger = JsonLogger(os.path.join(log_dir, f"ingest_{run_id}.jsonl"), run_id)
    logger.log("run_start", pdf_dir=pdf_dir, excel=excel_path,
               reconstruct=do_reconstruct, store=do_store, embed=do_embed)

    excel_paras = excel_index.load_excel(excel_path)
    groups = excel_index.map_periods_to_pdfs(excel_paras, pdf_dir)
    unmatched = excel_index.unmatched_pdfs(excel_paras, pdf_dir)

    all_records: list[ParaRecord] = []
    val = validate.ValidationResult(unmatched_pdfs=unmatched)
    stats = {"pdfs": 0, "tables": 0}

    for g in groups:
        if only_period and g.period != only_period:
            continue
        if not g.pdf_path:
            logger.log("period_no_pdf", period=g.period, expected=len(g.paras))
            val.periods.append(validate.reconcile_period(g, []))
            continue

        cache = _cache_path(cache_dir, g.period)
        records = _load_cache(cache) if resume else None
        if records is not None:
            logger.log("period_cached", period=g.period, pdf=os.path.basename(g.pdf_path),
                       paras=len(records))
        else:
            t0 = time.time()
            blocks, tables, pages = layout.parse_pdf(g.pdf_path, ocr=ocr)
            project = g.paras[0].project if g.paras else "Corporate Office"
            records = segmenter.segment(blocks, tables, g.paras, project, g.period,
                                        os.path.basename(g.pdf_path))
            _save_cache(cache, records)
            logger.log("period_parsed", period=g.period,
                       pdf=os.path.basename(g.pdf_path), pages=len(pages),
                       blocks=len(blocks), paras=len(records),
                       seconds=round(time.time() - t0, 1))

        stats["pdfs"] += 1
        stats["tables"] += sum(len(r.tables) for r in records)
        val.periods.append(validate.reconcile_period(g, records))
        all_records.extend(records)

    val.duplicate_keys = validate.check_key_uniqueness(all_records)

    # --- LLM reconstruction (resumable) ---
    if do_reconstruct:
        from ingest import reconstruct
        done = 0
        for g in groups:
            if only_period and g.period != only_period:
                continue
            cache = _cache_path(cache_dir, g.period)
            recs = [r for r in all_records if r.period == g.period]
            changed = False
            for r in recs:
                try:
                    if reconstruct.reconstruct_record(r):
                        done += 1
                        changed = True
                        logger.log("reconstructed", para_key=r.para_key)
                        _save_cache(cache, recs)  # save after each (resume-safe)
                except Exception as e:  # noqa: BLE001
                    logger.log("reconstruct_error", para_key=r.para_key,
                               error=f"{type(e).__name__}: {e}")
            if changed:
                _save_cache(cache, recs)
        stats["reconstructed"] = done

    # --- persistence ---
    if do_store:
        from ingest import store
        store.init_storage()
        n = store.upsert_paras(all_records)
        logger.log("stored_paras", count=n)
    if do_embed:
        from ingest import store
        n = store.embed_and_upsert(all_records, recreate=recreate)
        stats["chunks"] = n
        logger.log("embedded_chunks", count=n)

    # --- reports ---
    report_path = os.path.join(out_dir, "ingestion_report.md")
    review_csv = os.path.join(out_dir, "manual_review.csv")
    report.write_report(report_path, val, all_records, stats)
    n_review = report.write_manual_review_csv(review_csv, all_records)
    logger.log("run_done", paras=len(all_records),
               expected=val.total_expected, matched=val.total_matched,
               flagged=n_review, duplicates=len(val.duplicate_keys),
               fully_reconciled=val.fully_reconciled, report=report_path)
    logger.close()

    return {"records": all_records, "validation": val, "stats": stats,
            "report": report_path, "review_csv": review_csv}
