"""Retrieval eval harness for the routed retriever.

Computes ROUTE ACCURACY and RECALL@5 over a golden set, with a per-category
breakdown. The model endpoint is a parameter, so the SAME golden set can be run
against different answer/router models (e.g. 32B vs 14B) per the model policy.

Usage:
  python eval/run_eval.py                                   # uses .env config
  python eval/run_eval.py --golden eval/golden.jsonl
  python eval/run_eval.py --base-url http://gpu:8000/v1 --model Qwen/Qwen3-32B \
                          --llm-api openai --router-model Qwen/Qwen3-8B

Golden line schema (JSONL):
  {"question", "expected_route", "expected_para_key": str|null, "category"?}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def load_golden(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def returned_para_keys(result) -> list[str]:
    if result.kind == "answer":
        # preserve rank order, de-duplicate
        seen, keys = set(), []
        for c in result.chunks:
            k = c["para_key"]
            if k not in seen:
                seen.add(k); keys.append(k)
        return keys
    if result.kind == "disambiguation":
        return [c["para_key"] for c in result.candidates]
    return []  # aggregation


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=os.path.join(HERE, "golden.jsonl"))
    ap.add_argument("--base-url", default=None, help="override OPENAI_BASE_URL")
    ap.add_argument("--model", default=None, help="override MODEL_NAME (answer)")
    ap.add_argument("--router-model", default=None, help="override ROUTER_MODEL_NAME")
    ap.add_argument("--llm-api", default=None, choices=["openai", "ollama"])
    ap.add_argument("--k", type=int, default=5, help="recall@k")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # Override endpoint via env BEFORE importing app (settings are cached).
    if args.base_url:
        os.environ["OPENAI_BASE_URL"] = args.base_url
    if args.model:
        os.environ["MODEL_NAME"] = args.model
    if args.router_model:
        os.environ["ROUTER_MODEL_NAME"] = args.router_model
    if args.llm_api:
        os.environ["LLM_API"] = args.llm_api

    from app.config import get_settings
    from app import metadata
    from app.routed_retrieval import retrieve
    get_settings.cache_clear()
    metadata.clear_cache()
    s = get_settings()
    print(f"[eval] llm_api={s.llm_api} base_url={s.openai_base_url} "
          f"model={s.model_name} router={s.router_model_name or s.model_name}")

    golden = load_golden(args.golden)
    route_correct = 0
    recall_hits = 0
    recall_total = 0
    by_cat: dict[str, dict] = {}
    rows = []

    for ex in golden:
        q = ex["question"]
        exp_route = ex.get("expected_route")
        exp_key = ex.get("expected_para_key")
        cat = ex.get("category", "?")
        result = retrieve(q)
        got_route = result.route
        keys = returned_para_keys(result)

        route_ok = (got_route == exp_route)
        route_correct += route_ok

        cd = by_cat.setdefault(cat, {"route_ok": 0, "n": 0, "rec_ok": 0, "rec_n": 0})
        cd["n"] += 1
        cd["route_ok"] += route_ok

        recall_ok = None
        if exp_key:
            recall_total += 1
            cd["rec_n"] += 1
            recall_ok = exp_key in keys[: args.k]
            recall_hits += recall_ok
            cd["rec_ok"] += recall_ok

        rows.append((q, exp_route, got_route, route_ok, exp_key, recall_ok,
                     result.decision.source, keys[: args.k]))

    n = len(golden)
    print("\n" + "=" * 92)
    for (q, er, gr, rok, ek, reck, src, keys) in rows:
        tag = "OK " if rok else "XX "
        rtag = "" if reck is None else (" recall:PASS" if reck else " recall:FAIL")
        print(f"[{tag}] route {gr:<12} (exp {er:<12} src={src}){rtag}  | {q[:60]}")
        if args.verbose or (ek and not reck):
            print(f"        expected_key={ek}")
            print(f"        got_keys={keys}")
    print("=" * 92)

    print(f"\nROUTE ACCURACY: {route_correct}/{n} = {route_correct/n:.1%}")
    if recall_total:
        print(f"RECALL@{args.k}:     {recall_hits}/{recall_total} = "
              f"{recall_hits/recall_total:.1%}")
    print("\nPer-category:")
    for cat, d in sorted(by_cat.items()):
        rec = f"{d['rec_ok']}/{d['rec_n']}" if d["rec_n"] else "n/a"
        print(f"  {cat:<14} route {d['route_ok']}/{d['n']}   recall@{args.k} {rec}")

    threshold = 0.90
    ok = (recall_hits / recall_total if recall_total else 1.0) >= threshold
    print(f"\nEXIT CRITERIA recall@{args.k} >= {threshold:.0%}: "
          f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
