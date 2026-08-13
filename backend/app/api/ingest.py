"""Log ingestion endpoint.

Phase 2: the endpoint validates the payload and enqueues a Celery task instead
of writing to Postgres inline. If the database is slow or briefly down, this
endpoint still returns fast and the event waits in the broker — that decoupling
is the entire point of the async-processing design.

`INGEST_SYNC=true` falls back to a direct write, which keeps the system usable
(and testable) without a running broker.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import InferenceLog
from app.db.session import get_db
from app.models.schemas import IngestAccepted, InferenceLogIn, InferenceLogOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 500


def _write_directly(payload: InferenceLogIn, db: Session) -> None:
    """Synchronous fallback: same redaction and idempotency as the worker."""
    from worker.pii import redact

    values = payload.model_dump()
    values["input_preview"] = redact(values.get("input_preview"))
    values["output_preview"] = redact(values.get("output_preview"))

    stmt = insert(InferenceLog).values(**values).on_conflict_do_nothing(index_elements=["id"])
    db.execute(stmt)
    db.commit()


@router.post("/ingest", response_model=IngestAccepted, status_code=202)
def ingest_log(
    payload: InferenceLogIn,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> IngestAccepted:
    """Validate and hand off. 202 means accepted for processing, not persisted."""
    if settings.ingest_sync:
        _write_directly(payload, db)
        return IngestAccepted(id=payload.id, queued=False)

    from worker.tasks import process_log_event

    try:
        process_log_event.delay(payload.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - broker down must not lose the event
        # Degrade to a direct write rather than dropping observability data.
        logger.warning("Broker unavailable, writing log %s directly: %s", payload.id, exc)
        _write_directly(payload, db)
        return IngestAccepted(id=payload.id, queued=False)

    return IngestAccepted(id=payload.id, queued=True)


@router.get("/logs", response_model=list[InferenceLogOut])
def list_logs(
    conversation_id: uuid.UUID | None = None,
    limit: int = Query(default=DEFAULT_LOG_LIMIT, ge=1, le=MAX_LOG_LIMIT),
    db: Session = Depends(get_db),
) -> list[InferenceLog]:
    """Read back stored logs. Powers the UI log panel."""
    stmt = select(InferenceLog).order_by(InferenceLog.created_at.desc()).limit(limit)
    if conversation_id is not None:
        stmt = stmt.where(InferenceLog.conversation_id == conversation_id)
    return list(db.scalars(stmt).all())
