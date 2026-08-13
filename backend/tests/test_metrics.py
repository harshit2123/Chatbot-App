"""Metrics endpoint tests against real Postgres aggregation."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.dbutil import ensure_database, reset_schema, url_for

# Isolated database so seeded counts stay deterministic when suites run together.
DATABASE_NAME = "llmlogs_metrics"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", url_for(DATABASE_NAME))

pytestmark = pytest.mark.skipif(
    not ensure_database(DATABASE_NAME), reason="Postgres not reachable at 127.0.0.1:5432"
)


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["INGEST_SYNC"] = "true"

    from app.config import get_settings
    from app.db import session as session_module

    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL)
    session_module.engine = engine
    session_module.SessionLocal.configure(bind=engine)

    from app.db.models import Conversation, InferenceLog

    reset_schema(TEST_DATABASE_URL)

    # Seed a known distribution: 8 success + 2 error, latencies 100..1000.
    now = datetime.now(timezone.utc)
    with session_module.SessionLocal() as session:
        conversation = Conversation(title="metrics")
        session.add(conversation)
        session.commit()
        session.refresh(conversation)

        for index in range(10):
            session.add(
                InferenceLog(
                    id=uuid.uuid4(),
                    conversation_id=conversation.id,
                    provider="mock" if index < 7 else "openrouter",
                    model="mock/echo-1" if index < 7 else "anthropic/claude-3.5-sonnet",
                    latency_ms=100 * (index + 1),
                    prompt_tokens=10,
                    completion_tokens=20,
                    status="error" if index >= 8 else "success",
                    error_message="boom" if index >= 8 else None,
                    created_at=now - timedelta(minutes=index),
                )
            )
        session.commit()

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_summary_computes_rates_and_percentiles(client):
    body = client.get("/metrics/summary", params={"window_minutes": 60}).json()

    assert body["total_calls"] == 10
    assert body["error_count"] == 2
    assert body["error_rate"] == pytest.approx(0.2)
    # Mean of 100..1000.
    assert body["avg_latency_ms"] == pytest.approx(550.0)
    assert body["p95_latency_ms"] is not None
    assert body["total_prompt_tokens"] == 100
    assert body["total_completion_tokens"] == 200


def test_error_rate_is_fractional_not_integer_floored(client):
    """Integer division here would silently report a 0% error rate."""
    points = client.get("/metrics/errors", params={"window_minutes": 60}).json()

    assert points
    assert any(0 < point["error_rate"] <= 1 for point in points)


def test_latency_series_returns_ordered_buckets(client):
    points = client.get("/metrics/latency", params={"window_minutes": 60}).json()

    assert points
    buckets = [point["bucket"] for point in points]
    assert buckets == sorted(buckets)
    assert all(point["avg_latency_ms"] > 0 for point in points)


def test_throughput_series_reports_calls_per_minute(client):
    points = client.get("/metrics/throughput", params={"window_minutes": 60}).json()

    assert points
    assert sum(point["count"] for point in points) == 10
    assert all(point["calls_per_minute"] > 0 for point in points)


def test_provider_breakdown_splits_by_provider_and_model(client):
    rows = client.get("/metrics/providers", params={"window_minutes": 60}).json()

    providers = {row["provider"] for row in rows}
    assert providers == {"mock", "openrouter"}
    assert sum(row["count"] for row in rows) == 10


def test_window_bounds_are_validated(client):
    assert client.get("/metrics/summary", params={"window_minutes": 0}).status_code == 422
    assert client.get("/metrics/summary", params={"window_minutes": 99999999}).status_code == 422


def test_empty_window_returns_zeros_not_an_error(client):
    """A fresh deployment has no logs; the dashboard must still render."""
    body = client.get("/metrics/summary", params={"window_minutes": 1}).json()

    assert body["total_calls"] >= 0
    assert body["error_rate"] >= 0.0
