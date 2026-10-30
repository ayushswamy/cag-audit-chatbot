# Backend Runbook — running the API locally

Everything runs from `d:\CAG Chatbot`. The stack is: **Postgres + Qdrant**
(Docker), the **LLM** (Ollama `qwen3:8b` locally, or a remote vLLM/Qwen3-32B),
the **BGE** embedder/reranker (local files in `./models`), and the **FastAPI**
app (`uvicorn`).

The corpus is already ingested (50 paras in Postgres, 180 chunks in Qdrant) — you
do **not** need to re-ingest to run the API.

---

## Quick start (if Docker + Ollama are already running)

```powershell
cd "d:\CAG Chatbot"
$env:OLLAMA_KEEP_ALIVE = "0"     # unload the LLM between calls so BGE + qwen fit in 16 GB
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Leave that window open — it's the server. Use `127.0.0.1` instead of `0.0.0.0`
to restrict to this machine only.

- Health: <http://localhost:8080/api/v1/health>
- Interactive API docs (Swagger): <http://localhost:8080/docs>

---

## From a cold boot (after a restart)

1. **Start Docker Desktop** (launch the app; wait ~1–2 min for the whale icon to
   settle), then bring up the datastores:
   ```powershell
   cd "d:\CAG Chatbot"
   docker compose up -d postgres qdrant
   ```
2. **Ollama** runs as a Windows service and starts automatically. Confirm the model:
   ```powershell
   ollama list        # should list qwen3:8b
   ```
3. **Start the API** — the Quick-start block above.

---

## Verify

```powershell
# all three dependencies should report "ok"
curl.exe -s http://localhost:8080/api/v1/health

