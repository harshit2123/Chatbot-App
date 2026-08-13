"""Conversation lifecycle and the chat turn itself."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import ROLE_ASSISTANT, ROLE_USER, Conversation, Message
from app.db.session import get_db
from app.models.schemas import (
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    SendMessageResponse,
)
from app.sdk.logging import instrumented_completion
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

    user_message = Message(
        conversation_id=conversation.id, role=ROLE_USER, content=payload.content
    )
    db.add(user_message)
    if conversation.title is None:
        conversation.title = _derive_title(payload.content)
    db.commit()
    db.refresh(user_message)

    history = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(settings.history_turn_limit)
    ).all()
    # Re-reverse: newest-first was only for the LIMIT to select recent turns.
    context = [ChatMessage(role=m.role, content=m.content) for m in reversed(history)]

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
