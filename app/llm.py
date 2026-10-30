"""Answer LLM (Qwen). Strict grounding prompt: answer ONLY from provided
chunks, cite after every claim, else say not found.

Two transports, selected by LLM_API (shared prompt/parsing):
  - "openai": OpenAI-compatible endpoint (vLLM/prod). Qwen3 thinking is
    disabled via the vLLM chat-template arg enable_thinking=false.
  - "ollama": Ollama's native /api/chat (local dev), where think=false
    reliably disables the reasoning pass (the OpenAI-compat shim does not).

Model name + endpoint come entirely from .env, so swapping 32B <-> 8B (or the
whole endpoint) is a one-line config change.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import httpx
from openai import OpenAI

from app.config import get_settings

NOT_FOUND = "not found in the indexed reports"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _keep_alive_value(raw: str | None):
    """Ollama's JSON `keep_alive` must be a NUMBER (seconds; negative = keep
    forever) or a duration STRING WITH A UNIT ('10m', '-1m'). A bare '-1'/'0'
    string is parsed by Ollama as a Go duration and rejected with 400 for the
    missing unit — even though '-1' is valid as Ollama's OLLAMA_KEEP_ALIVE env
    var. So coerce plain numeric strings to int; leave real durations as-is."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw

SYSTEM_PROMPT = (
    "You are an internal-audit assistant for CAG Inspection Reports on NTPC. "
    "You answer STRICTLY and ONLY from the context chunks provided. "
    "Each chunk begins with a line 'CITE AS: [...]'.\n"
    "Rules:\n"
    "1. Use only facts stated in the chunks. Never use outside knowledge.\n"
    "2. The chunks are ordered most-relevant first. Base your answer on the "
    "most relevant chunks; do NOT pull facts from a different audit para than "
    "the one the question is about.\n"
    "3. The middle field of each CITE AS tag is the section: 'observation' = "
    "the audit's finding/observation, 'reply' = NTPC management's reply, "
    "'table' = a data table. When the question asks what AUDIT observed, answer "
    "from 'observation' chunks; when it asks about NTPC's/management's REPLY, "
    "answer from 'reply' chunks. A 'reply' chunk IS NTPC's reply even if it does "
    "not use the word 'reply' — summarize its content; never say the reply is "
    "missing when a 'reply' chunk is provided.\n"
    "4. After EVERY sentence, append the exact CITE AS tag(s) of the chunk(s) "
    "that support it, copied verbatim including the square brackets. If you use "
    "any chunk at all, you MUST include its tag.\n"
    "5. Preserve monetary values verbatim (Rs., crore, lakh) — never convert "
    "or round.\n"
    f"6. If the chunks do not support an answer, reply exactly: \"{NOT_FOUND}\".\n"
    "7. Be concise and factual. Do not speculate.\n"
    "8. Default to clear prose (short paragraphs). Use a list/table ONLY when the "
    "user's CURRENT question explicitly asks (e.g. 'in points', 'as a table') — "
    "never switch format on your own or because an earlier turn used one.\n"
    "9. When asked for 'points'/'bullets', produce 4-8 SHORT bullets — ONE fact "
    "each (a single sentence), NEVER a whole paragraph in one bullet. If both "
    "the observation and the reply are wanted, format EXACTLY like this shape:\n"
    "Observation:\n"
    "- <one fact> [tag]\n"
    "- <one fact> [tag]\n"
    "- <one fact> [tag]\n"
    "Reply:\n"
    "- <one fact> [tag]\n"
    "- <one fact> [tag]\n"
    "Put the CITE AS tag at the end of each bullet, list item, or table row.\n\n"
    "Example of the required citation style:\n"
    "  The contract was awarded for Rs. 634.04 crore "
    "[Corporate Office|2019-20|IIB|2 | observation | p.3-6]."
)


def build_context(chunks: list[dict]) -> str:
    """chunks: [{para_key, section, page, text}] -> numbered context block,
    each with an exact, copy-able CITE AS tag."""
    lines = []
    for i, c in enumerate(chunks, 1):
        tag = f"[{c['para_key']} | {c['section']} | {c['page']}]"
        lines.append(f"### Chunk {i}\nCITE AS: {tag}\n{c['text']}")
    return "\n\n".join(lines)


def _user_prompt(question: str, context: str, state_summary: str | None = None) -> str:
    state = f"Conversation state:\n{state_summary}\n\n" if state_summary else ""
    return (
        f"{state}"
        f"Context chunks:\n\n{context}\n\n"
        f"Question: {question}\n\n"
        "Answer using only the chunks above, with inline citations."
    )


@lru_cache
def get_client() -> OpenAI:
    s = get_settings()
    return OpenAI(base_url=s.openai_base_url, api_key=s.openai_api_key)


