"""API tests against a real Postgres.

These run against the compose Postgres rather than SQLite because the ingest
endpoint relies on Postgres-specific ON CONFLICT and UUID types — testing on a
different engine would not exercise the code that actually ships.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from tests.dbutil import ensure_database, url_for

DATABASE_NAME = "llmlogs_test"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", url_for(DATABASE_NAME))

# Skip only when Postgres itself is unreachable — never merely because the
# database has not been created yet.
pytestmark = pytest.mark.skipif(
    not ensure_database(DATABASE_NAME), reason="Postgres not reachable at 127.0.0.1:5432"
)


@pytest.fixture(scope="module")
def client():
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["LLM_PROVIDER"] = "mock"
    os.environ["LLM_MODEL"] = "mock/echo-1"
    # Write logs inline so assertions about persisted rows don't depend on a
    # running broker and worker. The queue path is covered in test_worker.py.
    os.environ["INGEST_SYNC"] = "true"

    # Imported after env is set so settings/engine pick up the test database.
    from app.config import get_settings
    from app.db import session as session_module

    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL)
    session_module.engine = engine
    session_module.SessionLocal.configure(bind=engine)

    from app.db.models import Base
    from app.main import app

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with TestClient(app) as test_client:
        # Route the wrapper's log delivery into this test app instead of over
        # real HTTP to a dev server on port 8000, which would write to a
        # different database and fail the conversation foreign key.
        import app.sdk.logging as sdk_logging

        original_emit = sdk_logging.emit_log_event

        def emit_via_test_client(event, settings):
            response = test_client.post("/ingest", json=event)
            assert response.status_code == 202, response.text

        sdk_logging.emit_log_event = emit_via_test_client
        try:
            yield test_client
        finally:
            sdk_logging.emit_log_event = original_emit


@pytest.fixture
def conversation_id(client) -> str:
    response = client.post("/conversations", json={})
    assert response.status_code == 201
    return response.json()["id"]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_conversation_is_titled_from_first_message(client, conversation_id):
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "Plan my week"})

    listed = client.get("/conversations").json()
    match = next(c for c in listed if c["id"] == conversation_id)
    assert match["title"] == "Plan my week"


def test_multi_turn_history_is_persisted_in_order(client, conversation_id):
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "first"})
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "second"})

    messages = client.get(f"/conversations/{conversation_id}/messages").json()

    # Resume depends on this exact ordering.
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "first"
    assert messages[2]["content"] == "second"


def test_chat_turn_emits_an_inference_log(client, conversation_id):
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "hello"})

    logs = client.get("/logs", params={"conversation_id": conversation_id}).json()
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert logs[0]["provider"] == "mock"
    assert logs[0]["message_id"] is not None


def test_rename_conversation(client, conversation_id):
    response = client.patch(f"/conversations/{conversation_id}", json={"title": "  Renamed  "})

    assert response.status_code == 200
    # Whitespace is trimmed so the sidebar never shows padded titles.
    assert response.json()["title"] == "Renamed"

    listed = client.get("/conversations").json()
    assert next(c for c in listed if c["id"] == conversation_id)["title"] == "Renamed"


def test_rename_rejects_empty_title(client, conversation_id):
    """A rename to nothing is a delete; it must not silently blank the title."""
    assert client.patch(f"/conversations/{conversation_id}", json={"title": ""}).status_code == 422


def test_rename_survives_a_new_message(client, conversation_id):
    """Auto-titling must not overwrite a title the user chose."""
    client.patch(f"/conversations/{conversation_id}", json={"title": "My title"})
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "hello"})

    listed = client.get("/conversations").json()
    assert next(c for c in listed if c["id"] == conversation_id)["title"] == "My title"


def test_delete_removes_conversation_and_messages(client, conversation_id):
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "hello"})

    assert client.delete(f"/conversations/{conversation_id}").status_code == 204

    assert client.get(f"/conversations/{conversation_id}/messages").status_code == 404
    assert all(c["id"] != conversation_id for c in client.get("/conversations").json())


def test_delete_keeps_inference_logs(client, conversation_id):
    """Telemetry outlives the chat: deleting a conversation must not rewrite history.

    Latency, error rate, and spend are operational metrics. Letting a sidebar
    cleanup silently change them would make the dashboard untrustworthy.
    """
    client.post(f"/conversations/{conversation_id}/messages", json={"content": "hello"})
    before = len(client.get("/logs", params={"limit": 500}).json())
    assert before > 0

    client.delete(f"/conversations/{conversation_id}")

    after = client.get("/logs", params={"limit": 500}).json()
    assert len(after) == before
    # The log survives, detached from the deleted conversation.
    assert any(log["conversation_id"] is None for log in after)


def test_delete_and_rename_404_on_unknown_conversation(client):
    missing = uuid.uuid4()
    assert client.delete(f"/conversations/{missing}").status_code == 404
    assert client.patch(f"/conversations/{missing}", json={"title": "x"}).status_code == 404


def test_unknown_conversation_returns_404(client):
    missing = uuid.uuid4()
    assert client.get(f"/conversations/{missing}/messages").status_code == 404
    assert (
        client.post(f"/conversations/{missing}/messages", json={"content": "hi"}).status_code == 404
    )


def test_empty_message_is_rejected(client, conversation_id):
    response = client.post(f"/conversations/{conversation_id}/messages", json={"content": ""})
    assert response.status_code == 422


def test_ingest_rejects_malformed_payload(client):
    response = client.post("/ingest", json={"provider": "mock"})
    assert response.status_code == 422


def test_ingest_rejects_invalid_status_value(client, conversation_id):
    response = client.post(
        "/ingest",
        json={
            "id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "provider": "mock",
            "model": "m",
            "status": "kind-of-worked",
        },
    )
    assert response.status_code == 422


def test_ingest_is_idempotent_on_redelivery(client, conversation_id):
    log_id = str(uuid.uuid4())
    payload = {
        "id": log_id,
        "conversation_id": conversation_id,
        "provider": "mock",
        "model": "mock/echo-1",
        "latency_ms": 12,
        "status": "success",
        "input_preview": "hi",
        "output_preview": "hello",
    }

    first = client.post("/ingest", json=payload)
    second = client.post("/ingest", json=payload)

    # A redelivered task must not 500 or double-insert.
    assert first.status_code == 202
    assert second.status_code == 202

    logs = client.get("/logs", params={"conversation_id": conversation_id}).json()
    assert len([log for log in logs if log["id"] == log_id]) == 1
