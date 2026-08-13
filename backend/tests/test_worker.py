"""Worker task tests.

The task runs out of band, so a failure here surfaces as "logs silently stopped
appearing" rather than a visible error. Tests call the task function directly
rather than through a broker.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/llmlogs_test"
)


def _postgres_available() -> bool:
    try:
        engine = create_engine(TEST_DATABASE_URL)
        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_available(), reason=f"Postgres not reachable at {TEST_DATABASE_URL}"
)


@pytest.fixture(scope="module")
def session_factory():
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

    from app.db.models import Base

    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(bind=engine)

    # Point the task's module-level session factory at the test database.
    import worker.tasks as tasks_module

    tasks_module._engine = engine
    tasks_module._Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    return tasks_module._Session


@pytest.fixture
def conversation_id(session_factory) -> uuid.UUID:
    from app.db.models import Conversation

    with session_factory() as session:
        conversation = Conversation(title="worker test")
        session.add(conversation)
        session.commit()
        return conversation.id


def _payload(conversation_id: uuid.UUID, **overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "conversation_id": str(conversation_id),
        "message_id": None,
        "provider": "mock",
        "model": "mock/echo-1",
        "latency_ms": 42,
        "prompt_tokens": 5,
        "completion_tokens": 9,
        "status": "success",
        "error_message": None,
        "input_preview": "user: hello",
        "output_preview": "hi there",
        "started_at": None,
        "completed_at": None,
    }
    return {**base, **overrides}


def _fetch(session_factory, log_id: str):
    from app.db.models import InferenceLog

    with session_factory() as session:
        return session.scalars(
            select(InferenceLog).where(InferenceLog.id == uuid.UUID(log_id))
        ).one_or_none()


def test_task_is_bound_to_the_configured_broker():
    """Regression: @shared_task bound to Celery's default app instead of ours.

    Importing worker.tasks alone left the task pointing at localhost, so every
    enqueue from the API failed with "Connection refused" and silently fell
    back to a synchronous write — the queue was never actually used.
    """
    from worker.tasks import process_log_event

    broker = process_log_event.app.conf.broker_url
    assert broker, "task is attached to Celery's default app, not the configured one"
    assert "redis" in broker


def test_task_persists_a_valid_event(session_factory, conversation_id):
    from worker.tasks import process_log_event

    payload = _payload(conversation_id)
    assert process_log_event(payload) == "ok"

    row = _fetch(session_factory, payload["id"])
    assert row is not None
    assert row.provider == "mock"
    assert row.latency_ms == 42
    assert row.status == "success"


def test_task_redacts_pii_before_persisting(session_factory, conversation_id):
    """Nothing unredacted may reach durable storage."""
    from worker.tasks import process_log_event

    payload = _payload(
        conversation_id,
        input_preview="user: my email is harshit@example.com and card 4111 1111 1111 1111",
        output_preview="I will contact harshit@example.com",
    )
    process_log_event(payload)

    row = _fetch(session_factory, payload["id"])
    assert "harshit@example.com" not in row.input_preview
    assert "4111" not in row.input_preview
    assert "[REDACTED_EMAIL]" in row.input_preview
    assert "[REDACTED_CARD]" in row.input_preview
    assert "harshit@example.com" not in row.output_preview


def test_malformed_payload_is_dropped_not_retried(session_factory):
    """An invalid payload never becomes valid, so retrying it would loop forever."""
    from worker.tasks import process_log_event

    assert process_log_event({"provider": "mock"}) == "dropped:invalid"


def test_redelivered_task_does_not_double_insert(session_factory, conversation_id):
    """acks_late means redelivery is expected; the write must be idempotent."""
    from app.db.models import InferenceLog
    from worker.tasks import process_log_event

    payload = _payload(conversation_id)

    assert process_log_event(payload) == "ok"
    assert process_log_event(payload) == "ok"

    with session_factory() as session:
        count = len(
            session.scalars(
                select(InferenceLog).where(InferenceLog.id == uuid.UUID(payload["id"]))
            ).all()
        )
    assert count == 1


def test_error_status_events_are_persisted(session_factory, conversation_id):
    from worker.tasks import process_log_event

    payload = _payload(
        conversation_id,
        status="error",
        error_message="OpenRouter returned 401",
        output_preview=None,
    )
    process_log_event(payload)

    row = _fetch(session_factory, payload["id"])
    assert row.status == "error"
    assert "401" in row.error_message


def test_cancelled_status_events_are_persisted(session_factory, conversation_id):
    from worker.tasks import process_log_event

    payload = _payload(conversation_id, status="cancelled", output_preview="partial out")
    process_log_event(payload)

    row = _fetch(session_factory, payload["id"])
    assert row.status == "cancelled"
    assert row.output_preview == "partial out"
