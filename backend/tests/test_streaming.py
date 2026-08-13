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
