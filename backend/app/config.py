"""Application configuration loaded from the environment."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/llmlogs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = DEFAULT_DATABASE_URL

    # Provider selection. "mock" needs no credentials and is the default so the
    # stack runs end to end without an API key.
    llm_provider: str = "mock"
    llm_model: str = "mock/echo-1"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"

    # Where the SDK wrapper posts log events. Kept as a URL rather than an
    # in-process call so the wrapper stays transport-identical to how it would
    # behave from a separate service.
    # 127.0.0.1 rather than localhost on purpose: localhost resolves to ::1
    # first on macOS, so an unrelated process bound to IPv6 :8000 would silently
    # receive these log events instead of this app.
    ingest_url: str = "http://127.0.0.1:8000/ingest"

    # Broker/result backend for the log-processing queue, and the store for
    # per-request cancellation flags.
    redis_url: str = "redis://localhost:6379/0"

    # Bypass the queue and write logs inline. Useful for running the API
    # standalone and for tests that assert on persisted rows.
    ingest_sync: bool = False

    # Conversational context budget: how many past messages get replayed to the
    # model on each turn.
    history_turn_limit: int = 20

    # How long a cancellation flag lives. Longer than any plausible generation,
    # short enough that stale keys expire on their own.
    cancel_flag_ttl_seconds: int = 600

    preview_max_chars: int = 500
    # Both spellings of the dev origin: localhost and 127.0.0.1 are distinct
    # origins to the browser, and the Vite dev server is reachable as either.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
