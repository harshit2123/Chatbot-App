"""Pydantic request/response schemas. These are the validation boundary."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_MESSAGE_CHARS = 20_000


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


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
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    status: Literal["success", "error"]
    error_message: str | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class InferenceLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    provider: str
    model: str
    latency_ms: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    status: str
    error_message: str | None
    input_preview: str | None
    output_preview: str | None
    created_at: datetime


class IngestAccepted(BaseModel):
    id: uuid.UUID
    accepted: bool = True
