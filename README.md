# Internal Audit Para Chatbot — NTPC CAG Inspection Reports

RAG chatbot over CAG Inspection Report (IR) PDFs. See `CLAUDE.md` for the full
project charter, data rules, and stack.

> ### ⚠️ Confidential data is NOT included in this repository
> This repo contains **code only**. The CAG Inspection Report PDFs are internal
> audit material, so they — and everything derived from them — are deliberately
> excluded via `.gitignore` and are **not** part of this repository:
>
> - `data/` — the source IR PDFs + the Excel para index
> - `ingest_out/`, `structured_paras.json`, `ingest_diff_report.txt` — extracted para text
> - `eval/*.jsonl`, `tests/*.json` — evaluation/test sets built from the reports
> - `.env` (secrets/API keys), `models/` (BGE + LLM weights), `qdrant_local/` (vector store)
>
> **To run it, you supply your own:**
> 1. Your IR PDFs + Excel index in `data/` (filenames must contain the report period, e.g. `... 2019-20 ...pdf`).
> 2. A `.env` — copy `.env.example` and fill in your paths, DB password, API key, and model endpoint.
> 3. Model weights under `models/` — BGE-M3, BGE-reranker-v2-m3 (downloaded offline), and an LLM via Ollama/vLLM.
>
> Then run ingestion (`python -m ingest --store --embed`) and start the API. See
> [`docs/RUNBOOK.md`](docs/RUNBOOK.md) and [`docs/DEPLOY-OFFLINE.md`](docs/DEPLOY-OFFLINE.md).

---

## Phase 6A — REST API contract (frontend surface)

The public API the frontend (React / MS Teams) integrates against. **This is a
contract** — breaking changes require `/api/v2`. Full spec + examples:
[`docs/frontend-handoff.md`](docs/frontend-handoff.md); import
[`postman_collection.json`](postman_collection.json).

All routes under `/api/v1` (`app/api/*`):

