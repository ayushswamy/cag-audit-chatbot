"""Multi-turn dialogue eval. Runs each scripted dialogue through the
conversation orchestrator (fresh session per dialogue) and asserts intent,
topic-change/reset, query rewriting, and retrieved/active para.

Runs in fast mode (generate_answer=False): it exercises the analysis + routing
+ retrieval + state machinery without the slow answer LLM.

Usage:
  python eval/run_dialogue_eval.py
  python eval/run_dialogue_eval.py --base-url http://gpu:8000/v1 \
                                   --router-model Qwen/Qwen3-8B --llm-api openai
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def retrieved_keys(r):
    seen, keys = set(), []
    for c in r.get("chunks", []):
        k = c["para_key"]
        if k not in seen:
            seen.add(k); keys.append(k)
    for c in r.get("candidates", []):
        keys.append(c["para_key"])
    return keys


def check_turn(turn, r):
    """Return list of (assertion, ok, detail)."""
    out = []
    if "expect_intent" in turn:
        ok = r["intent"] == turn["expect_intent"]
        out.append(("intent", ok, f"{r['intent']} vs {turn['expect_intent']}"))
    if "expect_topic_changed" in turn:
        ok = bool(r["topic_changed"]) == bool(turn["expect_topic_changed"])
        out.append(("topic_changed", ok, f"{r['topic_changed']}"))
    if "expect_kind" in turn:
        ok = r["kind"] == turn["expect_kind"]
        out.append(("kind", ok, f"{r['kind']} vs {turn['expect_kind']}"))
    if "expect_para_key" in turn:
        keys = retrieved_keys(r)
        ok = turn["expect_para_key"] in keys or r.get("active_para_key") == turn["expect_para_key"]
        out.append(("para_key", ok, f"want {turn['expect_para_key']} in {keys[:3]}"))
    if "expect_active" in turn:
        want = None if turn["expect_active"] in (None, "None") else turn["expect_active"]
        ok = r.get("active_para_key") == want
        out.append(("active", ok, f"{r.get('active_para_key')} vs {want}"))
    if turn.get("expect_rewrite"):
        sq = (r.get("standalone_query") or "").lower()
        ok = sq != turn["user"].lower() and len(sq) > len(turn["user"])
        out.append(("rewrite", ok, f"sq='{r.get('standalone_query')}'"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dialogues", default=os.path.join(HERE, "dialogues.jsonl"))
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--router-model", default=None)
    ap.add_argument("--llm-api", default=None, choices=["openai", "ollama"])
    ap.add_argument("--no-rerank", action="store_true",
                    help="skip the cross-encoder reranker (memory escape hatch; "
                         "by default the reranker IS used)")
    ap.add_argument("--keep-alive", default="0",
                    help="Ollama keep_alive; '0' unloads the LLM between calls so "
                         "the reranker fits in RAM on a small host (default '0')")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.base_url: os.environ["OPENAI_BASE_URL"] = args.base_url
    if args.model: os.environ["MODEL_NAME"] = args.model
    if args.router_model: os.environ["ROUTER_MODEL_NAME"] = args.router_model
    if args.llm_api: os.environ["LLM_API"] = args.llm_api
    if args.no_rerank:
        os.environ["DISABLE_RERANK"] = "true"
    if args.keep_alive:
        os.environ["OLLAMA_KEEP_ALIVE"] = args.keep_alive

    from app.config import get_settings
    from app import metadata, conversation
    get_settings.cache_clear(); metadata.clear_cache()
    s = get_settings()
    print(f"[eval] llm_api={s.llm_api} model={s.model_name} "
          f"router={s.router_model_name or s.model_name}")

    dialogues = load(args.dialogues)
    total_assert = pass_assert = 0
    dialogues_passed = 0
    exit_dialogue_ok = None

    for d in dialogues:
        sid = None
        dialogue_ok = True
        print("\n" + "=" * 90)
        print(f"DIALOGUE: {d['name']}")
        for turn in d["turns"]:
            r = conversation.respond(sid, turn["user"], generate_answer=False)
            sid = r["session_id"]
            checks = check_turn(turn, r)
            line_ok = all(ok for _, ok, _ in checks)
            dialogue_ok = dialogue_ok and line_ok
            tag = "OK " if line_ok else "XX "
            print(f"  [{tag}] '{turn['user']}'  intent={r['intent']} "
                  f"topic_changed={r['topic_changed']} active={r.get('active_para_key')}")
            for name, ok, detail in checks:
                total_assert += 1; pass_assert += ok
                if not ok or args.verbose:
                    print(f"        {'ok' if ok else 'FAIL'} {name}: {detail}")
        dialogues_passed += dialogue_ok
        if d["name"].startswith("exit-criteria"):
            exit_dialogue_ok = dialogue_ok

    print("\n" + "=" * 90)
    print(f"DIALOGUES PASSED: {dialogues_passed}/{len(dialogues)}")
    print(f"ASSERTIONS PASSED: {pass_assert}/{total_assert} = "
          f"{pass_assert/max(total_assert,1):.1%}")
    print(f"EXIT-CRITERIA DIALOGUE (Mandakini->BG->Loharinagpala): "
          f"{'PASS' if exit_dialogue_ok else 'FAIL'}")
    return 0 if exit_dialogue_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
