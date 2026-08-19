"""Log ingestion endpoint.

Validate -> redact PII -> persist. One path, synchronously.

There used to be a Celery queue behind this endpoint, on the reasoning that a
slow database must not stall ingestion. That reasoning still holds — but the
SDK's on-disk spool already provides it, and provides it *earlier* in the chain:
the producer never blocks on this endpoint at all, and an event survives even a
total collector outage, which a broker sitting behind the collector cannot help
with. The queue was a second durability mechanism guarding the same gap, with
its own broker to run, its own failure mode, and a circular import between the
API and the worker package. Removing it loses nothing and deletes a service.

If write throughput ever becomes the real bottleneck — as opposed to the
hypothetical one — the honest fix is batching inserts here, not reintroducing a
broker.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import InferenceLog
from app.db.session import get_db
from app.models.schemas import IngestAccepted, InferenceLogIn, InferenceLogOut
from app.telemetry.pii import redact

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])

DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 500


@router.post("/ingest", response_model=IngestAccepted, status_code=202)
def ingest_log(
    payload: InferenceLogIn,
    db: Session = Depends(get_db),
) -> IngestAccepted:
    """Accept one inference log event.

    A failure here is deliberately *not* swallowed: the producer's spool retries
    on anything but a terminal 4xx, so surfacing the error is what keeps the
    event alive. Returning 202 on a failed write would silently drop it.
    """
    values = payload.model_dump()

    # Redaction happens before the write, so nothing unredacted ever reaches
    # durable storage.
    values["input_preview"] = redact(values.get("input_preview"))
    values["output_preview"] = redact(values.get("output_preview"))

    # Idempotent on the producer-supplied id, so a spool replay of an event that
    # actually landed cannot double-write.
    stmt = insert(InferenceLog).values(**values).on_conflict_do_nothing(index_elements=["id"])
    db.execute(stmt)
    db.commit()

    return IngestAccepted(id=payload.id)


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