| Method | Path | Purpose |
|---|---|---|
| POST | `/sessions` | create a session → `{session_id}` |
| POST | `/sessions/{id}/ask` | **SSE stream** (intent → retrieval → token* → suggestions → done) |
| GET | `/sessions/{id}/history` | full turn history |
| POST | `/sessions/{id}/reset` | clear context |
| GET | `/paras?period=&para_type=&q=&page=` | paginated listing |
| GET | `/paras/{para_key}` | full structured para |
| GET | `/paras/{para_key}/pdf?pages=` | stream source PDF (optionally the para's pages) |
| GET | `/health` | per-dependency status (postgres, qdrant, llm) |

- **Auth:** `X-API-Key` header checked against `API_KEYS` (placeholder until
  Phase 7 IdP — but required now so the frontend builds with auth from day one).
- **CORS:** only `CORS_ORIGINS` (never `*`).
- **Error envelope** (all non-SSE): `{"error": {code, message, request_id}}`;
  Pydantic validation → `422`, never a stack trace. Every response carries
  `X-Request-ID`.
- **OpenAPI:** `/docs` (Swagger UI) + `/openapi.json`.

```bash
# runnable end-to-end journey with only curl:
BASE=http://localhost:8080 KEY=dev-key-123 bash scripts/demo_journey.sh
# contract tests (shapes for every endpoint; SSE shape via monkeypatch — no LLM):
pytest tests/test_api_contract.py -q
```

---

## Phase 5 — answer quality

**1. Enforced citations** (`app/citations.py`). After generation, the answer is
split into paragraphs; each content paragraph must contain a
`[para_key | section | p.X]` marker that resolves to a **retrieved** chunk.
Unsupported paragraphs trigger **one** regeneration with a stricter prompt; if
any remain, a **low-confidence warning** is prepended. Structural lines (headers,
table rows, the "not found" sentinel) are exempt.

**2. Structured answer modes** (`app/answer_modes.py`), selected deterministically:

| Mode | When | How |
|---|---|---|
| `qa` | default | grounded Q&A |
| `summary` | SUMMARY intent / "summarize" | Observation gist · Reply gist · Current status, each cited |
| `full_para` | resolved exact lookup / "full para" | both sides reproduced **verbatim** (deterministic, tag per paragraph) |
| `table` | numeric question + para has tables | answered from stored **table JSON**, not prose |

**3. Suggestion chips** (`app/suggestions.py`) — rule-based on state, **no LLM
call**. They change with context: after opening a para →
`["Summarize this para", "Show the full para", "Show management reply", "Show
audit observations", "Show the tables", …]`; on disambiguation → one chip per
candidate; on aggregation → drill-down chips; cold start → starter queries.

**4. Faithfulness eval** (`eval/faithfulness.jsonl` = 30 QA + `run_faithfulness.py`):
generates the real answer, then an **LLM-as-judge** decides whether every claim
is supported by the cited chunks. Reports faithfulness % and total uncited
paragraphs. Resumable + endpoint-parameterized (8B local vs 32B server).

```bash
python eval/run_faithfulness.py                 # all 30 (slow on CPU; resumable)
python eval/run_faithfulness.py --limit 5       # quick subset
python eval/run_faithfulness.py --base-url http://gpu:8000/v1 --model Qwen/Qwen3-32B --llm-api openai
```

> **Model note.** The exit thresholds (faithfulness ≥ 95%, 0 uncited) are
> properties of the system with the **32B** answer+judge model. On the local
> **8B** dev stand-in a 10-question subset scored 60% — but 3 of the 4 misses are
> the *8B judge* wrongly rejecting a correct, cited figure (e.g. "Rs. 1578.84
> crore", the para's own title), and only 1 is a genuinely weak 8B *answer*
> (correctly caught by enforcement → 1 uncited paragraph + low-confidence
> warning). The enforcement / modes / suggestions machinery is verified; certify
> the thresholds by re-running against the 32B (harness is endpoint-parameterized).

`/ask` now returns `answer_mode`, `suggestions[]`, and an `enforcement`
`{unsupported_paragraphs, regenerated, low_confidence}` block.

---

## Phase 4 — multi-turn conversation

`POST /ask` is now session-aware. Pass a `session_id` (omit it on the first turn;
the response returns one to reuse). State lives in the Postgres `sessions` table:
`{active_para_key, active_filters, last_route, last_intent, history[last 10],
timestamps}`.

Each turn runs **ONE structured 8B call** (`app/turn.py`) that merges intent
detection, topic-change detection, and query rewriting:

```
{intent: NEW_SEARCH|FOLLOW_UP|SUMMARY|OPEN_PDF|COMPARE|AGGREGATION|CHITCHAT,
 topic_changed: bool, standalone_query: str, filters: {...}, compare_entities: []}
```

- **FOLLOW_UP** produces a fully self-contained `standalone_query` (pronouns
  resolved from the active para) and scopes retrieval to that para. E.g. active =
  Tapovan para → *"Who approved it?"* → *"Who approved the special advances to
  M/s HCC in Para 2, Part IIB, IR 2019-20?"*.
- **topic_changed=true** clears `active_para_key`/`active_filters` — automatic
  topic switch, no exit button (`app/session.reset_context`).
- **COMPARE** retrieves for each entity separately and answers from the union
  with per-source citations.
- The answer prompt now also receives a 2-line conversation-state summary.

Dispatch reuses the Phase-3 routed retriever for new searches; a bare para
number with no report period is forced to **AMBIGUOUS → disambiguation** (never a
silent guess).

### Ingestion fix surfaced by the eval (two-column boundary)

The dialogue eval revealed a Phase-2 segmentation defect: because the two
columns flow independently, a long **Reply** could continue past the *next*
para's Observation heading and get mis-assigned. Fix in `ingest/segmenter.py`: a
block starting **lowercase** is a mid-sentence continuation and inherits the
previous same-column block's para (a new para's text always starts capitalized).
Re-parsing corrected **21 of 50 paras**. `scripts/migrate_resegment.py` re-parses
while preserving the expensive LLM reconstruction for unchanged paras (changed
paras fall back to the corrected geometric text, which is correct for retrieval).
After the fix, "Tapovan HRT" correctly retrieves `2019-20|IIB|2` (previously the
mislabeled `IIB|3`).

### Dialogue eval

`eval/dialogues.jsonl` (15 scripted multi-turn dialogues) + `eval/run_dialogue_eval.py`
assert intent, topic-reset, query rewriting, and retrieved/active para. Endpoint
is a parameter (32B-vs-8B), same as the Phase-3 harness.

```bash
python eval/run_dialogue_eval.py            # reranker on; keep_alive=0 fits RAM here
```

**Exit-criteria dialogue passes:** *"Show me the Mandakini-B para"* → retrieves
`2020-23|IIA|1`; *"why was the BG forfeited?"* → FOLLOW_UP answers from the same
para; *"now show the Loharinagpala para"* → **topic reset**, retrieves
`2011-13|IIB|6`.

---

## Phase 3 — routed retriever

`POST /ask` no longer runs a single naive search. Every question first hits a
**router** (one structured-output call to the small model, temperature 0), then
takes one of four paths:

| Route | Handler | Behaviour |
|---|---|---|
| **EXACT_LOOKUP** | `app/exact.py` → Postgres | Returns the one matching para. If a bare para number matches several reports, returns **all candidates for disambiguation** — never guesses. |
| **SEMANTIC** | `app/routed_retrieval.py` → Qdrant | Hybrid dense+sparse (RRF) with **router filters applied as Qdrant payload filters**, rerank top-20 → top-5, relevance gate. |
| **AGGREGATION** | `app/aggregate.py` → Postgres | Counts/listings via a **whitelist of parameterized SQL templates** with slots. The LLM never emits SQL. |
| **AMBIGUOUS** | `app/exact.py` | Bare para reference → disambiguation payload listing every match. |

Router output is strict JSON `{route, filters:{period?, para_type?, para_no?,
project?}, standalone_query}`, backstopped by deterministic regex extraction of
period/para-number so the small router stays reliable. The router model is
separately configurable (`ROUTER_MODEL_NAME`) so routing can run on the cheap 8B
while answers use the 32B.

To filter semantically, the Qdrant payload now carries `period`, `para_type`,
`para_no`, `project` (re-embed with `python -m ingest --embed --recreate`).

### Eval harness

`eval/golden.jsonl` (15 seeded examples covering exact, thematic, numeric-table,
aggregation, ambiguous) + `eval/run_eval.py` print **route accuracy** and
**recall@5** with a per-category breakdown. The endpoint is a parameter so the
same golden set compares models (per the model policy):

```bash
python eval/run_eval.py                                    # uses .env
python eval/run_eval.py --base-url http://gpu:8000/v1 --model Qwen/Qwen3-32B \
                        --router-model Qwen/Qwen3-8B --llm-api openai
```

**Result on the 15 seeded (qwen3:8b router):** route accuracy **15/15 (100%)**,
recall@5 **12/12 (100%)** — exit criterion is recall@5 ≥ 90%. `"para 1"` →
AMBIGUOUS with 7 disambiguation candidates (never a silent guess).

---

## Phase 2 — production ingestion for the whole corpus

`python -m ingest` ingests **all 8 IR PDFs** into structured, auditable para
records with Excel↔PDF reconciliation.

### Run

```bash
# 1) reconcile + parse everything (no DB/LLM); writes ingest_out/ingestion_report.md
python -m ingest --dry-run

# 2) persist to Postgres + embed into Qdrant (uses the parse cache)
docker compose up -d postgres qdrant
python -m ingest --store --embed --recreate

# 3) LLM reassembly pass (resumable; uses the configured model)
python -m ingest --reconstruct --store --embed --recreate

# spot-check column-split quality straight from Postgres
python scripts/spot_check.py 5
```

### What it does

- **Layout** (`ingest/layout.py`): Docling parse; every text block (table cells
  + free text) assigned to the Observation or Reply column by **x-coordinate
  clustering** per page (widest central gap, falling back to the page midpoint).
  Tables (≥3 cols) kept as markdown **and** JSON rows. OCR fallback available
  (`--ocr`), unused for these digital IRs.
- **Headings** (`ingest/headings.py`): one detector for every observed CAG
  format — `Para No. 3, Part IIB` · `Part IIB, Para No 1` · `IR 2017-18 Part IIB
  Para 1:` · `Para 02:` · sub-paras `Para 04A/4B/4c` · `Para no. – 7`. It
  searches the block's leading portion, validates the prefix is heading-junk
  (rejecting cross-references like "Para 10 above"), finds the Part
  independently, normalises zero-padding, and **splits headings merged
  mid-block**. Part (IIA/IIB) is resolved by a state machine seeded from the
  Excel Part order (numbering restarts between IIA and IIB).
- **Repair** (`ingest/textrepair.py`): drop-caps (`"1. T he"`→`"The"`,
  `"3. N TPC"`→`"NTPC"`), hyphenation, whitespace; monetary values verbatim.
- **Reconstruction** (`ingest/reconstruct.py`): after the geometric split, each
  para's raw column blocks go to the configured LLM to reassemble clean
  observation/reply text — **reorder + join only, never paraphrase**. Raw blocks
  AND reconstructed text are both stored, so it is fully auditable. Resumable.
- **Validation** (`ingest/validate.py`): every Excel row must resolve to a PDF
  para and vice-versa; composite-key uniqueness; per-para quality score; low
  scores → `ingest_out/manual_review.csv`.
- **CLI/pipeline**: idempotent, resumable (per-period parse cache), structured
  JSON logs with a run id, final `ingestion_report.md`.

### Result (this corpus)

- **50 paras** parsed from 8 PDFs → Postgres; **188 chunks** → Qdrant.
- **47 / 48 Excel rows reconciled (98%)**, **0 composite-key duplicates**.
- The one exception is a **reviewed exception**: 2023-24 para IIB-4 is split in
  the PDF into sub-paras **4A/4B/4c** (ingested as first-class paras; the single
  Excel row "IIB-4" therefore shows as an exception, by design).
- Filename/period mismatch handled explicitly: PDF `2024-25` ↔ Excel period
  `2023-24`.
- Spot-check of 5 random paras: clean, correctly separated Observation/Reply.

Outputs: `ingest_out/ingestion_report.md`, `ingest_out/manual_review.csv`,
`ingest_out/cache/<period>.json` (auditable per-para records + raw blocks),
`ingest_out/logs/ingest_<runid>.jsonl`.

---

## Phase 1 — minimal end-to-end pipeline for ONE PDF

Phase 1 ingests exactly one report — **Corporate Office IR 2019-20**
(`data/Corporate office IR 2019-20.pdf`, 7 IIB paras) — and answers questions
over it with grounded, cited responses.

Scope (deliberately): **no** auth, caching, intent detection, or conversation
memory. Only the semantic retrieval path is wired.

### What was built

| Component | File |
|---|---|
| Config (all via `.env`) | `app/config.py` |
| Postgres schema (`paras`, `chunks`) | `app/db.py` |
| BGE-M3 embedder + BGE-reranker-v2-m3 (offline) | `app/models_embed.py` |
| Qdrant hybrid store (dense+sparse, RRF) | `app/qdrant_store.py` |
| Retrieval: embed → hybrid top-20 → rerank top-5 | `app/retrieval.py` |
| Answer LLM (Qwen, OpenAI-compatible, grounding prompt) | `app/llm.py` |
| `POST /ask` endpoint | `app/main.py` |
| Ingestion (Docling column-aware parse → structured paras → chunk → embed) | `scripts/ingest_one.py` |
| Model download (build-time, online) | `scripts/download_models.py` |
| Smoke runner + questions | `scripts/run_smoke.py`, `tests/smoke_questions.json` |
| Retrieval-only verifier (no LLM needed) | `scripts/verify_retrieval.py` |
| Postgres + Qdrant + app | `docker-compose.yml`, `Dockerfile` |

### How ingestion handles the two-column layout

The IR pages are **landscape**, and Docling captures the
`Audit Observation | Management Reply` layout **as tables**. Docling's per-page
column *labels* are inconsistent, so we classify **every table cell into the
Observation or Reply column by its x-coordinate** (page midpoint ≈ 421pt) — the
column-aware approach `CLAUDE.md` mandates. Paras are segmented by the inline
`"Para N, Part IIB ..."` heading cells; broken drop-caps (`"1. T he"` → `"The"`,
`"3. N TPC"` → `"NTPC"`) are normalized; financial tables (Docling tables with
≥3 columns) are preserved as **atomic** markdown + JSON, never split across
chunks. Monetary values (Rs./crore/lakh/₹) are preserved verbatim.

Ingestion writes a **side-by-side diff report** (`ingest_diff_report.txt`,
column-aware vs naive interleaved) and **reconciles** PDF paras against the
Excel index, reporting every mismatch. Re-running is idempotent (composite
`para_key = project|period|para_type|para_no`; deterministic Qdrant point ids).

> **Data note:** the Excel index has **no page-range column** (contrary to the
> original brief), so paras are located inside the PDF by their heading/number,
> not by Excel-provided page ranges.

### Run it (Docker — the intended path)

```bash
cp .env.example .env                     # adjust OPENAI_BASE_URL / MODEL_NAME
python scripts/download_models.py        # one-time, online; -> ./models
docker compose up -d postgres qdrant
docker compose build app
docker compose run --rm app python scripts/ingest_one.py --recreate
docker compose up -d app
python scripts/run_smoke.py --url http://localhost:8080
```

### Verify

- **Parse quality:** open `ingest_diff_report.txt` — each para shows the
  column-aware Observation and Reply next to the naive interleaved text.
- **Retrieval (no LLM needed):** `python scripts/verify_retrieval.py` — the
  Tapovan question surfaces para `...|IIB|2`, the Noida question `...|IIB|1`
  (verified 4/4 top-1).
- **Exit criteria:** `python scripts/verify_ask.py` drives the real `POST /ask`
  handler for the two required questions.

#### Exit-criteria results (verified, dev model `qwen3:8b`)

- *"What did audit observe about the Tapovan HRT package?"* → correct answer
  (HCC contract Rs. 634.04 crore, 34-month term, 18-month extension, Special
  Advances of Rs. 8 crore + Rs. 45 crore), cited
  `[Corporate Office|2019-20|IIB|2 | observation | p.3-6]`.
- *"What was NTPC's reply on the Noida land parcels?"* → correct answer (plots
  B6&B7 / A3&A4, Rs. 21.11 cr + Rs. 33.45 cr, valuation Rs. 475.2 / 950.4 cr,
  DIPAM monetization + withdrawal letters), cited
  `[Corporate Office|2019-20|IIB|1 | reply | p.1-3]`.

Answer quality controls in the pipeline: a rerank **relevance gate** drops
low-score chunks before the LLM; table markdown is **whitespace-compacted** and
oversized full-page tables are kept in the para record but **not embedded** (they
duplicate the narrative and only add retrieval noise); the grounding prompt is
section-aware (observation vs reply) and requires copy-exact citation tags.

> **Note on running this on a 16 GB, no-GPU PC:** Docker (Postgres/Qdrant VM) +
> the BGE models + an 8B LLM together exceed 16 GB. So for the live `/ask` demo
> on this machine, use **embedded Qdrant** (`QDRANT_PATH` set) + **Ollama**
> (`LLM_API=ollama`) with Docker stopped. The Docker `postgres + qdrant` stack
> is verified separately for storage. On the GPU server, everything runs via
> `docker compose` + vLLM as designed.

### Configuration (`.env`)

Everything is `.env`-driven (see `.env.example`). The 32B/14B answer-model
choice is a one-line change (`MODEL_NAME`). Models load **offline** from
`MODELS_DIR` with `HF_HUB_OFFLINE=1` — no runtime calls to Hugging Face.

### Answer LLM — local dev vs server

The answer model is external and 100% `.env`-driven, so the model is a one-line
swap:

| Environment | `OPENAI_BASE_URL` | `MODEL_NAME` | Notes |
|---|---|---|---|
| **This PC (dev)** | `http://localhost:11434/v1` | `qwen3:8b` | Small Qwen via **Ollama** (CPU). This box has no GPU / 16 GB RAM, so the 32B model cannot run here — a small Qwen validates the `/ask` grounding + citation path. |
| **GPU server (prod)** | `http://<gpu-server-ip>:8000/v1` | `Qwen/Qwen3-32B` | Qwen3-32B via **vLLM** (must match `--served-model-name`). |

Qwen3 is a reasoning model; `LLM_DISABLE_THINKING=true` appends `/no_think` and
any `<think>…</think>` block is stripped from the answer defensively.

Local dev prerequisite: `ollama pull qwen3:8b` (Ollama must be running).

### Host-only (no-Docker) dev mode

For development without Docker, `.env` can point `QDRANT_PATH` at an embedded
local Qdrant and use host paths for models/data. In this mode ingestion runs and
embeds fully; if Postgres is not running, structured records are still written
to `structured_paras.json` and the DB step is reported as deferred (never
silently skipped).

### Known Phase-1 limitations (by design / accepted)

- Column separation is good but not perfect. Para IIB-3's observation is thin
  (most of its narrative sits in the reply column at a page boundary) — flagged
  for review; not an exit-criteria para.
- Financial sub-tables inside a para are preserved atomically as markdown, but
  Docling occasionally splits digits (`"13194.6 9"`); acceptable for Phase 1.
- OCR fallback (RapidOCR) is available but unused — this IR is digital text.
