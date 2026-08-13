"""Aggregation endpoints backing the dashboard.

Aggregation happens in Postgres, not Python: these are indexed columns, and
pulling rows into the API to sum them would not survive real log volume.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import STATUS_ERROR, InferenceLog
from app.db.session import get_db
from app.models.schemas import (
    ErrorPoint,
    LatencyPoint,
    MetricsSummary,
    ProviderBreakdown,
    ThroughputPoint,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])

DEFAULT_WINDOW_MINUTES = 60
MAX_WINDOW_MINUTES = 60 * 24 * 7


def _window_start(minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


def _bucket(column, seconds: int):
    """Truncate a timestamp to fixed-width buckets for time series grouping."""
    return func.to_timestamp(
        func.floor(func.extract("epoch", column) / seconds) * seconds
    )


def _bucket_seconds(minutes: int) -> int:
    """Keep the series readable: roughly 30-60 points regardless of window."""
    if minutes <= 60:
        return 60
    if minutes <= 60 * 24:
        return 60 * 30
    return 60 * 60 * 6


@router.get("/summary", response_model=MetricsSummary)
def metrics_summary(
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES),
    db: Session = Depends(get_db),
) -> MetricsSummary:
    """Headline numbers for the dashboard tiles."""
    since = _window_start(window_minutes)

    row = db.execute(
        select(
            func.count().label("total"),
            func.avg(InferenceLog.latency_ms).label("avg_latency"),
            func.percentile_cont(0.95)
            .within_group(InferenceLog.latency_ms.asc())
            .label("p95_latency"),
            func.sum(case((InferenceLog.status == STATUS_ERROR, 1), else_=0)).label("errors"),
            func.sum(func.coalesce(InferenceLog.prompt_tokens, 0)).label("prompt_tokens"),
            func.sum(func.coalesce(InferenceLog.completion_tokens, 0)).label(
                "completion_tokens"
            ),
        ).where(InferenceLog.created_at >= since)
    ).one()

    total = row.total or 0
    errors = int(row.errors or 0)

    return MetricsSummary(
        window_minutes=window_minutes,
        total_calls=total,
        error_count=errors,
        error_rate=(errors / total) if total else 0.0,
        avg_latency_ms=float(row.avg_latency) if row.avg_latency is not None else None,
        p95_latency_ms=float(row.p95_latency) if row.p95_latency is not None else None,
        total_prompt_tokens=int(row.prompt_tokens or 0),
        total_completion_tokens=int(row.completion_tokens or 0),
    )


@router.get("/latency", response_model=list[LatencyPoint])
def latency_series(
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES),
    db: Session = Depends(get_db),
) -> list[LatencyPoint]:
    since = _window_start(window_minutes)
    seconds = _bucket_seconds(window_minutes)
    bucket = _bucket(InferenceLog.created_at, seconds).label("bucket")

    rows = db.execute(
        select(
            bucket,
            func.avg(InferenceLog.latency_ms).label("avg_latency"),
            func.max(InferenceLog.latency_ms).label("max_latency"),
            func.count().label("count"),
        )
        .where(InferenceLog.created_at >= since, InferenceLog.latency_ms.isnot(None))
        .group_by(bucket)
        .order_by(bucket)
    ).all()

    return [
        LatencyPoint(
            bucket=row.bucket,
            avg_latency_ms=float(row.avg_latency) if row.avg_latency is not None else 0.0,
            max_latency_ms=int(row.max_latency) if row.max_latency is not None else 0,
            count=row.count,
        )
        for row in rows
    ]


@router.get("/errors", response_model=list[ErrorPoint])
def error_series(
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES),
    db: Session = Depends(get_db),
) -> list[ErrorPoint]:
    since = _window_start(window_minutes)
    seconds = _bucket_seconds(window_minutes)
    bucket = _bucket(InferenceLog.created_at, seconds).label("bucket")

    error_count = func.sum(case((InferenceLog.status == STATUS_ERROR, 1), else_=0))

    rows = db.execute(
        select(
            bucket,
            func.count().label("total"),
            error_count.label("errors"),
            # Cast so integer division doesn't floor the rate to 0.
            (cast(error_count, Float) / func.nullif(func.count(), 0)).label("rate"),
        )
        .where(InferenceLog.created_at >= since)
        .group_by(bucket)
        .order_by(bucket)
    ).all()

    return [
        ErrorPoint(
            bucket=row.bucket,
            total=row.total,
            errors=int(row.errors or 0),
            error_rate=float(row.rate or 0.0),
        )
        for row in rows
    ]


@router.get("/throughput", response_model=list[ThroughputPoint])
def throughput_series(
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES),
    db: Session = Depends(get_db),
) -> list[ThroughputPoint]:
    since = _window_start(window_minutes)
    seconds = _bucket_seconds(window_minutes)
    bucket = _bucket(InferenceLog.created_at, seconds).label("bucket")

    rows = db.execute(
        select(bucket, func.count().label("count"))
        .where(InferenceLog.created_at >= since)
        .group_by(bucket)
        .order_by(bucket)
    ).all()

    minutes_per_bucket = seconds / 60
    return [
        ThroughputPoint(
            bucket=row.bucket,
            count=row.count,
            calls_per_minute=row.count / minutes_per_bucket,
        )
        for row in rows
    ]


@router.get("/providers", response_model=list[ProviderBreakdown])
def provider_breakdown(
    window_minutes: int = Query(DEFAULT_WINDOW_MINUTES, ge=1, le=MAX_WINDOW_MINUTES),
    db: Session = Depends(get_db),
) -> list[ProviderBreakdown]:
    """Per-provider/model split — the payoff of storing resolved provider names."""
    since = _window_start(window_minutes)

    rows = db.execute(
        select(
            InferenceLog.provider,
            InferenceLog.model,
            func.count().label("count"),
            func.avg(InferenceLog.latency_ms).label("avg_latency"),
            func.sum(case((InferenceLog.status == STATUS_ERROR, 1), else_=0)).label("errors"),
        )
        .where(InferenceLog.created_at >= since)
        .group_by(InferenceLog.provider, InferenceLog.model)
        .order_by(func.count().desc())
    ).all()

    return [
        ProviderBreakdown(
            provider=row.provider,
            model=row.model,
            count=row.count,
            avg_latency_ms=float(row.avg_latency) if row.avg_latency is not None else None,
            error_count=int(row.errors or 0),
        )
        for row in rows
    ]
