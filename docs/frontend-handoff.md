# CAG Chatbot API — Frontend Handoff

**Base URL:** `https://itstaging.ntpc.co.in/cagchat` · **Swagger (try it live):**
`https://itstaging.ntpc.co.in/cagchat/docs`

---

## What is a "para"? (domain 101)

The source documents are **CAG Inspection Reports** (audits of NTPC by the
Comptroller & Auditor General). Each report is split into numbered **audit
paragraphs — "paras"**. **A para = one audit finding.** It's the basic unit of
content: the chatbot answers *about* paras, and users can also browse them.

Every para has two sides + metadata:
- **Observation** — what the auditor found (e.g. *"Undue advantage to contractor
  M/s HCC Ltd. … Rs. 634.04 crore"*).
- **Management Reply** — NTPC's response.
- sometimes **Tables** (financial figures), and always:
- **Metadata:** report period (e.g. `2019-20`), part (`para_type` = `IIA`/`IIB`),
  number (`para_no`), page range, and source PDF.
- **`para_key`** — the unique id, `project|period|para_type|para_no`
  (e.g. `Corporate Office|2019-20|IIB|2`). You always **read it from a response**
  (list / ask citations / disambiguation); never build it by hand.

### How the `GET /paras/{para_key}` fields map to a screen
| Field | Show as |
|---|---|
| `title` | the para heading |
| `period`, `para_type`, `para_no` | a label: "IR 2019-20 · Part IIB · Para 2" |
| `observation` | an **"Audit Observation"** section |
| `reply` | a **"Management Reply"** section |
| `tables[]` | render below — each has `markdown` (easy) and `rows` (real table) |
| `pages`, `source_pdf` | a **"View source PDF"** button → the `/pdf` endpoint |

## Suggested screens (how to use paras in the UI)

Build both:

**1) Chat-driven (main experience)**
- Chat box → stream the answer from `/ask`.
- The answer carries **citations** (para_keys — in the `retrieval` event and inline
  like `[Corporate Office|2019-20|IIB|2 | observation | p.3-7]`). Render each as a
  **clickable chip** → open that para's detail (`GET /paras/{para_key}`).
- Render the `suggestions` chips as buttons; clicking one sends it as the next message.

**2) Browse-driven ("Reports / Paras" section)**
- **List page:** `GET /paras` → cards/table with filters (`period`, `para_type`) and
  a search box (`q=`); it's paginated.
