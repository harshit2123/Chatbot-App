"""The recording API — the SDK's actual surface.

This is the piece that makes the library plug-and-play. The SDK never calls a
model, never imports a provider, and never knows what an OpenRouter response
looks like. It records *an attempt*: you open a span, you make your own call
however you like, and you tell the span how it went.

Two shapes, matching the two ways an LLM call can be made:

    with record(model="gpt-4") as span:              # blocking
        reply = my_client.complete(...)
        span.succeeded(output=reply.text)

    for chunk in record_stream(my_iterator, model="gpt-4"):   # streaming
        yield chunk

`record_stream` is the subtle one. A generation can end four ways — completion,
provider error, caller cancellation, and the consumer simply walking away — and
all four must produce exactly one log. The last case is the one that bites:
when an ASGI server abandons a response generator on client disconnect, the
generator is finalized without any normal exit path running. An unlogged model
call is the single outcome an observability tool must never have, so the
`finally` block below is the backstop.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypeVar

from llmlog.config import LogConfig

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

T = TypeVar("T")


def preview(text: str | None, max_chars: int) -> str | None:
    """Truncate for storage. Full payloads are deliberately not persisted."""
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


@dataclass
class Span:
    """One in-flight LLM attempt, and the event it will become.

    Mutable by design: the caller fills in what it learns as it learns it —
    the resolved model only after the response arrives, token counts only at
    the end of a stream.
    """

    model: str
    provider: str = "unknown"
    conversation_id: str | None = None
    message_id: str | None = None
    input_preview: str | None = None
    output_preview: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    status: str = STATUS_SUCCESS
    error_message: str | None = None
    ttft_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _start: float = field(default_factory=time.perf_counter, repr=False)
    _max_chars: int = field(default=500, repr=False)

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)

    def mark_first_token(self) -> None:
        """Record time-to-first-token, once.

        TTFT is the latency a user perceives on a streamed reply; total duration
        is a throughput number, not a responsiveness one.
        """
        if self.ttft_ms is None:
            self.ttft_ms = self.elapsed_ms()

    def succeeded(
        self,
        *,
        output: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        """Record a successful attempt.

        `provider` and `model` accept the *resolved* values: an aggregator may
        route to a different upstream provider or model variant than requested,
        and the log should say what actually served the call.
        """
        self.status = STATUS_SUCCESS
        if output is not None:
            self.output_preview = preview(output, self._max_chars)
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        if prompt_tokens is not None:
            self.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = completion_tokens

    def failed(self, error: BaseException | str) -> None:
        self.status = STATUS_ERROR
        self.error_message = (
            error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        )

    def cancelled(self, reason: str | None = None) -> None:
        self.status = STATUS_CANCELLED
        self.error_message = reason

    def set_input(self, text: str) -> None:
        self.input_preview = preview(text, self._max_chars)

    def append_output(self, delta: str) -> None:
        """Accumulate streamed output, truncating as it goes."""
        current = self.output_preview or ""
        self.output_preview = preview(current + delta, self._max_chars)

    def to_event(self) -> dict:
        """Serialize to the wire format accepted by the collector."""
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.elapsed_ms(),
            "ttft_ms": self.ttft_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "status": self.status,
            "error_message": self.error_message,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "started_at": self.started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **self.metadata,
        }


def build_span(config: LogConfig, *, model: str, input_text: str | None = None, **kwargs) -> Span:
    span = Span(model=model, _max_chars=config.preview_max_chars, **kwargs)
    if input_text is not None:
        span.set_input(input_text)
    return span


@contextmanager
def record_span(emit: Callable[[dict], None], span: Span) -> Iterator[Span]:
    """Emit exactly one event for a blocking attempt, success or failure.

    Failures are logged *and then re-raised*: error rate is only measurable if
    failed calls are recorded, not just successful traffic.
    """
    try:
        yield span
    except BaseException as exc:
        span.failed(exc)
        emit(span.to_event())
        raise
    else:
        emit(span.to_event())


def record_stream_span(
    emit: Callable[[dict], None],
    span: Span,
    chunks: Iterable[T],
    *,
    on_chunk: Callable[[Span, T], str | None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[T]:
    """Wrap a streaming iterator, emitting one event however the stream ends.

    `on_chunk` maps a caller-defined chunk onto the span and returns the text
    delta to accumulate — the only place the SDK touches a provider's shape, and
    the caller supplies it. Return None from it to swallow a chunk (a final
    usage-only frame, say) rather than yielding it on.
    """
    # Guards against double-emitting when several exit paths could fire, and
    # lets the `finally` below detect a stream that ended with no log at all.
    emitted = False

    try:
        for chunk in chunks:
            if should_cancel is not None and should_cancel():
                span.cancelled("cancelled by caller")
                emitted = True
                emit(span.to_event())
                return

            delta = on_chunk(span, chunk) if on_chunk is not None else None
            if delta is None:
                continue

            span.mark_first_token()
            span.append_output(delta)
            yield chunk

        emitted = True
        emit(span.to_event())
    except GeneratorExit:
        # The consumer stopped iterating — in practice, the browser disconnected
        # mid-stream. GeneratorExit derives from BaseException, so it escapes a
        # plain `except Exception`; without this branch the call was never logged
        # despite tokens having been spent. Recorded as `cancelled` because that
        # is what it is: a generation stopped early, not a provider error.
        span.cancelled("client disconnected")
        emitted = True
        emit(span.to_event())
        raise
    except BaseException as exc:
        span.failed(exc)
        emitted = True
        emit(span.to_event())
        raise
    finally:
        # Last line of defence. If this generator is finalized without any path
        # above running — which is what happens when the ASGI server abandons it
        # on client disconnect — a log is still emitted.
        if not emitted:
            span.cancelled("stream abandoned")
            emit(span.to_event())
