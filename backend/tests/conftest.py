"""Shared test configuration.

Suites configure the app through environment variables and a cached settings
object. Without isolation, whichever suite imported first wins and later suites
silently run against the wrong database — which is how a full-suite run ended up
skipping tests that passed individually.
"""

from __future__ import annotations

import os

import pytest

# Environment the suites mutate to configure the app under test.
_MANAGED_VARS = (
    "DATABASE_URL",
    "REDIS_URL",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "INGEST_SYNC",
    "INGEST_URL",
    "OPENROUTER_API_KEY",
)


@pytest.fixture(autouse=True, scope="module")
def isolate_settings():
    """Snapshot and restore managed env vars, clearing the settings cache."""
    from app.config import get_settings

    saved = {name: os.environ.get(name) for name in _MANAGED_VARS}
    get_settings.cache_clear()

    yield

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()
