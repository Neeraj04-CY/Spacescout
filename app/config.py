"""Environment-based configuration. No secrets live in code."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    # Any OpenAI-compatible chat-completions endpoint works (Groq, OpenAI, local vLLM...).
    llm_api_key: str | None = Field(default=None, validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-20b"
    llm_timeout_s: float = 10.0
    llm_reasoning_effort: str | None = "low"  # sent only to models that accept it (gpt-oss)
    llm_strict_schema: bool = True  # json_schema strict mode; falls back to json_object if rejected
    explain_with_llm: bool = True  # LLM rewrites template explanations, output is grounding-checked

    app_timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"
    max_results: int = 10

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
