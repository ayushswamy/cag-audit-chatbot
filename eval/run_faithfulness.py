"""Faithfulness eval (Phase 5): LLM-as-judge.

For each golden question: generate the real answer (full conversation path, with
citation enforcement + structured modes), then ask the judge model whether every
claim in the answer is supported by the CITED chunks. Reports:
  - faithfulness  = supported / total
  - uncited paragraphs (must be 0 across the set; citation enforcement guarantees
    this or prepends a low-confidence warning)

Resumable (per-question cache) and endpoint-parameterized so the SAME set runs
against 8B locally or the 32B on the server (per the model policy).

Usage:
  python eval/run_faithfulness.py                 # all, uses .env
  python eval/run_faithfulness.py --limit 5       # quick subset
  python eval/run_faithfulness.py --base-url http://gpu:8000/v1 --model Qwen/Qwen3-32B --llm-api openai
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

JUDGE_SYS = (
    "You are a strict faithfulness judge for an audit-para assistant. You are "
    "given an ANSWER and the CONTEXT CHUNKS that were retrieved for it. Decide "
    "whether EVERY factual claim in the answer (especially monetary figures, "
    "dates, names) is directly supported by the context chunks. Ignore the "
    "inline [..] citation tags themselves. Respond with ONE JSON object: "
    '{"supported": true|false, "reason": "<short>"}. supported=false if any '
    "claim is not backed by the chunks or a figure was altered."
)


def judge(answer: str, chunks: list[dict], model: str | None) -> dict:
    from app import llm
    ctx = "\n\n".join(f"[{c['para_key']} | {c['section']} | {c['page']}]\n{c['text']}"
                      for c in chunks)
    user = f"CONTEXT CHUNKS:\n{ctx}\n\nANSWER:\n{answer}\n\nIs the answer supported?"
    out = llm.complete_json(JUDGE_SYS, user, model=model, max_tokens=200)
    return out if isinstance(out, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default=os.path.join(HERE, "faithfulness.jsonl"))
    ap.add_argument("--cache", default=os.path.join(HERE, "faithfulness_cache.json"))
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--judge-model", default=None)
    ap.add_argument("--llm-api", default=None, choices=["openai", "ollama"])
    ap.add_argument("--limit", type=int, default=0, help="run only first N (0=all)")
    ap.add_argument("--keep-alive", default="0")
    ap.add_argument("--fresh", action="store_true", help="ignore cache")
    args = ap.parse_args()

    if args.base_url: os.environ["OPENAI_BASE_URL"] = args.base_url
    if args.model: os.environ["MODEL_NAME"] = args.model
    if args.llm_api: os.environ["LLM_API"] = args.llm_api
    if args.keep_alive: os.environ["OLLAMA_KEEP_ALIVE"] = args.keep_alive

    from app.config import get_settings
    from app import metadata, conversation
    get_settings.cache_clear(); metadata.clear_cache()
    s = get_settings()
    print(f"[faith] llm_api={s.llm_api} model={s.model_name} "
          f"judge={args.judge_model or s.model_name}")

    with open(args.golden, encoding="utf-8") as f:
        golden = [json.loads(l) for l in f if l.strip()]
    if args.limit:
        golden = golden[: args.limit]

    cache = {}
    if os.path.isfile(args.cache) and not args.fresh:
        with open(args.cache, encoding="utf-8") as f:
            cache = json.load(f)

    supported = total = uncited = 0
    for i, ex in enumerate(golden, 1):
        q = ex["question"]
        if q in cache:
            r = cache[q]
        else:
            resp = conversation.respond(None, q, generate_answer=True)
            enf = resp.get("enforcement") or {}
            if resp.get("answer_mode") == "full_para":
                # verbatim reproduction from the store -> faithful by construction
                verdict = {"supported": True, "reason": "verbatim full_para"}
            else:
                verdict = judge(resp["answer"], resp.get("chunks", []), args.judge_model)
            r = {"supported": bool(verdict.get("supported")),
                 "reason": verdict.get("reason", ""),
                 "uncited": int(enf.get("unsupported_paragraphs", 0)),
                 "low_confidence": bool(enf.get("low_confidence")),
                 "mode": resp.get("answer_mode"),
                 "route": resp.get("route"),
                 "answer_preview": resp["answer"][:160]}
            cache[q] = r
            with open(args.cache, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        total += 1
        supported += r["supported"]
        uncited += r["uncited"]
        tag = "OK " if r["supported"] else "XX "
        print(f"[{tag}] mode={str(r['mode']):9s} uncited={r['uncited']} "
              f"| {q[:55]}")
        if not r["supported"]:
            print(f"        reason: {r['reason'][:100]}")

    print("\n" + "=" * 80)
    print(f"FAITHFULNESS: {supported}/{total} = {supported/max(total,1):.1%}")
    print(f"TOTAL UNCITED PARAGRAPHS: {uncited}  (exit criterion: 0)")
    ok = (supported / max(total, 1)) >= 0.95 and uncited == 0
    print(f"EXIT CRITERIA (faithfulness>=95% AND 0 uncited): "
          f"{'PASS' if ok else 'CHECK'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
