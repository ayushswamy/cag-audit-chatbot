"""Verify the retrieval+rerank path WITHOUT the LLM (which needs the Qwen
endpoint). Embeds each question, hybrid-searches Qdrant, reranks, and prints
the top paras/sections so we can confirm the right para is retrieved.

Exit-criteria check: the Tapovan question should surface para IIB-2, and the
Noida question should surface para IIB-1.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.retrieval import retrieve  # noqa: E402

QUESTIONS = [
    ("What did audit observe about the Tapovan HRT package?", "Corporate Office|2019-20|IIB|2"),
    ("What was NTPC's reply on the Noida land parcels?", "Corporate Office|2019-20|IIB|1"),
    ("How much funds were blocked due to ineffective recovery from DISCOMs?", "Corporate Office|2019-20|IIB|7"),
    ("What was disallowed by CERC at Kudgi?", "Corporate Office|2019-20|IIB|4"),
]


def main() -> int:
    passed = 0
    for q, expect in QUESTIONS:
        chunks = retrieve(q)
        print("=" * 90)
        print(f"Q: {q}")
        print(f"expected top para_key: {expect}")
        if not chunks:
            print("  NO RESULTS")
            continue
        top = chunks[0]["para_key"]
        ok = top == expect
        passed += ok
        print(f"  -> top para_key: {top}  [{'PASS' if ok else 'CHECK'}]")
        for c in chunks:
            print(f"     {c['rerank_score']:.4f}  {c['para_key']} | {c['section']} "
                  f"| {c['page']} :: {c['text'][:80]}")
    print("=" * 90)
    print(f"top-1 matches: {passed}/{len(QUESTIONS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
