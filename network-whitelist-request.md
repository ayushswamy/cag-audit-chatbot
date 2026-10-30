# Network Whitelist Requests

Per CLAUDE.md network policy: every failed network operation (pip, npm, docker
pull, git clone, huggingface download, apt, wget/curl) is logged here with ALL
domains in the redirect chain, then alternatives are attempted, then marked
BLOCKED if truly stuck.

| Date | Domain(s) to whitelist | Full URL attempted | Tool | Needed for | Blocking? (yes/no) |
|------|------------------------|--------------------|------|------------|--------------------|
| 2026-07-06 | (none yet — all attempted downloads succeeded) | — | — | Phase 1 pre-flight verification | no |
| 2026-07-06 | www.modelscope.cn | https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.1/torch/PP-OCRv4/rec/ch_PP-OCRv4_rec_mobile.pth | Docling (RapidOCR) | OCR fallback models on first Docling run | no (succeeded; this IR is digital text, OCR not needed) |
| 2026-07-06 | aka.ms, Microsoft Store CDN (for `wsl --install`) | https://aka.ms/wslinstall | wsl.exe | WSL2 backend so Docker Desktop daemon can start | RESOLVED — WSL2 installed, Docker daemon now up (v29.6.1); postgres:16 + qdrant/qdrant images pulled OK from Docker Hub |
| 2026-07-15 | pypi.org, files.pythonhosted.org | pip download -r requirements-lock.txt | pip | OFFLINE server bundle: wheelhouse for 10.0.8.107 (no internet) | no (build-time on dev box; server installs from wheelhouse) |
| 2026-07-15 | www.python.org | https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe | curl | Python 3.12 installer for offline server | no (HTTP 200, downloaded to bundle) |
| 2026-07-15 | ollama.com (+ CDN) | https://ollama.com/download/OllamaSetup.exe | curl | Ollama installer for offline server | no (HTTP 200, downloaded to bundle) |
| 2026-07-15 | get.enterprisedb.com (+ CDN) | https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64.exe | curl | PostgreSQL 16 Windows installer for offline server | no (HTTP 200, downloaded to bundle) |

### Offline server deployment (10.0.8.107, Windows, 32 GB, no GPU, NO internet)
The server has no outbound internet. All artifacts are fetched on this
internet-connected dev box and transferred as a bundle (`D:\cag-offline-bundle\`):
wheelhouse (pip `--no-index`), Python/PostgreSQL/Ollama installers, BGE `models\`,
`data\`, the Ollama `qwen3:8b` blobs, and `app-src.zip` (code + ingest cache).
The server pulls nothing. See `docs/DEPLOY-OFFLINE.md`.

## Pre-flight verification results (2026-07-06)

Reachable from this machine (no whitelist action needed):
- **pip / PyPI** — `pypi.org`, `files.pythonhosted.org`: OK (test download of openpyxl succeeded).
- **Hugging Face** — `huggingface.co`: OK (HEAD 200 for `BAAI/bge-m3` and
  `BAAI/bge-reranker-v2-m3` config.json). Note: LFS weight blobs are served from
  `cdn-lfs.huggingface.co` / `cdn-lfs-us-1.huggingface.co` / `xethub`-backed
  hosts — if the large `.safetensors` downloads fail, log those domains here.

Environment gaps (NOT network/whitelist issues — recorded for visibility):
- **Docker**: Docker Desktop installed (CLI v29.6.1, Compose v5.1.4) but the
  daemon **fails to start: WSL2 is not installed** (`wsl --status` = "not
  installed"). Docker Desktop's default backend is WSL2. Bringing up
  docker-compose is BLOCKED until WSL2 is installed (`wsl --install`, requires
  admin + another restart; downloads a Linux kernel/distro that may hit the
  proxy — see row below).
- **RapidOCR (Docling OCR fallback)** pulled models from `modelscope.cn` at
  first run (see row below).
- **Model weights**: DONE. BGE-M3 (2.3 GB) and BGE-reranker-v2-m3 (2.3 GB)
  downloaded (build-time, allowed by policy) into `./models` from huggingface.co
  and loaded at runtime with `HF_HUB_OFFLINE=1`. No failures.
