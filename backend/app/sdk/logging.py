"""The instrumentation wrapper.

Every LLM call in the app goes through `instrumented_completion()`. It times the
call, captures metadata, and ships a log event to the ingestion endpoint —
whether the call succeeded or failed. No call site does its own logging, and no
provider adapter knows logging exists.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import httpx

from app.config import Settings
from app.sdk.providers import (
    ChatMessage,
    CompletionResult,
    Provider,
    ProviderError,
    StreamChunk,
)

STATUS_CANCELLED = "cancelled"

logger = logging.getLogger(__name__)

INGEST_TIMEOUT_SECONDS = 5.0


def _preview(text: str, max_chars: int) -> str:
    """Truncate for storage. Full payloads are deliberately not persisted."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _flatten(messages: list[ChatMessage]) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in messages)


def emit_log_event(event: dict, settings: Settings) -> None:
    """Ship a log event to the ingestion endpoint.

    Logging must never break the chat request, so transport failures are
    swallowed and recorded locally instead of propagating. That is a real
    tradeoff: an ingestion outage silently drops observability data. A durable
    local spool would be the fix; see README future work.
    """
    try:
        response = httpx.post(settings.ingest_url, json=event, timeout=INGEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.warning("Failed to deliver inference log %s: %s", event.get("id"), exc)
        return

    # A non-2xx is a dropped log, not a delivered one. Without this check a
    # misrouted endpoint looks identical to success and logs vanish silently.
    if response.status_code >= 400:
        logger.warning(
            "Ingestion endpoint rejected log %s: HTTP %s %s",
            event.get("id"),
            response.status_code,
            response.text[:200],
        )


def instrumented_completion(
    provider: Provider,
    model: str,
    messages: list[ChatMessage],
    settings: Settings,
    conversation_id: str,
    message_id: str | None = None,
) -> CompletionResult:
    """Call the provider and emit an inference log for the attempt.

    Raises ProviderError on failure — after logging it, so error rate is
    measurable and not just success traffic.
    """
    log_id = str(uuid.uuid4())
    input_preview = _preview(_flatten(messages), settings.preview_max_chars)
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()

    def build_event(**overrides) -> dict:
        completed_at = datetime.now(timezone.utc)
        base = {
            "id": log_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "provider": provider.name,
            "model": model,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "prompt_tokens": None,
            "completion_tokens": None,
            "status": "success",
            "error_message": None,
            "input_preview": input_preview,
            "output_preview": None,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        }
        return {**base, **overrides}

    try:
        result = provider.complete(model=model, messages=messages)
    except ProviderError as exc:
        emit_log_event(build_event(status="error", error_message=str(exc)), settings)
        raise
    except Exception as exc:  # noqa: BLE001 - unexpected failures must still be logged
        emit_log_event(
            build_event(status="error", error_message=f"{type(exc).__name__}: {exc}"),
            settings,
        )
        raise

    emit_log_event(
        build_event(
            # The resolved values, not the requested ones: an aggregator may
            # route to a different upstream provider or model variant.
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            output_preview=_preview(result.content, settings.preview_max_chars),
        ),
        settings,
    )
    return result


def instrumented_stream(
    provider: Provider,
    model: str,
    messages: list[ChatMessage],
    settings: Settings,
    conversation_id: str,
    message_id: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[StreamChunk]:
    """Stream a completion, emitting one log event when the stream terminates.

    Terminates for three reasons, all of which must be logged:
      - the stream finishes normally    -> status=success
      - the provider errors mid-stream  -> status=error
      - the caller cancels              -> status=cancelled, with partial output

    `latency_ms` measures the full stream duration. Time-to-first-token would be
    the more useful streaming metric; noted in the README as a future addition.
    """
    log_id = str(uuid.uuid4())
    input_preview = _preview(_flatten(messages), settings.preview_max_chars)
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()

    collected: list[str] = []
    resolved_provider = provider.name
    resolved_model = model
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def build_event(**overrides) -> dict:
        base = {
            "id": log_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "provider": resolved_provider,
            "model": resolved_model,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "status": "success",
            "error_message": None,
            "input_preview": input_preview,
            "output_preview": _preview("".join(collected), settings.preview_max_chars),
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        return {**base, **overrides}

    try:
        for chunk in provider.stream(model=model, messages=messages):
            resolved_provider = chunk.provider
            resolved_model = chunk.model

            if chunk.is_final:
                prompt_tokens = chunk.prompt_tokens
                completion_tokens = chunk.completion_tokens
                break

            if should_cancel is not None and should_cancel():
                emit_log_event(build_event(status=STATUS_CANCELLED), settings)
                return

            collected.append(chunk.delta)
            yield chunk
    except ProviderError as exc:
        emit_log_event(build_event(status="error", error_message=str(exc)), settings)
        raise
    except Exception as exc:  # noqa: BLE001 - unexpected failures must still be logged
        emit_log_event(
            build_event(status="error", error_message=f"{type(exc).__name__}: {exc}"),
            settings,
        )
        raise

    emit_log_event(build_event(), settings)
