"""Run the smoke questions against a running API and print, for each:
question -> answer -> citations. Also flags any answer that came back as the
"not found" sentinel so regressions are obvious.

Usage:
    # API must be running (docker compose up, or uvicorn app.main:app)
    python scripts/run_smoke.py                 # uses http://localhost:8080
    python scripts/run_smoke.py --url http://localhost:8080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS = os.path.join(HERE, "..", "tests", "smoke_questions.json")
NOT_FOUND = "not found in the indexed reports"


def ask(url: str, question: str) -> dict:
    body = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/ask", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--questions", default=QUESTIONS)
    args = ap.parse_args()

    with open(args.questions, encoding="utf-8") as f:
        data = json.load(f)
    questions = data["questions"]

    not_found = 0
    for i, q in enumerate(questions, 1):
        print("=" * 80)
        print(f"Q{i}: {q}")
        try:
            resp = ask(args.url, q)
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] {type(e).__name__}: {e}")
            continue
        answer = resp.get("answer", "")
        print(f"\nANSWER:\n{answer}\n")
        cites = resp.get("citations", [])
        if cites:
            print("CITATIONS:")
            for c in cites:
                print(f"  - [{c['para_key']} | {c['section']} | {c['page']}]")
        else:
            print("CITATIONS: (none)")
        if NOT_FOUND in answer.lower():
            not_found += 1
        chunks = resp.get("chunks_used", [])
        print(f"\nchunks_used: {len(chunks)} "
              f"(top para_keys: {', '.join(sorted({c['para_key'] for c in chunks}))})")

    print("=" * 80)
    print(f"Summary: {len(questions)} questions, {not_found} answered '{NOT_FOUND}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