STRICT_SUFFIX = (
    "\n\nSTRICT MODE: your previous answer contained a sentence with no valid "
    "citation. Re-answer so EVERY sentence ends with the exact CITE AS tag of a "
    "provided chunk. Drop any claim not supported by the chunks."
)


def _complete_answer(system_prompt: str, user_prompt: str,
                     state_summary: str | None = None) -> str:
    """Core generation used by all answer modes: configured transport, thinking
    disabled, reasoning stripped. State summary (if any) is prepended."""
    s = get_settings()
    if state_summary:
        user_prompt = f"Conversation state:\n{state_summary}\n\n{user_prompt}"
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    if s.llm_api == "ollama":
        base = s.openai_base_url.rstrip("/")
        native = base[:-3].rstrip("/") + "/api/chat" if base.endswith("/v1") else base + "/api/chat"
        payload = {
            "model": s.model_name, "think": not s.llm_disable_thinking, "stream": False,
            "options": {"temperature": s.llm_temperature, "num_predict": s.llm_max_tokens},
            "messages": messages,
        }
        ka = _keep_alive_value(s.ollama_keep_alive)
        if ka is not None:
            payload["keep_alive"] = ka
        r = httpx.post(native, json=payload, timeout=600)
        r.raise_for_status()
        text = r.json().get("message", {}).get("content", "") or ""
    else:
        kwargs = {}
        if s.llm_disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = get_client().chat.completions.create(
            model=s.model_name, temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens, messages=messages, **kwargs)
        text = resp.choices[0].message.content or ""
    return _THINK_RE.sub("", text).strip()


def complete(system_prompt: str, user_prompt: str, max_tokens: int | None = None) -> str:
    """Generic single-turn completion using the configured transport. Used by
    the ingestion LLM-reconstruction pass."""
    s = get_settings()
    mt = max_tokens or s.llm_max_tokens
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    if s.llm_api == "ollama":
        base = s.openai_base_url.rstrip("/")
        native = base[:-3].rstrip("/") + "/api/chat" if base.endswith("/v1") else base + "/api/chat"
        payload = {
            "model": s.model_name, "think": not s.llm_disable_thinking, "stream": False,
            "options": {"temperature": s.llm_temperature, "num_predict": mt},
            "messages": messages,
        }
        ka = _keep_alive_value(s.ollama_keep_alive)
        if ka is not None:
            payload["keep_alive"] = ka
        r = httpx.post(native, json=payload, timeout=1800)
        r.raise_for_status()
        text = r.json().get("message", {}).get("content", "") or ""
    else:
        kwargs = {}
        if s.llm_disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = get_client().chat.completions.create(
            model=s.model_name, temperature=s.llm_temperature, max_tokens=mt,
            messages=messages, **kwargs)
        text = resp.choices[0].message.content or ""
    return _THINK_RE.sub("", text).strip()


def _extract_json(text: str) -> dict:
    """Best-effort parse of a JSON object from model output."""
    text = _THINK_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # fall back to the first {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    return {}


def complete_json(system_prompt: str, user_prompt: str, model: str | None = None,
                  max_tokens: int | None = None) -> dict:
    """Single-turn completion forced to JSON output. Used by the router.

    `model` overrides the configured model (e.g. the small router model).
    Returns a parsed dict ({} on failure — caller decides the fallback).
    """
    s = get_settings()
    mt = max_tokens or s.router_max_tokens
    mdl = model or s.router_model_name or s.model_name
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    if s.llm_api == "ollama":
        base = s.openai_base_url.rstrip("/")
        native = base[:-3].rstrip("/") + "/api/chat" if base.endswith("/v1") else base + "/api/chat"
        payload = {
            "model": mdl, "think": not s.llm_disable_thinking, "stream": False,
            "format": "json",  # Ollama: force valid JSON
            "options": {"temperature": 0.0, "num_predict": mt},
            "messages": messages,
        }
        ka = _keep_alive_value(s.ollama_keep_alive)
        if ka is not None:
            payload["keep_alive"] = ka
        r = httpx.post(native, json=payload, timeout=300)
        r.raise_for_status()
        text = r.json().get("message", {}).get("content", "") or ""
    else:
        kwargs = {"response_format": {"type": "json_object"}}
        if s.llm_disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        resp = get_client().chat.completions.create(
            model=mdl, temperature=0.0, max_tokens=mt, messages=messages, **kwargs)
        text = resp.choices[0].message.content or ""
    return _extract_json(text)


def answer(question: str, chunks: list[dict], state_summary: str | None = None,
           strict: bool = False) -> str:
    if not chunks:
        return NOT_FOUND
    context = build_context(chunks)
    system = SYSTEM_PROMPT + (STRICT_SUFFIX if strict else "")
    user = (f"Context chunks:\n\n{context}\n\nQuestion: {question}\n\n"
            "Answer using only the chunks above, with inline citations.")
    return _complete_answer(system, user, state_summary)
