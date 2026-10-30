"""Central configuration. Everything comes from .env — nothing hardcoded.

The 32B/14B model choice, all paths, and all URLs are one-line .env changes.
"""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Postgres ---
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "audit"
    postgres_user: str = "audit"
    postgres_password: str = "audit"
    database_url: str | None = None  # full DSN override

    # --- Qdrant ---
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "paras_v1"
    # Host-side dev without Docker: set an embedded local path instead of a URL.
    # If set, it wins over qdrant_url (same client API, so switching back to the
    # Docker Qdrant is a one-line .env change).
    qdrant_path: str | None = None

    # --- Models (offline) ---
    hf_hub_offline: str = "1"
    transformers_offline: str = "1"
    models_dir: str = "/models"
    bge_m3_path: str = "/models/bge-m3"
    reranker_path: str = "/models/bge-reranker-v2-m3"
    embed_device: str = "cpu"

    # --- Answer LLM (Qwen via vLLM, OpenAI-compatible) ---
    # llm_api: "openai" (vLLM/prod, OpenAI-compatible) or "ollama" (local dev;
    # uses Ollama's native /api/chat so thinking can be reliably disabled).
    llm_api: str = "openai"
    openai_base_url: str = "http://localhost:8000/v1"
    openai_api_key: str = "EMPTY"
    model_name: str = "Qwen3-32B-Instruct"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    # Router / intent classifier — latency-critical, runs every turn. Defaults
    # to the answer model; set to the smaller (8B) model in prod so routing is
    # cheap while answers use the 32B. One-line .env change (per model policy).
    router_model_name: str | None = None
    router_max_tokens: int = 384
    # Ollama keep_alive (dev only). "0" unloads the model after each call so the
    # BGE embedder+reranker fit alongside it on a small-RAM host. None = server
    # default (~5 min). Irrelevant for the OpenAI/vLLM transport.
    ollama_keep_alive: str | None = None
    # Qwen3 is a reasoning model; disable its <think> pass for grounded answers
    # (faster + no reasoning leakage). Harmless for non-Qwen models.
    llm_disable_thinking: bool = True

    # --- Retrieval knobs ---
    retrieve_top_k: int = 20
    rerank_top_k: int = 5
    # Relevance gate: after reranking, drop chunks scoring below
    # rerank_keep_ratio * top_score (always keep at least the top chunk). Stops
    # low-relevance chunks from distracting the answer model.
    rerank_keep_ratio: float = 0.3
    # Skip the cross-encoder reranker and use hybrid RRF order directly. Memory/
    # latency escape hatch for the constrained dev host (the rerank path is
    # validated separately in the Phase-3 eval). Off by default.
    disable_rerank: bool = False
    chunk_tokens: int = 800
    chunk_overlap: int = 100
    # Tables larger than this (chars, after whitespace compaction) are full-page
    # layout tables that duplicate the narrative — kept in the para record but
    # NOT embedded as their own chunks (they only add retrieval noise).
    table_chunk_max_chars: int = 3500

    # --- API surface (Phase 6A contract) ---
    api_v1_prefix: str = "/api/v1"
    api_title: str = "NTPC CAG Audit-Para Chatbot API"
    api_version: str = "1.0.0"
    # Comma-separated API keys checked against the X-API-Key header (placeholder
    # auth until Phase 7 IdP). Empty => auth disabled (dev only).
    api_keys: str = ""
    # Comma-separated allowed CORS origins. NEVER "*" (credentials + contract).
    cors_origins: str = "http://localhost:3000"
    session_ttl_hours: int = 24

    # --- Ingestion target ---
    data_dir: str = "/data"
    excel_index: str = "/data/Corporate office Para - Sample for database.xlsx"
    target_pdf: str = "/data/Corporate office IR 2019-20.pdf"
    target_period: str = "2019-20"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def apply_offline_env(self) -> None:
        """Force HF/Transformers offline before any model import touches the net."""
        os.environ.setdefault("HF_HUB_OFFLINE", self.hf_hub_offline)
        os.environ.setdefault("TRANSFORMERS_OFFLINE", self.transformers_offline)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.apply_offline_env()
    return s
