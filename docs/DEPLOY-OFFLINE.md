# Offline deployment to the Windows server (10.0.8.107, 32 GB, no GPU)

Target is **fully offline** (no internet/proxy), **Windows**, mirroring this dev
box but **without Docker** (Docker is the hardest thing to install offline on
Windows Server; we don't need it). The stack:

| Component | On the server | Why |
|---|---|---|
| API | Python 3.12 venv + `uvicorn` | same as dev |
| Vectors | **Qdrant embedded** (`QDRANT_PATH`, in-process) | zero install, no Docker |
| Metadata/sessions | **PostgreSQL** native Windows service | needs real Postgres (JSONB, upserts) |
| LLM | **Ollama** native + `qwen3:8b` | same as dev; 32 GB keeps it warm |
| Embed/rerank | BGE files under `models\` | offline, `HF_HUB_OFFLINE=1` |

> 32 GB means BGE (~5 GB) + qwen3:8b (~6 GB) + Postgres + OS all fit warm — set
> `OLLAMA_KEEP_ALIVE=-1` so the model never unloads. No swapping, so it's faster
> than the 16 GB dev box despite no GPU.

---

## Part A — build the transfer bundle (on THIS machine, has internet)

Everything is staged under `D:\cag-offline-bundle\`. Contents:

```
cag-offline-bundle\
  app-src.zip                 # the repo (code, configs, ingest cache)  — built below
  requirements-lock.txt       # exact pinned deps
  wheelhouse\                 # every dependency as a .whl (offline pip)
  models\                     # BGE-M3 + reranker  (~4.3 GB) — copy in
  data\                       # PDFs + Excel
  ollama-models\             # qwen3:8b blobs+manifests (~5.2 GB) — copy in
  installers\
    python-3.12.x-amd64.exe   # from python.org
    postgresql-16-windows-x64.exe
    OllamaSetup.exe
  .env.server                 # ready-to-use offline config (rename to .env)
  DEPLOY-OFFLINE.md           # this file
```

Build steps (this machine):
```powershell
$B = "D:\cag-offline-bundle"
# 1. code + ingest cache (excludes venv/models/data/wheels)  -> app-src.zip
#    (script provided: scripts\build_offline_bundle.ps1)
powershell -File "d:\CAG Chatbot\scripts\build_offline_bundle.ps1"

# 2. wheelhouse (already downloading in the background):
#    pip download -r requirements-lock.txt -d wheelhouse

# 3. copy the big folders in
Copy-Item "d:\CAG Chatbot\models" "$B\models" -Recurse
Copy-Item "d:\CAG Chatbot\data"   "$B\data"   -Recurse
Copy-Item "$env:USERPROFILE\.ollama\models" "$B\ollama-models" -Recurse

# 4. installers -> $B\installers\  (Python 3.12 x64, PostgreSQL 16 x64, OllamaSetup.exe)
```

Then copy the whole `D:\cag-offline-bundle\` to the server over your RDP session
(drive redirection) or a USB/network share.

---

## Part B — install on the server (10.0.8.107, offline)

Assume the bundle is at `C:\cag-offline-bundle\` and the app will live at
`C:\CAG Chatbot\`.

### Automated (recommended): `install_server.ps1`

Install the 3 prerequisites first (or use `-InstallPrereqs`), then run the
one-shot script from the bundle folder in an **elevated** PowerShell:

```powershell
cd C:\cag-offline-bundle
# optional: silently install Python + PostgreSQL + Ollama, then re-open PowerShell
powershell -ExecutionPolicy Bypass -File .\install_server.ps1 -InstallPrereqs -PgSuperPassword "YourPgPass"

# main run: model load + app deploy + offline pip + DB + ingest + firewall
powershell -ExecutionPolicy Bypass -File .\install_server.ps1 `
    -PgSuperPassword "YourPgPass" -StartServer
```

It runs steps 2-8 below automatically (idempotent). Edit `C:\CAG Chatbot\.env`
afterwards to set a real `API_KEYS` and the frontend's `CORS_ORIGINS`. The manual
steps below are the same thing, for reference / troubleshooting.

### 1. Python 3.12
Run `installers\python-3.12.x-amd64.exe` → check **"Add python.exe to PATH"** →
Install. Verify: `python --version` → 3.12.x.

### 2. PostgreSQL 16
Run `installers\postgresql-16-windows-x64.exe`. Set the `postgres` superuser
password (remember it). Default port 5432. After install, create the app DB/user:
```powershell
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE USER audit WITH PASSWORD 'audit';"
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" -U postgres -c "CREATE DATABASE audit OWNER audit;"
```

### 3. Ollama + model (offline)
Run `installers\OllamaSetup.exe`. Then place the model blobs (no download):
```powershell
# copy the bundled ollama store into the server's ollama home
Copy-Item "C:\cag-offline-bundle\ollama-models\*" "$env:USERPROFILE\.ollama\models\" -Recurse -Force
ollama list        # should show qwen3:8b
```

### 4. App code + models + data
```powershell
Expand-Archive "C:\cag-offline-bundle\app-src.zip" "C:\CAG Chatbot"
Copy-Item "C:\cag-offline-bundle\models" "C:\CAG Chatbot\models" -Recurse
Copy-Item "C:\cag-offline-bundle\data"   "C:\CAG Chatbot\data"   -Recurse
Copy-Item "C:\cag-offline-bundle\.env.server" "C:\CAG Chatbot\.env"
```

### 5. Python venv + offline install (no internet)
```powershell
cd "C:\CAG Chatbot"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-index `
    --find-links "C:\cag-offline-bundle\wheelhouse" -r "C:\cag-offline-bundle\requirements-lock.txt"
```

### 6. Load the corpus (from the transferred cache — no PDF re-parse, no LLM)
```powershell
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe -m ingest --store --embed --recreate
# -> "stored_paras 50", "embedded_chunks ~180". Uses ingest_out\cache\ (bundled),
#    so it does NOT re-parse PDFs or call the LLM.
```

### 7. Run the API
```powershell
$env:OLLAMA_KEEP_ALIVE = "-1"       # keep the model warm (plenty of RAM)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 8. Verify
```powershell
curl.exe -s http://localhost:8080/api/v1/health      # postgres/qdrant/llm all "ok"
# from another machine on the intranet:
curl.exe -s http://10.0.8.107:8080/api/v1/health
```
Open a Windows Firewall inbound rule for TCP 8080 so the frontend can reach it:
```powershell
New-NetFirewallRule -DisplayName "CAG Chatbot API 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
```

---

## `.env.server` (offline native config) — key differences from dev

```
POSTGRES_HOST=localhost
POSTGRES_PASSWORD=audit
QDRANT_PATH=C:\CAG Chatbot\qdrant_local     # embedded Qdrant (no server/Docker)
MODELS_DIR=C:\CAG Chatbot\models
BGE_M3_PATH=C:\CAG Chatbot\models\bge-m3
RERANKER_PATH=C:\CAG Chatbot\models\bge-reranker-v2-m3
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
LLM_API=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
MODEL_NAME=qwen3:8b
OLLAMA_KEEP_ALIVE=-1
API_KEYS=<set-a-real-key>
CORS_ORIGINS=http://<frontend-host>
DATA_DIR=C:\CAG Chatbot\data
EXCEL_INDEX=C:\CAG Chatbot\data\Corporate office Para - Sample for database.xlsx
```

---

## Notes / gotchas

- **Single uvicorn worker** with embedded Qdrant (the embedded store is opened by
  one process). For higher concurrency later, run the native `qdrant.exe` binary
  and set `QDRANT_URL=http://localhost:6333` instead of `QDRANT_PATH`.
- **Same Python (3.12) + x64** on the server as the wheelhouse was built against,
  or some wheels won't match.
- **Network-whitelist:** the installer downloads (python.org, EDB PostgreSQL,
  github.com/ollama) were fetched on the internet-connected build machine and are
  logged in `network-whitelist-request.md`. The server itself pulls nothing.
- **Later, when the GPU box exists:** point `.env` at it (`LLM_API=openai`,
  `OPENAI_BASE_URL=http://<gpu>:8000/v1`, `MODEL_NAME=Qwen/Qwen3-32B`) — no other
  change. The 8B stays as a fallback.
```
