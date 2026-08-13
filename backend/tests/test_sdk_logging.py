"""Tests for the instrumentation wrapper.

The wrapper is the component whose failures are invisible at runtime — it
deliberately swallows transport errors so logging can't break chat. These tests
are what stop that from hiding a real regression.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import Settings
from app.sdk.logging import instrumented_completion
from app.sdk.providers import ChatMessage, CompletionResult, ProviderError


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused/unused",
        ingest_url="http://ingest.invalid/ingest",
        preview_max_chars=50,
    )


@pytest.fixture
def captured(monkeypatch) -> list[dict]:
    """Capture emitted log events instead of sending them over HTTP."""
    events: list[dict] = []
    monkeypatch.setattr(
        "app.sdk.logging.emit_log_event", lambda event, settings: events.append(event)
    )
    return events


class StubProvider:
    name = "stub"

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult:
        if self._error is not None:
            raise self._error
        return self._result


def test_successful_call_logs_full_metadata(settings, captured):
    provider = StubProvider(
        result=CompletionResult(
            content="hi there",
            provider="anthropic",
            model="claude-3.5-sonnet",
            prompt_tokens=11,
            completion_tokens=4,
        )
    )
    conversation_id = str(uuid.uuid4())

    result = instrumented_completion(
        provider=provider,
        model="requested-model",
        messages=[ChatMessage(role="user", content="hello")],
        settings=settings,
        conversation_id=conversation_id,
        message_id=None,
    )

    assert result.content == "hi there"
    assert len(captured) == 1
    event = captured[0]

    assert event["status"] == "success"
    assert event["conversation_id"] == conversation_id
    assert event["prompt_tokens"] == 11
    assert event["completion_tokens"] == 4
    assert event["latency_ms"] >= 0
    assert event["started_at"] and event["completed_at"]
    assert event["error_message"] is None


def test_resolved_provider_and_model_are_logged_not_requested_ones(settings, captured):
    """An aggregator may route elsewhere; the log must record what actually served it."""
    provider = StubProvider(
        result=CompletionResult(
            content="ok", provider="google", model="gemini-2.0-flash", prompt_tokens=1
        )
    )

    instrumented_completion(
        provider=provider,
        model="auto",
        messages=[ChatMessage(role="user", content="hello")],
        settings=settings,
        conversation_id=str(uuid.uuid4()),
    )

    assert captured[0]["provider"] == "google"
    assert captured[0]["model"] == "gemini-2.0-flash"


def test_provider_failure_is_logged_then_reraised(settings, captured):
    provider = StubProvider(error=ProviderError("upstream exploded"))

    with pytest.raises(ProviderError):
        instrumented_completion(
            provider=provider,
            model="m",
            messages=[ChatMessage(role="user", content="hello")],
            settings=settings,
            conversation_id=str(uuid.uuid4()),
        )

    # Error traffic must be measurable, not dropped.
    assert len(captured) == 1
    assert captured[0]["status"] == "error"
    assert "upstream exploded" in captured[0]["error_message"]
    assert captured[0]["output_preview"] is None


def test_unexpected_exception_is_also_logged(settings, captured):
    provider = StubProvider(error=ValueError("boom"))

    with pytest.raises(ValueError):
        instrumented_completion(
            provider=provider,
            model="m",
            messages=[ChatMessage(role="user", content="hello")],
            settings=settings,
            conversation_id=str(uuid.uuid4()),
        )

    assert captured[0]["status"] == "error"
    assert "ValueError: boom" in captured[0]["error_message"]


def test_previews_are_truncated_to_budget(settings, captured):
    long_reply = "x" * 500
    provider = StubProvider(
        result=CompletionResult(content=long_reply, provider="stub", model="m")
    )

    instrumented_completion(
        provider=provider,
        model="m",
        messages=[ChatMessage(role="user", content="y" * 500)],
        settings=settings,
        conversation_id=str(uuid.uuid4()),
    )

    event = captured[0]
    # max_chars plus the ellipsis marker.
    assert len(event["output_preview"]) == settings.preview_max_chars + 1
    assert event["output_preview"].endswith("…")
    assert len(event["input_preview"]) == settings.preview_max_chars + 1


def test_transport_failure_does_not_break_the_call(settings, monkeypatch):
    """An ingestion outage must not fail the user's chat request."""
    import httpx

    def explode(*args, **kwargs):
        raise httpx.ConnectError("ingest down")

    monkeypatch.setattr(httpx, "post", explode)

    provider = StubProvider(
        result=CompletionResult(content="still works", provider="stub", model="m")
    )

    result = instrumented_completion(
        provider=provider,
        model="m",
        messages=[ChatMessage(role="user", content="hello")],
        settings=settings,
        conversation_id=str(uuid.uuid4()),
    )

    assert result.content == "still works"


def test_non_2xx_ingest_response_is_warned_not_silently_dropped(settings, monkeypatch, caplog):
    """A misrouted endpoint returning 400 must not look like a successful delivery."""
    import httpx

    class FakeResponse:
        status_code = 400
        text = "rejected"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())

    provider = StubProvider(result=CompletionResult(content="ok", provider="s", model="m"))

    with caplog.at_level("WARNING"):
        instrumented_completion(
            provider=provider,
            model="m",
            messages=[ChatMessage(role="user", content="hello")],
            settings=settings,
            conversation_id=str(uuid.uuid4()),
        )

    assert any("rejected log" in record.message for record in caplog.records)
