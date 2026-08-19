"""Pydantic request/response schemas. These are the validation boundary."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_MESSAGE_CHARS = 20_000


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdate(BaseModel):
    """Rename. Title is required here — a rename to nothing is a delete."""

    title: str = Field(min_length=1, max_length=200)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    status: str
    created_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class SendMessageResponse(BaseModel):
    """Both sides of the turn, so the client can reconcile optimistic state."""

    user_message: MessageOut
    assistant_message: MessageOut


class InferenceLogIn(BaseModel):
    """Payload accepted by POST /ingest.

    Deliberately strict: a malformed log event is a 422, not a silent partial
    write. `id` comes from the producer so redelivery is idempotent.
    """

    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None = None
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    latency_ms: int | None = Field(default=None, ge=0)
    # Streaming only; null for blocking calls.
    ttft_ms: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    status: Literal["success", "error", "cancelled"]
    error_message: str | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class InferenceLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Null when the parent conversation was deleted but the telemetry was kept.
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    provider: str
    model: str
    latency_ms: int | None
    ttft_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    status: str
    error_message: str | None
    input_preview: str | None
    output_preview: str | None
    created_at: datetime


class MetricsSummary(BaseModel):
    window_minutes: int
    total_calls: int
    error_count: int
    error_rate: float
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    # Averaged over streamed calls only; null when there are none in the window.
    avg_ttft_ms: float | None
    total_prompt_tokens: int
    total_completion_tokens: int


class LatencyPoint(BaseModel):
    bucket: datetime
    avg_latency_ms: float
    max_latency_ms: int
    count: int


class ErrorPoint(BaseModel):
    bucket: datetime
    total: int
    errors: int
    error_rate: float


class ThroughputPoint(BaseModel):
    bucket: datetime
    count: int
    calls_per_minute: float


class ProviderBreakdown(BaseModel):
    provider: str
    model: str
    count: int
    avg_latency_ms: float | None
    error_count: int


class IngestAccepted(BaseModel):
    id: uuid.UUID
    accepted: bool = True
