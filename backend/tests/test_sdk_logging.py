"""Tests for the instrumentation wrapper.

The wrapper is the component whose failures are invisible at runtime — it
deliberately swallows transport errors so logging can't break chat. These tests
are what stop that from hiding a real regression.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from app.config import Settings
from app.telemetry.instrument import instrumented_completion
from app.llm.providers import ChatMessage, CompletionResult, ProviderError


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll until `predicate()` is true.

    Waiting on a condition rather than on an empty pool queue: the delivery pool
    is process-wide, so "queue is empty" can be satisfied by another suite's
    work and produces flaky passes when suites run together.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def settings() -> Settings:
    # Pinned explicitly: other suites set SPOOL_* in the process environment,
    # and Settings() would otherwise pick them up and silently change which
    # delivery path is under test.
    return Settings(
        database_url="postgresql+psycopg://unused/unused",
        ingest_url="http://ingest.invalid/ingest",
        preview_max_chars=50,
        spool_enabled=False,
    )


@pytest.fixture
def captured(settings) -> list[dict]:
    """Capture emitted log events instead of sending them over HTTP."""
    import llmlog
    from app.telemetry.instrument import configure_telemetry

    configure_telemetry(settings)
    events: list[dict] = []
    llmlog.get_client().emit = events.append
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
        conversation_id=str(uuid.uuid4()),
    )

    assert result.content == "still works"


def test_delivery_does_not_block_the_caller(settings, monkeypatch):
    """Regression: synchronous delivery deadlocked streaming requests.

    The wrapper POSTs to an endpoint served by the same process. Doing that
    inline from inside a streaming request meant the single worker was busy
    serving the stream and could not answer its own request — the POST timed
    out and the log was lost, with no visible error.
    """
    import httpx

    release = threading.Event()

    def slow_post(*args, **kwargs):
        # Simulates an endpoint that cannot respond while the caller is blocked.
        # Bounded tightly: this occupies a worker in the process-wide delivery
        # pool, and holding it longer starves tests that run afterwards.
        release.wait(timeout=2)
        raise httpx.ConnectError("would have deadlocked")

    monkeypatch.setattr(httpx, "post", slow_post)

    provider = StubProvider(result=CompletionResult(content="ok", provider="s", model="m"))

    start = time.perf_counter()
    result = instrumented_completion(
        provider=provider,
        model="m",
        messages=[ChatMessage(role="user", content="hello")],
        conversation_id=str(uuid.uuid4()),
    )
    elapsed = time.perf_counter() - start
    release.set()

    assert result.content == "ok"
    # The caller must return immediately rather than waiting on delivery.
    assert elapsed < 1.0, f"emit blocked the caller for {elapsed:.2f}s"


def test_non_2xx_ingest_response_is_not_treated_as_delivered(settings, monkeypatch):
    """A misrouted endpoint returning 4xx must not look like a successful delivery.

    Asserts the return contract rather than a log line: `_post` returns True
    only when the event reached a terminal state, and log capture proved
    unreliable across suites that reconfigure the root logger.

    The distinction that matters:
      - 4xx (not 408/429) -> terminal; the payload will never be accepted
      - 5xx / transport   -> retryable; the spool keeps the event
    """
    import httpx

    from llmlog.client import LogClient
    from llmlog.config import LogConfig

    def responding(status: int):
        class FakeResponse:
            status_code = status
            text = "body"

        return lambda *a, **k: FakeResponse()

    client = LogClient(
        LogConfig(ingest_url="http://ingest.invalid/ingest", spool_enabled=False)
    )

    # Terminal: discard rather than retry forever.
    monkeypatch.setattr(httpx, "post", responding(400))
    assert client.post({"id": "a"}) is True

    monkeypatch.setattr(httpx, "post", responding(422))
    assert client.post({"id": "b"}) is True

    # Retryable: the event must be kept for a later attempt.
    monkeypatch.setattr(httpx, "post", responding(503))
    assert client.post({"id": "c"}) is False

    monkeypatch.setattr(httpx, "post", responding(429))
    assert client.post({"id": "d"}) is False, "rate limiting is temporary"

    monkeypatch.setattr(httpx, "post", responding(202))
    assert client.post({"id": "e"}) is True
