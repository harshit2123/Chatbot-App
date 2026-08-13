"""Conversation lifecycle and the chat turn itself."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import cancellation
from app.config import Settings, get_settings
from app.db.models import ROLE_ASSISTANT, ROLE_USER, Conversation, Message
from app.db.session import SessionLocal, get_db
from app.models.schemas import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    SendMessageResponse,
)
from app.sdk.logging import instrumented_completion, instrumented_stream
from app.sdk.providers import ChatMessage, ProviderError, build_provider

router = APIRouter(prefix="/conversations", tags=["conversations"])

TITLE_MAX_CHARS = 60


def _load_conversation(conversation_id: uuid.UUID, db: Session) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _derive_title(content: str) -> str:
    single_line = " ".join(content.split())
    if len(single_line) <= TITLE_MAX_CHARS:
        return single_line
    return single_line[:TITLE_MAX_CHARS] + "…"


def _build_context(
    conversation_id: uuid.UUID, db: Session, limit: int
) -> list[ChatMessage]:
    """Most recent `limit` messages, oldest-first, as provider input."""
    history = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    ).all()
    # Re-reverse: newest-first was only for the LIMIT to select recent turns.
    return [ChatMessage(role=m.role, content=m.content) for m in reversed(history)]


def _persist_user_message(
    conversation: Conversation, content: str, db: Session
) -> Message:
    """Commit the user's message before any model call, so a failure cannot lose it."""
    message = Message(conversation_id=conversation.id, role=ROLE_USER, content=content)
    db.add(message)
    if conversation.title is None:
        conversation.title = _derive_title(content)
    db.commit()
    db.refresh(message)
    return message


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: ConversationCreate, db: Session = Depends(get_db)
) -> Conversation:
    conversation = Conversation(title=payload.title)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("", response_model=list[ConversationOut])
def list_conversations(db: Session = Depends(get_db)) -> list[Conversation]:
    stmt = select(Conversation).order_by(Conversation.created_at.desc())
    return list(db.scalars(stmt).all())


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
def get_messages(conversation_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Message]:
    """Resume support: full history for a conversation, oldest first."""
    _load_conversation(conversation_id, db)
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(db.scalars(stmt).all())


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
def send_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SendMessageResponse:
    """One chat turn: persist the user message, call the model with trimmed
    history, persist the reply.

    The user message is committed before the model call so a provider failure
    doesn't lose what the user typed.
    """
    conversation = _load_conversation(conversation_id, db)
    user_message = _persist_user_message(conversation, payload.content, db)
    context = _build_context(conversation_id, db, settings.history_turn_limit)

    try:
        provider = build_provider(settings)
        result = instrumented_completion(
            provider=provider,
            model=settings.llm_model,
            messages=context,
            settings=settings,
            conversation_id=str(conversation_id),
            message_id=str(user_message.id),
        )
    except ProviderError as exc:
        # Already logged as status=error by the wrapper.
        raise HTTPException(status_code=502, detail=f"Provider call failed: {exc}") from exc

    assistant_message = Message(
        conversation_id=conversation.id, role=ROLE_ASSISTANT, content=result.content
    )
    db.add(assistant_message)
    db.commit()
    db.refresh(assistant_message)

    return SendMessageResponse(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
    )


@router.post("/{conversation_id}/messages/stream")
def stream_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Send a message and stream the reply back as Server-Sent Events.

    Frames: `start` (ids), `delta` (token), `done` (final), `cancelled`, `error`.
    """
    conversation = _load_conversation(conversation_id, db)

    # Clear any stale flag so a previous cancel cannot kill this generation.
    cancellation.clear_cancel(str(conversation_id))

    user_message = _persist_user_message(conversation, payload.content, db)
    context = _build_context(conversation_id, db, settings.history_turn_limit)
    user_message_id = str(user_message.id)
    user_message_payload = MessageOut.model_validate(user_message).model_dump(mode="json")

    def event_stream() -> Iterator[str]:
        # The request-scoped session is closed once this generator starts, so
        # the stream owns its own session for the final write.
        collected: list[str] = []

        yield _sse("start", {"user_message": user_message_payload})

        try:
            provider = build_provider(settings)
            chunks = instrumented_stream(
                provider=provider,
                model=settings.llm_model,
                messages=context,
                settings=settings,
                conversation_id=str(conversation_id),
                message_id=user_message_id,
                should_cancel=lambda: cancellation.is_cancelled(str(conversation_id)),
            )

            for chunk in chunks:
                collected.append(chunk.delta)
                yield _sse("delta", {"content": chunk.delta})
        except ProviderError as exc:
            yield _sse("error", {"detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surface, don't hang the client
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"})
            return

        was_cancelled = cancellation.is_cancelled(str(conversation_id))
        content = "".join(collected)

        # Persist whatever was generated. A cancelled turn keeps its partial
        # reply so the conversation stays coherent on resume.
        assistant_payload = None
        if content:
            with SessionLocal() as session:
                assistant_message = Message(
                    conversation_id=conversation_id,
                    role=ROLE_ASSISTANT,
                    content=content,
                )
                session.add(assistant_message)
                session.commit()
                session.refresh(assistant_message)
                assistant_payload = MessageOut.model_validate(assistant_message).model_dump(
                    mode="json"
                )

        if was_cancelled:
            cancellation.clear_cancel(str(conversation_id))
            yield _sse("cancelled", {"assistant_message": assistant_payload})
        else:
            yield _sse("done", {"assistant_message": assistant_payload})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Prevents proxies (nginx) from buffering the stream into one blob.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{conversation_id}/cancel", status_code=202)
def cancel_generation(
    conversation_id: uuid.UUID, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Signal an in-flight generation to stop.

    Sets a flag the streaming loop checks between chunks; it does not abort the
    upstream provider request mid-flight.
    """
    _load_conversation(conversation_id, db)
    cancellation.request_cancel(str(conversation_id))
    return {"status": "cancellation_requested"}
