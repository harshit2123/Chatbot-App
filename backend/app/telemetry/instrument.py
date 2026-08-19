"""Binds the generic `llmlog` SDK to this app's provider shapes.

This module is the *only* place that knows both sides. `llmlog` knows nothing
about `Provider` or `StreamChunk`; `app.llm.providers` knows nothing about
logging. Everything app-specific about instrumentation lives here, in about a
hundred lines, which is what makes the SDK liftable into another project.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import llmlog

from app.config import Settings
from app.llm.providers import ChatMessage, CompletionResult, Provider, StreamChunk


def _flatten(messages: list[ChatMessage]) -> str:
    return "\n".join(f"{m.role}: {m.content}" for m in messages)


def configure_telemetry(settings: Settings) -> None:
    """Point the SDK at this app's collector. Called once at startup."""
    llmlog.configure(
        llmlog.LogConfig(
            ingest_url=settings.ingest_url,
            spool_enabled=settings.spool_enabled,
            spool_dir=settings.spool_dir,
            replay_interval_seconds=settings.spool_replay_interval_seconds,
            preview_max_chars=settings.preview_max_chars,
        )
    )


def instrumented_completion(
    provider: Provider,
    model: str,
    messages: list[ChatMessage],
    conversation_id: str,
    message_id: str | None = None,
) -> CompletionResult:
    """Call the provider and record the attempt.

    Failures propagate — after being recorded, so error rate is measurable and
    not just success traffic.
    """
    with llmlog.record(
        model=model,
        provider=provider.name,
        input_text=_flatten(messages),
        conversation_id=conversation_id,
        message_id=message_id,
    ) as span:
        result = provider.complete(model=model, messages=messages)
        span.succeeded(
            output=result.content,
            # The resolved values, not the requested ones: an aggregator may
            # route to a different upstream provider or model variant.
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result


def _on_chunk(span: llmlog.Span, chunk: StreamChunk) -> str | None:
    """Map one provider chunk onto the span.

    Returning None swallows the chunk: the final frame carries usage totals
    rather than text, so it updates the span and is not yielded on.
    """
    if chunk.is_final:
        span.succeeded(
            provider=chunk.provider,
            model=chunk.model,
            prompt_tokens=chunk.prompt_tokens,
            completion_tokens=chunk.completion_tokens,
        )
        return None

    span.provider = chunk.provider
    span.model = chunk.model
    return chunk.delta


def instrumented_stream(
    provider: Provider,
    model: str,
    messages: list[ChatMessage],
    conversation_id: str,
    message_id: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[StreamChunk]:
    """Stream a completion, recording one event however the stream terminates."""
    return llmlog.record_stream(
        provider.stream(model=model, messages=messages),
        model=model,
        provider=provider.name,
        on_chunk=_on_chunk,
        should_cancel=should_cancel,
        input_text=_flatten(messages),
        conversation_id=conversation_id,
        message_id=message_id,
    )
