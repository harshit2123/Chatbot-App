"""Streaming wrapper tests.

The streaming path has three exits — completion, error, cancellation — and each
must produce exactly one log event. A missing log here is invisible at runtime,
which is what makes these worth writing.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.sdk.logging import instrumented_stream
from app.sdk.providers import ChatMessage, MockProvider, ProviderError, StreamChunk


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused/unused",
        ingest_url="http://ingest.invalid/ingest",
        preview_max_chars=200,
    )


@pytest.fixture
def captured(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(
        "app.sdk.logging.emit_log_event", lambda event, settings: events.append(event)
    )
    return events


class StreamStub:
    name = "stub"

    def __init__(self, deltas: list[str], error: Exception | None = None) -> None:
        self._deltas = deltas
        self._error = error

    def complete(self, model, messages):  # pragma: no cover - unused here
        raise NotImplementedError

    def stream(self, model: str, messages: list[ChatMessage]):
        for delta in self._deltas:
            yield StreamChunk(delta=delta, provider=self.name, model=model)
        if self._error is not None:
            raise self._error
        yield StreamChunk(
            delta="",
            provider=self.name,
            model=model,
            is_final=True,
            prompt_tokens=7,
            completion_tokens=len(self._deltas),
        )


def _run(provider, settings, should_cancel=None) -> list[StreamChunk]:
    return list(
        instrumented_stream(
            provider=provider,
            model="m",
            messages=[ChatMessage(role="user", content="hello")],
            settings=settings,
            conversation_id=str(uuid.uuid4()),
            should_cancel=should_cancel,
        )
    )


def test_stream_yields_all_deltas_and_logs_once(settings, captured):
    chunks = _run(StreamStub(["Hel", "lo", " world"]), settings)

    assert "".join(c.delta for c in chunks) == "Hello world"
    assert len(captured) == 1

    event = captured[0]
    assert event["status"] == "success"
    assert event["output_preview"] == "Hello world"
    assert event["prompt_tokens"] == 7
    assert event["completion_tokens"] == 3
    assert event["latency_ms"] >= 0


def test_cancellation_stops_stream_and_logs_partial_output(settings, captured):
    # Cancel after the first delta has been consumed.
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    chunks = _run(StreamStub(["one", "two", "three"]), settings, should_cancel)

    # Stopped early rather than draining the generator.
    assert len(chunks) < 3
    assert len(captured) == 1

    event = captured[0]
    assert event["status"] == "cancelled"
    # Partial output is retained — a cancelled turn is still evidence.
    assert event["output_preview"] == "one"


def test_provider_error_midstream_is_logged_and_reraised(settings, captured):
    with pytest.raises(ProviderError):
        _run(StreamStub(["partial"], error=ProviderError("stream died")), settings)

    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    assert "stream died" in captured[0]["error_message"]
    # Whatever arrived before the failure is preserved for debugging.
    assert captured[0]["output_preview"] == "partial"


def test_resolved_provider_from_chunks_is_logged(settings, captured):
    class Rerouted(StreamStub):
        def stream(self, model, messages):
            yield StreamChunk(delta="hi", provider="anthropic", model="claude-3.5-sonnet")
            yield StreamChunk(
                delta="", provider="anthropic", model="claude-3.5-sonnet", is_final=True
            )

    _run(Rerouted([]), settings)

    assert captured[0]["provider"] == "anthropic"
    assert captured[0]["model"] == "claude-3.5-sonnet"


def test_client_disconnect_midstream_is_logged(settings, captured):
    """Regression: an abandoned stream produced no log at all.

    GeneratorExit derives from BaseException, so it bypassed both `except`
    clauses and the generator unwound silently — a real provider call, real
    tokens spent, and zero record of it. Exactly the blind spot this system
    exists to prevent.
    """
    stream = instrumented_stream(
        provider=StreamStub(["one", "two", "three", "four"]),
        model="m",
        messages=[ChatMessage(role="user", content="hello")],
        settings=settings,
        conversation_id=str(uuid.uuid4()),
    )

    # Consume two chunks, then abandon the generator the way a disconnecting
    # client does.
    next(stream)
    next(stream)
    stream.close()

    assert len(captured) == 1, "abandoned stream must still emit exactly one log"
    event = captured[0]
    assert event["status"] == "cancelled"
    assert event["error_message"] == "client disconnected"
    # Partial output is retained: those tokens were generated and paid for.
    assert event["output_preview"] == "onetwo"
    assert event["latency_ms"] >= 0


def test_abandoned_generator_still_emits_exactly_one_log(settings, captured):
    """A stream dropped without close() must still produce a log, and only one.

    The ASGI server abandons the body iterator on client disconnect rather than
    closing it, so the `finally` is the only cleanup that reliably runs.
    """
    stream = instrumented_stream(
        provider=StreamStub(["a", "b", "c"]),
        model="m",
        messages=[ChatMessage(role="user", content="hello")],
        settings=settings,
        conversation_id=str(uuid.uuid4()),
    )
    next(stream)

    # Drop the only reference and force finalization.
    del stream
    import gc

    gc.collect()

    assert len(captured) == 1
    assert captured[0]["status"] == "cancelled"
    assert captured[0]["error_message"] in {"client disconnected", "stream abandoned"}


def test_ttft_is_captured_and_distinct_from_total_latency(settings, captured):
    """Time-to-first-token is what a user feels; total duration is not.

    A 17s stream whose first token arrived in 400ms was previously logged as
    17s of "latency", which made streamed calls look far worse than they felt.
    """
    import time as time_module

    class SlowFirstToken:
        name = "slow"

        def complete(self, model, messages):  # pragma: no cover - unused
            raise NotImplementedError

        def stream(self, model, messages):
            time_module.sleep(0.2)  # model think time before the first token
            yield StreamChunk(delta="first", provider=self.name, model=model)
            time_module.sleep(0.15)  # remaining generation
            yield StreamChunk(delta=" rest", provider=self.name, model=model)
            yield StreamChunk(
                delta="", provider=self.name, model=model, is_final=True,
                prompt_tokens=2, completion_tokens=2,
            )

    _run(SlowFirstToken(), settings)

    event = captured[0]
    assert event["ttft_ms"] is not None
    assert event["ttft_ms"] >= 150, "should reflect the wait before the first token"
    # The distinction is the entire point of the metric.
    assert event["ttft_ms"] < event["latency_ms"]


def test_non_streaming_calls_have_no_ttft(settings, captured):
    """TTFT is meaningless for a blocking call — null, not zero, not latency."""
    from app.sdk.logging import instrumented_completion
    from app.sdk.providers import CompletionResult

    class Blocking:
        name = "blocking"

        def complete(self, model, messages):
            return CompletionResult(content="hi", provider=self.name, model=model)

        def stream(self, model, messages):  # pragma: no cover - unused
            raise NotImplementedError

    instrumented_completion(
        provider=Blocking(),
        model="m",
        messages=[ChatMessage(role="user", content="hello")],
        settings=settings,
        conversation_id=str(uuid.uuid4()),
    )

    assert captured[0].get("ttft_ms") is None


def test_mock_provider_stream_matches_complete_output(settings):
    """Streaming and non-streaming must not diverge in content."""
    provider = MockProvider()
    provider.chunk_delay_seconds = 0  # keep the test fast
    messages = [ChatMessage(role="user", content="hello there")]

    streamed = "".join(
        chunk.delta for chunk in provider.stream("mock/echo-1", messages) if not chunk.is_final
    )
    completed = provider.complete("mock/echo-1", messages).content

    assert streamed == completed
