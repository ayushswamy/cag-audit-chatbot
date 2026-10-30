"""CLI:  python -m ingest --pdf-dir data --excel "data/...xlsx" [options]

Modes:
  --dry-run        parse + validate + report only (no LLM, no DB, no embed)
  --reconstruct    run the LLM reassembly pass (resumable)
  --store          upsert structured paras into Postgres
  --embed          chunk + embed into Qdrant
  --recreate       recreate the Qdrant collection before embedding
  --only PERIOD    process a single period (e.g. 2020-23)
  --no-resume      ignore the parse cache and re-parse
"""
from __future__ import annotations

import argparse
import os

from app.config import get_settings
from ingest import pipeline


def main() -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(prog="ingest")
    ap.add_argument("--pdf-dir", default=s.data_dir)
    ap.add_argument("--excel", default=s.excel_index)
    ap.add_argument("--out-dir", default="ingest_out")
    ap.add_argument("--only", default=None, help="single period, e.g. 2020-23")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse+validate+report only")
    ap.add_argument("--reconstruct", action="store_true")
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--recreate", action="store_true")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--ocr", action="store_true")
    args = ap.parse_args()

    dry = args.dry_run
    result = pipeline.run(
        pdf_dir=args.pdf_dir, excel_path=args.excel, out_dir=args.out_dir,
        only_period=args.only,
        do_reconstruct=args.reconstruct and not dry,
        do_store=args.store and not dry,
        do_embed=args.embed and not dry,
        recreate=args.recreate, resume=not args.no_resume, ocr=args.ocr,
    )
    val = result["validation"]
    print("\n" + "=" * 70)
    print(f"REPORT: {result['report']}")
    print(f"REVIEW CSV: {result['review_csv']}")
    print(f"expected={val.total_expected} matched={val.total_matched} "
          f"fully_reconciled={val.fully_reconciled} "
          f"duplicates={len(val.duplicate_keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
