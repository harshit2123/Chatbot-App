"""Log ingestion endpoint.

Phase 1 writes straight to Postgres. Phase 2 swaps the body of `ingest_log()`
for a Celery enqueue — the wrapper, the payload shape, and the DB row stay
identical, which is why the swap is contained to this one function.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import InferenceLog
from app.db.session import get_db
from app.models.schemas import IngestAccepted, InferenceLogIn, InferenceLogOut

router = APIRouter(tags=["ingest"])

DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 500


@router.post("/ingest", response_model=IngestAccepted, status_code=202)
def ingest_log(payload: InferenceLogIn, db: Session = Depends(get_db)) -> IngestAccepted:
    """Accept a log event. Idempotent on the producer-supplied id."""
    values = payload.model_dump()

    # ON CONFLICT DO NOTHING: a retried delivery of the same event must not
    # produce a duplicate row or a 500.
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
    """Read back stored logs. Powers manual verification now, dashboards later."""
    stmt = select(InferenceLog).order_by(InferenceLog.created_at.desc()).limit(limit)
    if conversation_id is not None:
        stmt = stmt.where(InferenceLog.conversation_id == conversation_id)
    return list(db.scalars(stmt).all())
