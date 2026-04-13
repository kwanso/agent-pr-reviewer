from __future__ import annotations

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROFILES: dict[str, dict[str, object]] = {
    "cost_safe": {
        "min_response_length": 70,
        "fallback_retry_min_length": 60,
        "enable_early_exit": True,
        "chunk_delay_ms": 3500,
    },
    "balanced": {},
    "quality_heavy": {
        "min_response_length": 120,
        "fallback_retry_min_length": 100,
        "enable_early_exit": False,
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = 3400
    github_token: str = ""
    github_app_id: int = 0
    github_app_private_key_path: str = ""
    github_app_private_key: str = ""
    slack_bot_token: str = ""
    slack_channel: str = ""
    webhook_secret: str = ""

    # LLM
    llm_api_key: str = ""
    llm_flash_model: str = "gemini-2.5-flash"
    llm_mock_mode: bool = False
    llm_max_output_tokens: int = 8192
    llm_thinking_budget: int = 0
    llm_timeout_s: int = 25
    llm_retry_count: int = 1
    llm_temperature: float = 0.2

    # Review profile
    review_profile: str = "cost_safe"

    # PR-level guards
    max_files: int = 30
    max_diff_size: int = 30000
    chunk_size: int = 5000
    chunk_delay_ms: int = 2000
    min_response_length: int = 100
    fallback_retry_min_length: int = 80
    skip_config_only: bool = False
    compact_diff: bool = True
    compact_context_lines: int = 1

    enable_early_exit: bool = False

    # RAG
    enable_rag: bool = True
    rag_embedding_model: str = "models/gemini-embedding-001"
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 100
    rag_top_k: int = 5

    # SQLite checkpoint
    db_path: str = "./data/checkpoints.db"

    @model_validator(mode="before")
    @classmethod
    def _apply_profile(cls, values: dict) -> dict:
        """Profile defaults sit between base defaults and env overrides.

        Only applied when the env var is NOT explicitly set, avoiding the
        layering bug from the JS version where env overrides always won.
        """
        name = str(
            values.get("review_profile")
            or os.environ.get("REVIEW_PROFILE", "cost_safe")
        ).lower()
        for key, val in PROFILES.get(name, {}).items():
            if key.upper() not in os.environ:
                values.setdefault(key, val)
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
