"""Exercise the real POST /ask endpoint (via FastAPI TestClient, in-process)
for the two exit-criteria questions and print answer + citations + timing.

Same route handler as the HTTP server; avoids running a separate server under
memory pressure on this 16 GB host.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

QUESTIONS = [
    "What did audit observe about the Tapovan HRT package?",
    "What was NTPC's reply on the Noida land parcels?",
]


def main() -> int:
    client = TestClient(app)
    for i, q in enumerate(QUESTIONS, 1):
        print("=" * 90)
        print(f"Q{i}: {q}")
        t0 = time.time()
        r = client.post("/ask", json={"question": q})
        dt = time.time() - t0
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {r.text[:300]}")
            continue
        data = r.json()
        print(f"\n[{dt:.0f}s] ANSWER:\n{data['answer']}\n")
        print("CITATIONS:")
        for c in data["citations"]:
            print(f"  [{c['para_key']} | {c['section']} | {c['page']}]")
        print("\nchunks_used (para | section | page | rerank):")
        for c in data["chunks_used"]:
            print(f"  {c['para_key']} | {c['section']} | {c['page']} | {c['rerank_score']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
