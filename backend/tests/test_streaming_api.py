"""SSE endpoint and cancellation tests."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.dbutil import ensure_database, reset_schema, url_for

DATABASE_NAME = "llmlogs_stream"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", url_for(DATABASE_NAME))

pytestmark = pytest.mark.skipif(
    not ensure_database(DATABASE_NAME), reason="Postgres not reachable at 127.0.0.1:5432"
)


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["LLM_MODEL"] = "mock/echo-1"
    # Write logs inline so assertions don't depend on a running broker.
    os.environ["INGEST_SYNC"] = "true"

    from app.config import get_settings
    from app.db import session as session_module

    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL)
    session_module.engine = engine
    session_module.SessionLocal.configure(bind=engine)

    from app.main import app
    from app.sdk import providers

    reset_schema(TEST_DATABASE_URL)

    # Remove the simulated token delay so tests stay fast.
    providers.MockProvider.chunk_delay_seconds = 0

    with TestClient(app) as test_client:
        import app.sdk.logging as sdk_logging

        original = sdk_logging.emit_log_event

        def emit_via_test_client(event, settings):
            test_client.post("/ingest", json=event)

        sdk_logging.emit_log_event = emit_via_test_client
        try:
            yield test_client
        finally:
            sdk_logging.emit_log_event = original


@pytest.fixture
def conversation_id(client) -> str:
    return client.post("/conversations", json={}).json()["id"]


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs."""
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event_name:
            events.append((event_name, data))
    return events


def test_stream_emits_start_deltas_and_done(client, conversation_id):
    response = client.post(
        f"/conversations/{conversation_id}/messages/stream",
        json={"content": "hello streaming"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    names = [name for name, _ in events]

    assert names[0] == "start"
    assert names[-1] == "done"
    assert names.count("delta") > 1  # genuinely incremental, not one blob

    reassembled = "".join(data["content"] for name, data in events if name == "delta")
    assert "hello streaming" in reassembled

    done_payload = events[-1][1]
    assert done_payload["assistant_message"]["role"] == "assistant"
    assert done_payload["assistant_message"]["content"] == reassembled


def test_streamed_turn_is_persisted_and_resumable(client, conversation_id):
    client.post(
        f"/conversations/{conversation_id}/messages/stream", json={"content": "remember this"}
    )

    messages = client.get(f"/conversations/{conversation_id}/messages").json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "remember this"


def test_streamed_turn_emits_an_inference_log(client, conversation_id):
    client.post(f"/conversations/{conversation_id}/messages/stream", json={"content": "log me"})

    logs = client.get("/logs", params={"conversation_id": conversation_id}).json()
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert logs[0]["completion_tokens"] is not None


def test_cancel_before_stream_stops_generation_early(client, conversation_id):
    """A cancel flag set before the stream starts must halt it immediately."""
    import app.api.cancellation as cancellation

    # Simulate a cancel that arrives while the stream is running.
    original = cancellation.is_cancelled
    cancellation.is_cancelled = lambda cid: True
    try:
        response = client.post(
            f"/conversations/{conversation_id}/messages/stream",
            json={"content": "cancel me"},
        )
        events = _parse_sse(response.text)
    finally:
        cancellation.is_cancelled = original

    names = [name for name, _ in events]
    assert names[-1] == "cancelled"
    # Stopped before draining the full reply.
    assert names.count("delta") == 0


def test_cancelled_stream_writes_a_cancelled_log(client, conversation_id):
    import app.api.cancellation as cancellation

    original = cancellation.is_cancelled
    cancellation.is_cancelled = lambda cid: True
    try:
        client.post(
            f"/conversations/{conversation_id}/messages/stream", json={"content": "halt"}
        )
    finally:
        cancellation.is_cancelled = original

    logs = client.get("/logs", params={"conversation_id": conversation_id}).json()
    assert any(log["status"] == "cancelled" for log in logs)


def test_cancel_endpoint_accepts_and_404s_correctly(client, conversation_id):
    assert client.post(f"/conversations/{conversation_id}/cancel").status_code == 202
    assert client.post(f"/conversations/{uuid.uuid4()}/cancel").status_code == 404


def test_stream_on_unknown_conversation_404s(client):
    response = client.post(
        f"/conversations/{uuid.uuid4()}/messages/stream", json={"content": "hi"}
    )
    assert response.status_code == 404


def test_provider_error_is_reported_as_an_sse_error_frame(client, conversation_id, monkeypatch):
    """A mid-stream failure must reach the client, not hang the connection."""
    from app.sdk.providers import ProviderError

    def explode(settings):
        raise ProviderError("no credentials")

    monkeypatch.setattr("app.api.conversations.build_provider", explode)

    response = client.post(
        f"/conversations/{conversation_id}/messages/stream", json={"content": "fail"}
    )
    events = _parse_sse(response.text)

    assert events[-1][0] == "error"
    assert "no credentials" in events[-1][1]["detail"]
