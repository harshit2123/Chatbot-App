"""Log-processing task.

This is where the actual work moved to in Phase 2: the ingestion endpoint now
only validates and enqueues, and everything below happens off the request path.

Pipeline: validate -> redact PII -> normalize metadata -> persist.
"""

from __future__ import annotations

import logging
import os

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import sessionmaker

from worker.celery_app import celery_app
from worker.pii import redact

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/llmlogs"
)

# The worker owns its own engine: it is a separate process from the API and
# must not share a connection pool across a fork boundary.
_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
_Session = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


# Bound to the configured app explicitly rather than via @shared_task: a
# shared task attaches to whichever Celery app happens to be current, so the
# API (which only imports this module) silently got the default app pointing at
# localhost and every enqueue failed with "Connection refused".
@celery_app.task(name="worker.tasks.process_log_event", bind=True)
def process_log_event(self, payload: dict) -> str:
    """Process one inference log event.

    Returns a short status string, which is what shows up in Flower and in the
    Celery result backend.
    """
    # Imported lazily so the module can be imported without the backend package
    # present (e.g. in a worker-only image).
    from app.db.models import InferenceLog
    from app.models.schemas import InferenceLogIn

    try:
        event = InferenceLogIn.model_validate(payload)
    except ValidationError as exc:
        # A malformed payload will never become valid on retry, so this is a
        # dead end, not a retryable failure. Logged loudly instead of silently
        # dropped. A real DLQ is the production answer.
        logger.error("Dropping malformed log event: %s", exc)
        return "dropped:invalid"

    values = event.model_dump()

    # Redaction happens here, not at the call site, so nothing unredacted is
    # ever written to durable storage.
    values["input_preview"] = redact(values.get("input_preview"))
    values["output_preview"] = redact(values.get("output_preview"))

    try:
        with _Session() as session:
            stmt = (
                insert(InferenceLog)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["id"])
            )
            session.execute(stmt)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - transient DB failures should retry
        logger.warning("DB write failed for log %s, retrying: %s", event.id, exc)
        # acks_late + retry: the task is redelivered, and the insert is
        # idempotent, so a retry cannot double-write.
        raise self.retry(exc=exc, countdown=5) from exc

    return "ok"
