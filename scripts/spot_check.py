"""Spot-check N random stored paras: show separated Observation and Reply text
straight from Postgres, so column-split quality can be eyeballed (Phase 2
exit-criteria check).

    python scripts/spot_check.py [N] [--seed S]
"""
from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app import db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=5)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    engine = db.get_engine()
    with engine.begin() as conn:
        rows = conn.execute(select(db.paras)).mappings().all()
    if not rows:
        print("no paras in DB")
        return 1
    picks = random.sample(rows, min(args.n, len(rows)))
    for r in picks:
        print("=" * 100)
        print(f"{r['para_key']}   [{r['source_pdf']}  p.{r['page_start']}-{r['page_end']}  "
              f"quality={r['quality_score']}  review={r['needs_review']}]")
        print(f"TITLE: {r['title']}")
        print(f"\n-- OBSERVATION ({len(r['observation_text'])} chars) --")
        print(r['observation_text'][:600])
        print(f"\n-- MANAGEMENT REPLY ({len(r['reply_text'])} chars) --")
        print(r['reply_text'][:600])
        rec = r.get('observation_reconstructed')
        if rec:
            print(f"\n-- OBSERVATION (LLM-reconstructed) --")
            print(rec[:400])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
