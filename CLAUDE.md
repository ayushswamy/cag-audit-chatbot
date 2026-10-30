# Project: Internal Audit Para Chatbot (NTPC CAG Inspection Reports)

## What we are building
A RAG chatbot over CAG Inspection Report (IR) PDFs. Each IR contains numbered
"audit paras". Each para has a TWO-COLUMN layout: left column = "Audit
Observation", right column = "Management Reply". Some paras also contain
tables (financial figures in Rs. crore), audit queries, and further submissions.

An Excel index maps every para to its PDF and metadata:
columns: Sr No | Type | MAB | Region | Project | Period/Report No |
Addition Year | Para Type (IIA/IIB) | Para No | Para Description

## Critical data facts — never violate these
1. Para numbers REPEAT across reports and parts. The unique key is the
   composite: (project, period, para_type, para_no). Never key on para_no
   alone. When a user references a bare para number without context,
   disambiguate — never guess.
2. Naive pdftotext INTERLEAVES the two columns mid-sentence. All ingestion
   must be column-aware (layout parsing via Docling with x-coordinate
   block classification), never plain text extraction.
3. Section numbering in PDFs renders with broken drop-caps
   ("1. T\nhe management..." = "1. The management..."). Normalize during
   ingestion.
4. Tables (e.g., credit/debit notes per plant per year) must be extracted
   as atomic units, stored as markdown + JSON, never split across chunks.
5. Monetary values use Indian conventions (Rs., crore, lakh). Preserve
   verbatim — never convert or round.
6. The Excel index may have gaps (e.g., missing para numbers). Ingestion
   must reconcile Excel rows against paras actually found in PDFs and
   report every mismatch — never silently skip.

## Architecture principles
- Ingestion is a one-time, expensive, careful pipeline producing STRUCTURED
  para records: {para_key, title, observation_text, reply_text, tables[],
  page_range, source_pdf}. Runtime never parses PDFs.
- Retrieval is a ROUTER with 3 paths:
  (a) exact lookup -> Postgres -> return structured para directly
  (b) semantic     -> Qdrant hybrid (dense + sparse) search, metadata filters
  (c) aggregation  -> SQL over metadata via whitelisted query templates
      ("how many paras in 2020-23") — never free-form LLM-generated SQL
- Every LLM answer must cite (para_key, section, page). Answers without
  retrievable support must say "not found in the indexed reports".
- Conversation follow-ups are rewritten into standalone queries BEFORE
  retrieval ("Who approved it?" -> fully self-contained question).
- Topic change detection resets active para context automatically.

## Stack (v1 — deliberately minimal)
- API: FastAPI
- Metadata + structured paras + sessions + audit log: PostgreSQL
- Vectors: Qdrant (hybrid dense + sparse, RRF fusion)
- Embeddings: BGE-M3 (dense + sparse outputs)
- Reranker: BGE-reranker-v2
- LLM (answers, ingestion reconstruction, eval-judge): Qwen3-32B-Instruct
  served via vLLM, OpenAI-compatible endpoint
- LLM (router / intent / query-rewrite — runs every turn, latency-critical):
  Qwen3-8B, temperature 0, structured JSON output
- Layout parsing: Docling (+ PaddleOCR fallback for scanned pages)
- Containerization: Docker Compose
- NOT in v1: Kubernetes, Keycloak, Redis, MinIO, Prometheus. Do not scaffold
  these. They are added later only if measurements or requirements demand it.

## Model policy
- Model endpoints are configured ONLY via .env (OPENAI_BASE_URL, MODEL_NAME).
  Never hardcode a model name in application code — the 32B/14B decision
  must remain a one-line config change.
- The golden-set eval (Phase 3+) must be runnable against any configured
  model so 32B vs 14B accuracy/latency can be compared with evidence.
- Model weights live at /models on local disk. ALWAYS set HF_HUB_OFFLINE=1
  and load models from local paths. NEVER call huggingface.co at runtime.
  Weights are transferred to this machine offline (downloaded elsewhere,
  copied over) — code must assume no internet access to model hubs.

## Network policy — INTRANET ENVIRONMENT
This machine sits behind a corporate proxy with domain whitelisting.
Many downloads WILL fail. Follow these rules strictly:

1. NEVER silently skip, stub out, or work around a failed download.
   Every failure must be recorded and surfaced.
2. Maintain a file at repo root: network-whitelist-request.md
   On ANY failed network operation (pip, npm, docker pull, git clone,
   huggingface download, apt, wget/curl), append a row:

   | Date | Domain(s) to whitelist | Full URL attempted | Tool | Needed for | Blocking? (yes/no) |

   Include ALL domains involved — e.g. a Hugging Face download needs
   both huggingface.co AND cdn-lfs.huggingface.co; pip needs both
   pypi.org AND files.pythonhosted.org. Check redirect chains and log
   every domain in the chain.
3. After logging, attempt reasonable alternatives IN THIS ORDER before
   declaring the task blocked:
   a. Internal mirror if configured (Nexus/Artifactory/JFrog — check for
      pip.conf, .npmrc, or Docker daemon config hints in the environment
      and ask me for the mirror URL if found)
   b. A different official source for the same artifact
   c. Pin an older version already present in local cache
4. If truly blocked, stop that sub-task, mark it BLOCKED in the phase
   summary, and continue with sub-tasks that don't need it. Never
   fabricate a "mock" version of a dependency to fake progress.
5. At the end of every session, print the current contents of
   network-whitelist-request.md so I can forward it to IT.

### Domains expected to be pre-whitelisted (request submitted to IT)
- pip: pypi.org, files.pythonhosted.org
- Hugging Face (build-time only): huggingface.co, cdn-lfs.huggingface.co,
  cdn-lfs-us-1.huggingface.co
- Docker: hub.docker.com, registry-1.docker.io, auth.docker.io,
  production.cloudflare.docker.com, ghcr.io
- OS packages: archive.ubuntu.com, security.ubuntu.com, deb.debian.org
- Git: github.com, codeload.github.com, raw.githubusercontent.com,
  objects.githubusercontent.com
- npm (frontend): registry.npmjs.org
If a needed domain is not on this list, log it per rule 2 — do not assume.

## Working rules for you (Claude Code)
- Complete only the phase I name. Do not scaffold future phases.
- Every phase ends with runnable code + a test I can execute + a short
  README section describing what was built and how to verify it.
- Do not advance past a phase's exit criteria. If exit criteria cannot be
  met, say so explicitly and list what is blocking.
- Prefer boring, readable code over clever abstractions.
- All config via .env + pydantic-settings. Never hardcode paths, keys,
  model names, or URLs.
- Ingestion must be idempotent and resumable: rerunning never creates
  duplicate paras or duplicate vectors.
- All timestamps and logs in structured JSON with request IDs.
- This tool is FOR an audit department: every user question, retrieved
  para set, and answer must be recorded in an immutable audit log table
  (implemented in its designated phase — but never write code that would
  make this hard to add).