# create a session and ask (streamed). Body-from-file avoids PowerShell quoting issues:
'{"message":"What did audit observe about the Tapovan HRT package?"}' | Out-File -Encoding ascii -NoNewline "$env:TEMP\b.json"
$sid = (curl.exe -s -X POST http://localhost:8080/api/v1/sessions -H "X-API-Key: dev-key-123" | ConvertFrom-Json).session_id
curl.exe -sN -X POST "http://localhost:8080/api/v1/sessions/$sid/ask" `
  -H "X-API-Key: dev-key-123" -H "Content-Type: application/json" -d "@$env:TEMP\b.json"
```

The first answer is slow on CPU (~1–3 min) — expected. `intent` and `retrieval`
SSE events arrive in seconds; answer `token` events follow.

---

## Stop

- API: `Ctrl+C` in the uvicorn window.
- Containers (optional): `docker compose stop`. Data persists in the
  `pgdata` / `qdrantdata` volumes; `docker compose down -v` would delete it.

---

## Good to know

- **Auth:** every `/api/v1` call needs `-H "X-API-Key: dev-key-123"` (except
  `/api/v1/health`). Keys are in `.env` → `API_KEYS` (comma-separated).
- **All config is in `.env`.** To use the GPU server later (no code change):
  ```
  LLM_API=openai
  OPENAI_BASE_URL=http://<gpu-ip>:8000/v1
  MODEL_NAME=Qwen/Qwen3-32B
  ```
- **Re-ingest only if the PDFs/Excel change:**
  ```powershell
  .\.venv\Scripts\python.exe -m ingest --store --embed --recreate
  ```
- **Health troubleshooting** (`/api/v1/health` shows a dep `down`):
  - `postgres` / `qdrant` down → `docker compose up -d postgres qdrant`
  - `llm` down → Ollama not running, or model missing → `ollama pull qwen3:8b`
- **Memory (16 GB box):** keep `OLLAMA_KEEP_ALIVE=0` and avoid other heavy apps —
  BGE embedder+reranker (~5 GB) + qwen3:8b (~6 GB) + Docker is tight.
- **CORS:** browser clients must be served from an origin listed in `.env` →
  `CORS_ORIGINS` (never `*`). Add the frontend dev URL there.

---

## Re-ingesting corrected data on the offline server (10.0.8.107)

Use this when the **ingestion improves** (e.g. a segmenter fix) and the corpus
must be refreshed on the server — **without** re-parsing PDFs. The fixed
`ingest_out\cache\*.json` produced on the dev box carry the corrected
segmentation; the server just re-stores + re-embeds from them.

> **Embedded Qdrant is single-process.** The server uses `QDRANT_PATH`
> (`C:\CAG Chatbot\qdrant_local`), which the running API holds open. You **must
> stop the API** before ingesting, or Qdrant fails with *"already accessed by
> another instance"*.

1. **Copy from dev → server** (overwrite): the 8 fixed `ingest_out\cache\*.json`
   → `C:\CAG Chatbot\ingest_out\cache\`, and any changed `ingest\*.py`
   (e.g. `segmenter.py`) → `C:\CAG Chatbot\ingest\`.

2. **Stop the API + free the port** (elevated PowerShell):
   ```powershell
   Stop-ScheduledTask -TaskName CAGChatbotAPI
   Start-Sleep 2
   Get-NetTCPConnection -LocalPort 8471 -State Listen -ErrorAction SilentlyContinue |
     ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
   ```

3. **Rebuild the vector store cleanly.** Delete the embedded store first so
   `--recreate` starts truly empty — otherwise a stale/incomplete wipe can leave
   old chunks behind (symptom: point count higher than `embedded_chunks`):
   ```powershell
   Remove-Item -Recurse -Force "C:\CAG Chatbot\qdrant_local"
   cd "C:\CAG Chatbot"; $env:PYTHONUTF8="1"
   .\.venv\Scripts\python.exe -m ingest --store --embed --recreate
   ```
   Expect `period_cached` for every period (NOT Docling "Loading weights" per PDF
   — that would mean it re-parsed), then `stored_paras 50` and `embedded_chunks 189`.
   The `QdrantClient.__del__ ... sys.meta_path is None` line at the end is a
   harmless shutdown warning; the exact point count is the real check.

4. **Restart + verify:**
   ```powershell
   Start-ScheduledTask -TaskName CAGChatbotAPI
   Start-Sleep 15
   curl.exe http://127.0.0.1:8471/api/v1/health   # qdrant should read "189 points"
   ```
   Health must show `postgres 50 paras` and `qdrant … 189 points`. If the count
   is not 189, repeat step 3 (the embedded store didn't wipe cleanly).

5. **Spot-check content** (no DB password needed — reads Postgres via the API):
   ```powershell
   $sid = (curl.exe -s -X POST "http://127.0.0.1:8471/api/v1/sessions" -H "X-API-Key: <key>" | ConvertFrom-Json).session_id
   Set-Content b.json '{ "message": "para 2" }' -Encoding ascii
   curl.exe -N -X POST "http://127.0.0.1:8471/api/v1/sessions/$sid/ask" -H "Content-Type: application/json" -H "X-API-Key: <key>" -d "@b.json"
   ```
   Or directly (DB password is in `.env` → `POSTGRES_PASSWORD`):
   ```powershell
   $env:PGPASSWORD = ((Get-Content "C:\CAG Chatbot\.env" | Where-Object { $_ -match '^POSTGRES_PASSWORD=' }) -replace '^POSTGRES_PASSWORD=','').Trim()
   & "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U audit -d audit -h localhost -c "SELECT right(reply_text,80) FROM paras WHERE para_key='Corporate Office|2019-20|IIB|2';"
   Remove-Item Env:PGPASSWORD
   ```

**Server note:** on 10.0.8.107 the API runs as scheduled task `CAGChatbotAPI`
(uvicorn on `127.0.0.1:8471 --root-path /cagchat`), fronted by IIS at
`http://itstaging.ntpc.co.in/cagchat`. There is no `Restart-ScheduledTask` on
that Windows build — use `Stop-` then `Start-ScheduledTask`.

---

## Full stack via Docker (alternative to host uvicorn)

Once the app image is built (`docker compose build app`), you can run the API in
a container instead of host uvicorn:

```powershell
docker compose up -d          # postgres + qdrant + app
```

The `app` service reads the same `.env`. Note the LLM is still external (Ollama on
the host, or the remote vLLM server) — it is intentionally not part of compose.
See `docs/frontend-handoff.md` for the API contract the frontend integrates against.
