"""llmlog — drop-in durable telemetry for LLM calls.

Provider-agnostic by construction: this package never makes a model call and
never imports a provider SDK. You make the call; llmlog records that you made
it, how long it took, and how it ended — then guarantees the record survives a
collector outage, a crash, or a power cut.

Quick start:

    import llmlog

    llmlog.configure(ingest_url="http://collector:8000/ingest")
    llmlog.start_replay_worker()

    # Blocking call
    with llmlog.record(model="gpt-4", input_text=prompt) as span:
        reply = my_client.complete(prompt)
        span.succeeded(output=reply.text, completion_tokens=reply.usage.output)

    # Streaming call
    def to_delta(span, chunk):
        if chunk.done:
            span.succeeded(completion_tokens=chunk.usage)
            return None          # swallow the usage-only frame
        return chunk.text

    for chunk in llmlog.record_stream(provider_iter, model="gpt-4", on_chunk=to_delta):
        yield chunk

Configuration comes from `configure()`, `LogConfig.from_env()`, or the
`LLMLOG_*` environment variables. Nothing here imports the host application.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from llmlog.client import LogClient
from llmlog.config import LogConfig
from llmlog.record import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_SUCCESS,
    Span,
    build_span,
    record_span,
    record_stream_span,
)

__all__ = [
    "LogConfig",
    "LogClient",
    "Span",
    "STATUS_SUCCESS",
    "STATUS_ERROR",
    "STATUS_CANCELLED",
    "configure",
    "get_client",
    "emit",
    "record",
    "record_stream",
    "replay",
    "start_replay_worker",
]

T = TypeVar("T")

_client: LogClient | None = None


def configure(config: LogConfig | None = None, **kwargs: Any) -> LogClient:
    """Install the process-wide client. Call once at startup.

    Accepts either a `LogConfig` or bare keyword overrides:

        llmlog.configure(ingest_url="http://collector/ingest", spool_dir="/var/spool")
    """
    global _client
    if config is None:
        config = LogConfig(**kwargs) if kwargs else LogConfig.from_env()
    _client = LogClient(config)
    return _client


def get_client() -> LogClient:
    """The process-wide client, created from the environment on first use.

    Lazily defaulting rather than raising means an unconfigured import still
    works — telemetry should never be the thing that breaks someone's app.
    """
    global _client
    if _client is None:
        _client = LogClient(LogConfig.from_env())
    return _client


def emit(event: dict) -> None:
    """Spool and ship a pre-built event. The low-level escape hatch."""
    get_client().emit(event)


@contextmanager
def record(
    *,
    model: str,
    input_text: str | None = None,
    provider: str = "unknown",
    conversation_id: str | None = None,
    message_id: str | None = None,
    **metadata: Any,
) -> Iterator[Span]:
    """Record one blocking LLM attempt. Emits exactly one event either way."""
    client = get_client()
    span = build_span(
        client.config,
        model=model,
        input_text=input_text,
        provider=provider,
        conversation_id=conversation_id,
        message_id=message_id,
        metadata=metadata,
    )
    with record_span(client.emit, span) as active:
        yield active


def record_stream(
    chunks: Iterable[T],
    *,
    model: str,
    on_chunk: Callable[[Span, T], str | None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    input_text: str | None = None,
    provider: str = "unknown",
    conversation_id: str | None = None,
    message_id: str | None = None,
    **metadata: Any,
) -> Iterator[T]:
    """Record one streaming LLM attempt, emitting one event however it ends.

    Handles all four terminations: completion, provider error, caller
    cancellation, and consumer abandonment (client disconnect).
    """
    client = get_client()
    span = build_span(
        client.config,
        model=model,
        input_text=input_text,
        provider=provider,
        conversation_id=conversation_id,
        message_id=message_id,
        metadata=metadata,
    )
    return record_stream_span(
        client.emit,
        span,
        chunks,
        on_chunk=on_chunk,
        should_cancel=should_cancel,
    )


def replay() -> int:
    """Drain the spool once. Returns the number of events delivered."""
    return get_client().replay()


def start_replay_worker():
    """Start the background loop that drains the spool after an outage."""
    return get_client().start_replay_worker()