- **Detail page:** click a row → `GET /paras/{para_key}` → observation + reply +
  tables + an **"Open PDF"** button (`…/{para_key}/pdf?pages=true` = just that
  para's pages).

**First build order:** chat with streaming → citation + suggestion chips → click
citation opens the para detail panel → a separate "Browse paras" tab. The list
endpoint is where you get `para_key`s; detail + PDF render one para.

---

## Auth (required from day one)

Every `/api/v1` call **except `/api/v1/health`** requires:

```
X-API-Key: <one of API_KEYS>
```

Dev key: `dev-key-123` (set your own in `.env` → `API_KEYS`). Missing → `401 missing_api_key`; wrong → `401 invalid_api_key`.
Full IdP auth arrives in Phase 7 **without changing this header**, so send it now.

Every response carries `X-Request-ID` (echo it in bug reports). You may also
*send* `X-Request-ID` to correlate.

---

## Error envelope (all non-SSE responses)

```json
{ "error": { "code": "not_found", "message": "…", "request_id": "4e3e47…" } }
```

| HTTP | code | when |
|------|------|------|
| 401 | `missing_api_key` / `invalid_api_key` | auth |
| 404 | `session_not_found` / `para_not_found` / `pdf_not_found` | unknown id |
| 422 | `validation_error` | bad/missing input (never a stack trace) |
| 500 | `internal_error` | unexpected; message is generic |
| 502 | `upstream_error` | LLM/dependency failure |

---

## Endpoints

### 1. `POST /api/v1/sessions` — create a session
Response `201`:
```json
{ "session_id": "e8c8d4f4be434ea9abc9a56b0a15af32", "created_at": "2026-07-14T11:06:28+00:00" }
```

### 2. `POST /api/v1/sessions/{id}/ask` — ask (SSE stream)
Request body: `{ "message": "What did audit observe about the Tapovan HRT package?" }`
Response: `200 text/event-stream` — see **SSE flow** below. `404 session_not_found`
if the session doesn't exist (create one first).

### 3. `GET /api/v1/sessions/{id}/history` — full turn history
```json
{ "session_id": "e8c8…", "active_para_key": "Corporate Office|2019-20|IIB|2",
  "active_filters": {"period": "2019-20"}, "last_intent": "FOLLOW_UP",
  "turns": [ { "user": "…", "assistant": "…", "intent": "NEW_SEARCH",
               "para_key": "Corporate Office|2019-20|IIB|2" } ] }
```

### 4. `POST /api/v1/sessions/{id}/reset` — clear context (keep session id)
```json
{ "session_id": "e8c8…", "reset": true, "active_para_key": null }
```

### 5. `GET /api/v1/paras` — list / search (paginated)
Query: `period`, `para_type` (`IIA|IIB`), `q` (free text), `page` (≥1),
`page_size` (1–100, default 20).
```json
{ "items": [ { "para_key": "Corporate Office|2019-20|IIB|2", "project": "Corporate Office",
    "period": "2019-20", "para_type": "IIB", "para_no": "2",
    "title": "Undue advantage to the contractor M/s HCC Ltd. for HRT package in Tapovan Vishnugad HEPP",
    "page_start": 3, "page_end": 7, "source_pdf": "Corporate office IR 2019-20.pdf",
    "needs_review": false } ],
  "page": 1, "page_size": 20, "total": 7, "total_pages": 1 }
```

### 6. `GET /api/v1/paras/{para_key}` — full structured para
**`para_key` must be URL-encoded** (it contains spaces and `|`):
`Corporate Office|2019-20|IIB|2` → `Corporate%20Office%7C2019-20%7CIIB%7C2`
```json
{ "para_key": "Corporate Office|2019-20|IIB|2", "project": "Corporate Office",
  "period": "2019-20", "para_type": "IIB", "para_no": "2",
  "title": "Undue advantage to the contractor M/s HCC Ltd. for HRT package in Tapovan Vishnugad HEPP",
  "observation": "Undue advantage to the contractor M/s HCC Ltd. for HRT package in Tapovan Vishnugad HEPP The contract awarded for construction of Head Race Tunnel (HRT) … Rs. 634.04 crore …",
  "reply": "… NTPC provided special advance-1 of ₹ 8.0Cr … special advance-2 of ₹ 45 crore …",
  "tables": [], "pages": { "start": 3, "end": 7 },
  "source_pdf": "Corporate office IR 2019-20.pdf",
  "column_confidence": "high", "needs_review": false }
```

### 7. `GET /api/v1/paras/{para_key}/pdf` — stream source PDF
Query `pages=true` → only this para's page range (`3–7`); default → whole PDF.
Returns `application/pdf` (`Content-Disposition: inline`).

### 8. `GET /api/v1/health` — service + dependency health (no auth)
```json
{ "status": "ok", "version": "1.0.0",
  "dependencies": [
    { "name": "postgres", "status": "ok", "latency_ms": 12.3, "detail": "50 paras" },
    { "name": "qdrant",   "status": "ok", "latency_ms": 8.1,  "detail": "collection 'paras_v1', 180 points" },
    { "name": "llm",      "status": "ok", "latency_ms": 42.0, "detail": "Qwen/Qwen3-32B reachable" } ] }
```
`status` is `degraded` if any dependency is `down` (still HTTP 200 — inspect the array).

---

## SSE flow (`/ask`)

Events arrive in this order. Render progressively.

| event | data | notes |
|-------|------|-------|
| `intent` | `{intent, topic_changed, standalone_query}` | first; show the resolved query |
| `retrieval` | `{route, citations:[{para_key, section, pages}]}` | sources being used |
| `disambiguation` | `{candidates:[…], message}` | **instead of tokens** — see gotcha |
| `token` | `{text}` | repeated; concatenate. Chunks of the final, citation-checked answer |
| `suggestions` | `{chips:[…]}` | render as buttons |
| `done` | `{answer_id, latency_ms, kind, intent, route, answer_mode, active_para_key}` | last; close |
| `error` | `{code, message}` | on failure, then stream closes |

### Worked transcript — Tapovan follow-up
Request: `POST /api/v1/sessions/{id}/ask  {"message":"What did audit observe about the Tapovan HRT package?"}`
```
event: intent
data: {"intent":"NEW_SEARCH","topic_changed":false,"standalone_query":"What did audit observe about the Tapovan HRT package?"}

event: retrieval
data: {"route":"SEMANTIC","citations":[{"para_key":"Corporate Office|2019-20|IIB|2","section":"observation","pages":"p.3-7"},{"para_key":"Corporate Office|2019-20|IIB|2","section":"reply","pages":"p.3-7"}]}

event: token
data: {"text":"The contract for construction of the Head Race Tunnel (HRT) for Tapov"}

event: token
data: {"text":"an Vishnugad HEPP was awarded to M/s HCC for Rs. 634.04 crore [Corpor"}

event: token
data: {"text":"ate Office|2019-20|IIB|2 | observation | p.3-7]."}

event: suggestions
data: {"chips":["Summarize this para","Show the full para (both sides)","Show management reply","Show audit observations","Any similar paras?","Download source PDF"]}

event: done
data: {"answer_id":"df55651624f34b7c9d1a46227c98e933","latency_ms":34120,"kind":"answer","intent":"NEW_SEARCH","route":"SEMANTIC","answer_mode":"qa","active_para_key":"Corporate Office|2019-20|IIB|2"}
```

---

## Gotchas (read these)

- **Disambiguation instead of tokens.** A bare para number with no report period
  (e.g. `"para 1"`) matches multiple reports. `/ask` then emits **`event:
  disambiguation`** with a `candidates[]` list (each `{para_key, period,
  para_type, para_no, title}`) and **no `token` events**. Render the candidates as
  choices; the user's next turn (e.g. "the 2019-20 one" or "para 1 of 2019-20")
  resolves it. Do **not** expect an answer in this case.
  ```
  event: disambiguation
  data: {"candidates":[{"para_key":"Corporate Office|2017-18|IIB|1","period":"2017-18","para_type":"IIB","para_no":"1","title":"…"}, …],"message":"That para number appears in multiple reports — pick which report you mean."}
  ```
- **`para_key` encoding.** Always `encodeURIComponent(paraKey)` for paths 6 & 7.
- **Session expiry.** Sessions live in Postgres and don't hard-expire in v1, but
  treat them as ephemeral (TTL target 24h). If a session id 404s, create a new one.
- **Streaming timeouts.** Answers run on a CPU dev box today (a token burst can be
  30s–3min to first `token`; `intent`/`retrieval` arrive in a few seconds). Set the
  client read timeout to **≥ 300s** and rely on `event: done` to close — do not
  time out on silence between `retrieval` and the first `token`. On the GPU server
  this drops to seconds.
- **`status: degraded`** on `/health` is still HTTP 200 — check the `dependencies`
  array, don't rely on the HTTP code alone.
- **CORS.** Only origins in `CORS_ORIGINS` are allowed (never `*`). Send your dev
  origin to the backend team to be added.

---

## curl (every endpoint)

```bash
BASE=http://localhost:8080; KEY=dev-key-123

# health (no auth)
curl -s $BASE/api/v1/health

# create session
SID=$(curl -s -X POST $BASE/api/v1/sessions -H "X-API-Key: $KEY" | jq -r .session_id)

# ask (streamed) — -N disables curl buffering
curl -sN -X POST $BASE/api/v1/sessions/$SID/ask -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' \
     -d '{"message":"What did audit observe about the Tapovan HRT package?"}'

# follow-up (same session keeps context)
curl -sN -X POST $BASE/api/v1/sessions/$SID/ask -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' -d '{"message":"Who was the contractor?"}'

# history / reset
curl -s $BASE/api/v1/sessions/$SID/history -H "X-API-Key: $KEY"
curl -s -X POST $BASE/api/v1/sessions/$SID/reset -H "X-API-Key: $KEY"

# list / detail / pdf
curl -s "$BASE/api/v1/paras?period=2019-20&para_type=IIB&page=1&page_size=20" -H "X-API-Key: $KEY"
curl -s "$BASE/api/v1/paras/Corporate%20Office%7C2019-20%7CIIB%7C2" -H "X-API-Key: $KEY"
curl -s "$BASE/api/v1/paras/Corporate%20Office%7C2019-20%7CIIB%7C2/pdf?pages=true" -H "X-API-Key: $KEY" -o para.pdf
```

## Postman
Import `postman_collection.json` (repo root). It has all 8 endpoints with the
`{{baseUrl}}`, `{{apiKey}}`, `{{sessionId}}`, `{{paraKey}}` variables pre-wired;
"Create session" saves `sessionId` automatically.
