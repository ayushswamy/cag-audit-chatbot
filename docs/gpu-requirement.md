# Compute & GPU Requirement — CAG Audit-Para Chatbot

**In one line:** The search side of the system runs comfortably on an ordinary
CPU server. A GPU is needed for **one thing only** — making the AI's *written
answers* fast. Whether a dedicated GPU is required depends on how the tool is
used, and the design lets us switch CPU ↔ GPU with a single config change.

---

## What the system does (two phases per question)

1. **Retrieval (search).** Find the relevant audit paras — embed the question,
   search the vector index, re-rank the best matches. Pure search; no answer is
   written yet.
2. **Generation (the AI answer).** A language model reads the retrieved paras
   and *writes* a grounded, cited answer.

The GPU matters for phase 2 only.

---

## Where the compute actually goes

| Step | What it is | Time on **CPU** | Needs GPU? |
|---|---|---|---|
| Embed the question | BGE-M3 model | ~1–2 sec | No |
| Vector search | Qdrant (math only) | milliseconds | No |
| Re-rank top matches | BGE re-ranker | ~2–4 sec | No |
| **Retrieval total** | | **~5 sec** | **No** |
| Analyse the question | small LLM | ~30 sec | Helps |
| **Write the answer** | **LLM (8B / 32B)** | **~3–8 min** | **Yes — the only real reason** |

**Retrieval is light and CPU is plenty for it.** The GPU accelerates only the
step where the LLM *composes* the answer. On CPU that step takes minutes; on a
GPU it takes seconds.

A further point: **a large share of everyday interactions use no LLM at all** and
are already instant on CPU — browsing/listing a report's paras, opening a
specific para (its full text is returned directly from the database), counts,
"report not indexed" replies, disambiguation, and follow-up selection. Only
**free-form questions and summaries** invoke the LLM.

---

## The three options

| Option | Generated-answer speed | Hardware | Notes |
|---|---|---|---|
| **A. CPU only** (current) | minutes per answer | existing server, no GPU | Fine if usage is mostly looking up and reading paras. Works today. |
| **B. Dedicated GPU server** | seconds per answer | 1 GPU box | Needed for responsive free-form Q&A at scale. |
| **C. Shared / external LLM endpoint** | seconds per answer | none of our own — point at an existing GPU-served model | No dedicated box to buy; reuse an LLM service that already exists. |

Switching between these is a **one-line configuration change** (`.env`) — no
re-engineering. We can run on CPU now and add a GPU (B or C) later without
rebuilding anything.

---

## Recommendation

- If the tool is used mainly to **look up, browse and read** audit paras →
  **CPU is sufficient** for now (Option A).
- If **asking questions and getting written answers** is a core, day-to-day use
  case → a **GPU is needed for acceptable speed** — either a dedicated box
  (Option B) or, cheaper, pointing the app at an **existing shared LLM endpoint**
  (Option C).

**The honest summary:** the GPU is not for search — search is cheap and runs on
CPU. The GPU buys exactly one thing: fast AI-written answers. The decision is
simply whether responsive, free-form answering is a required feature or a
nice-to-have.
