"""SDK configuration.

Deliberately a plain dataclass with its own env-var reading rather than the
host application's settings object. The SDK previously imported
`app.config.Settings`, which meant dropping it into any other project was
impossible — that coupling, more than anything else, is what stopped this
being a library.

Every field has a working default, so `LogConfig()` alone is enough to start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_INGEST_URL = "http://127.0.0.1:8000/ingest"
DEFAULT_SPOOL_DIR = "/tmp/llmlog-spool"
DEFAULT_REPLAY_INTERVAL_SECONDS = 30
DEFAULT_PREVIEW_MAX_CHARS = 500
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_DELIVERY_WORKERS = 4


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class LogConfig:
    """Everything the SDK needs. Immutable so a live client cannot be re-pointed."""

    # Where events are POSTed. HTTP rather than an in-process call on purpose:
    # it is what lets the SDK sit in a different process, or a different machine,
    # from the collector without any code change.
    ingest_url: str = DEFAULT_INGEST_URL

    # Durable on-disk spool. Events are written here before delivery is tried
    # and removed only on confirmed acceptance.
    spool_enabled: bool = True
    spool_dir: str = DEFAULT_SPOOL_DIR
    replay_interval_seconds: int = DEFAULT_REPLAY_INTERVAL_SECONDS

    preview_max_chars: int = DEFAULT_PREVIEW_MAX_CHARS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    delivery_workers: int = DEFAULT_DELIVERY_WORKERS

    @classmethod
    def from_env(cls, prefix: str = "LLMLOG_") -> LogConfig:
        """Build from environment variables, e.g. `LLMLOG_INGEST_URL`."""
        return cls(
            ingest_url=os.getenv(f"{prefix}INGEST_URL", DEFAULT_INGEST_URL),
            spool_enabled=_env_bool(f"{prefix}SPOOL_ENABLED", True),
            spool_dir=os.getenv(f"{prefix}SPOOL_DIR", DEFAULT_SPOOL_DIR),
            replay_interval_seconds=_env_int(
                f"{prefix}REPLAY_INTERVAL_SECONDS", DEFAULT_REPLAY_INTERVAL_SECONDS
            ),
            preview_max_chars=_env_int(
                f"{prefix}PREVIEW_MAX_CHARS", DEFAULT_PREVIEW_MAX_CHARS
            ),
        )
