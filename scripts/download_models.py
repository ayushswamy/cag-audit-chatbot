"""Build-time model download. Pulls BGE-M3 and BGE-reranker-v2-m3 into
MODELS_DIR so the app can run fully offline (HF_HUB_OFFLINE=1) afterwards.

This is the ONE place we deliberately go online, and only at build time.
Per CLAUDE.md network policy: any failure here must be logged to
network-whitelist-request.md (all redirect-chain domains) — do not silently
skip.

Usage:
    python scripts/download_models.py
"""
from __future__ import annotations

import os
import sys

# This script MUST be allowed online — undo any offline flags for this process.
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.pop("TRANSFORMERS_OFFLINE", None)

from huggingface_hub import snapshot_download  # noqa: E402

MODELS_DIR = os.environ.get("MODELS_DIR", "./models")

MODELS = {
    "bge-m3": "BAAI/bge-m3",
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",
}

# Skip optional/duplicate weight formats to save bandwidth; keep safetensors.
IGNORE = ["*.onnx", "onnx/*", "*.h5", "*.ckpt", "*.msgpack", "openvino/*"]


def main() -> int:
    os.makedirs(MODELS_DIR, exist_ok=True)
    rc = 0
    for folder, repo in MODELS.items():
        dest = os.path.join(MODELS_DIR, folder)
        print(f"[download] {repo} -> {dest}")
        try:
            snapshot_download(
                repo_id=repo,
                local_dir=dest,
                ignore_patterns=IGNORE,
            )
            print(f"[ok] {repo}")
        except Exception as e:  # noqa: BLE001
            rc = 1
            print(f"[FAIL] {repo}: {type(e).__name__}: {e}", file=sys.stderr)
            print(
                "  -> Log this to network-whitelist-request.md including "
                "huggingface.co, cdn-lfs.huggingface.co, cdn-lfs-us-1.huggingface.co "
                "and any xet/redirect hosts, then retry.",
                file=sys.stderr,
            )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
