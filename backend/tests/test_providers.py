"""Provider adapter tests.

These are what make "multi-provider support" a verifiable claim rather than an
assertion. Anthropic's API is deliberately not OpenAI-shaped, so these tests
prove the adapter interface abstracts a genuinely different wire format —
different auth, different request shape, different response shape, different
streaming events — and that the normalized `CompletionResult` comes out the same
either way.

Responses are stubbed, so the suite runs with no API keys and no network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.sdk.providers import (
    AnthropicProvider,
    ChatMessage,
    MockProvider,
    OpenRouterProvider,
    ProviderError,
    build_provider,
)

MESSAGES = [
    ChatMessage(role="system", content="Be brief."),
    ChatMessage(role="user", content="Hello"),
]

ANTHROPIC_RESPONSE = {
    "id": "msg_123",
    "type": "message",
    "model": "claude-sonnet-4-20250514",
    "content": [
        {"type": "text", "text": "Hi "},
        {"type": "text", "text": "there"},
    ],
    "usage": {"input_tokens": 12, "output_tokens": 3},
}

OPENROUTER_RESPONSE = {
    "id": "gen_123",
    "model": "anthropic/claude-3.5-sonnet",
    "provider": "Anthropic",
    "choices": [{"message": {"role": "assistant", "content": "Hi there"}}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
}


class _Response:
    """Minimal httpx.Response stand-in for the non-streaming path."""

    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Request shape: the two providers must send genuinely different payloads.
# --------------------------------------------------------------------------


def test_anthropic_hoists_system_out_of_messages(monkeypatch):
    """Anthropic rejects role="system" inside messages; it must be a top-level field."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Response(ANTHROPIC_RESPONSE)

    monkeypatch.setattr(httpx, "post", fake_post)

    AnthropicProvider(api_key="k").complete(model="claude-sonnet-4-20250514", messages=MESSAGES)

    payload = captured["json"]
    assert payload["system"] == "Be brief."
    assert all(m["role"] != "system" for m in payload["messages"])
    # Required by the API — omitting it is a 400.
    assert payload["max_tokens"] > 0


def test_anthropic_uses_api_key_header_not_bearer(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json=None, headers=None, timeout=None: (
            captured.update(headers=headers) or _Response(ANTHROPIC_RESPONSE)
        ),
    )

    AnthropicProvider(api_key="secret-key").complete(model="m", messages=MESSAGES)

    assert captured["headers"]["x-api-key"] == "secret-key"
    assert captured["headers"]["anthropic-version"]
    assert "Authorization" not in captured["headers"]


def test_openrouter_uses_bearer_and_keeps_system_inline(monkeypatch):
    """The contrast case: same interface, materially different request."""
    captured = {}

    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json=None, headers=None, timeout=None: (
            captured.update(json=json, headers=headers) or _Response(OPENROUTER_RESPONSE)
        ),
    )

    OpenRouterProvider(api_key="k", base_url="https://openrouter.ai/api/v1").complete(
        model="anthropic/claude-3.5-sonnet", messages=MESSAGES
    )

    assert captured["headers"]["Authorization"] == "Bearer k"
    assert "system" not in captured["json"]
    assert captured["json"]["messages"][0]["role"] == "system"


# --------------------------------------------------------------------------
# Response shape: different wire formats, identical normalized result.
# --------------------------------------------------------------------------


def test_both_providers_normalize_to_the_same_result_shape(monkeypatch):
    """The payoff: callers never learn which provider answered."""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Response(ANTHROPIC_RESPONSE)
    )
    anthropic = AnthropicProvider(api_key="k").complete(model="m", messages=MESSAGES)

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _Response(OPENROUTER_RESPONSE)
    )
    openrouter = OpenRouterProvider(api_key="k", base_url="https://x/v1").complete(
        model="m", messages=MESSAGES
    )

    # Anthropic returns content blocks; OpenRouter returns choices[].message.
    assert anthropic.content == "Hi there"
    assert openrouter.content == "Hi there"

    # Anthropic reports input/output_tokens; OpenRouter prompt/completion_tokens.
    assert anthropic.prompt_tokens == openrouter.prompt_tokens == 12
    assert anthropic.completion_tokens == openrouter.completion_tokens == 3

    # Each records the provider that actually served the call.
    assert anthropic.provider == "anthropic"
    assert openrouter.provider == "Anthropic"


def test_anthropic_joins_multiple_text_blocks(monkeypatch):
    """Content is a block list, not a string — partial joins would truncate replies."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(ANTHROPIC_RESPONSE))

    result = AnthropicProvider(api_key="k").complete(model="m", messages=MESSAGES)

    assert result.content == "Hi there"


def test_anthropic_ignores_non_text_blocks(monkeypatch):
    """Tool-use blocks must not leak into the text content."""
    body = {
        **ANTHROPIC_RESPONSE,
        "content": [
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "t1", "name": "search", "input": {}},
        ],
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response(body))

    assert AnthropicProvider(api_key="k").complete(model="m", messages=MESSAGES).content == "answer"


# --------------------------------------------------------------------------
# Streaming: different event protocols, identical chunk stream.
# --------------------------------------------------------------------------


class _StreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.text = ""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b""


def test_anthropic_stream_parses_typed_events(monkeypatch):
    """Anthropic streams `content_block_delta`, not OpenAI-style `choices` deltas."""
    lines = [
        'data: {"type":"message_start","message":{"model":"claude-sonnet-4-20250514","usage":{"input_tokens":9}}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}',
        'data: {"type":"message_delta","usage":{"output_tokens":2}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _StreamResponse(lines))

    chunks = list(AnthropicProvider(api_key="k").stream(model="m", messages=MESSAGES))

    text = "".join(c.delta for c in chunks if not c.is_final)
    assert text == "Hello"

    final = chunks[-1]
    assert final.is_final
    assert final.prompt_tokens == 9
    assert final.completion_tokens == 2
    assert final.model == "claude-sonnet-4-20250514"


def test_anthropic_stream_surfaces_http_errors(monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _StreamResponse([], status_code=401))

    with pytest.raises(ProviderError, match="401"):
        list(AnthropicProvider(api_key="bad").stream(model="m", messages=MESSAGES))


# --------------------------------------------------------------------------
# Factory: selection is configuration, not code.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_name", "expected_type"),
    [("mock", MockProvider), ("openrouter", OpenRouterProvider), ("anthropic", AnthropicProvider)],
)
def test_factory_resolves_each_provider(provider_name, expected_type):
    settings = Settings(
        llm_provider=provider_name,
        openrouter_api_key="k",
        anthropic_api_key="k",
        database_url="postgresql+psycopg://unused/unused",
    )
    assert isinstance(build_provider(settings), expected_type)


def test_factory_requires_a_key_for_real_providers():
    """Failing loudly at startup beats failing on the first user message."""
    for name in ("openrouter", "anthropic"):
        settings = Settings(
            llm_provider=name,
            openrouter_api_key=None,
            anthropic_api_key=None,
            database_url="postgresql+psycopg://unused/unused",
        )
        with pytest.raises(ProviderError, match="requires"):
            build_provider(settings)


def test_unknown_provider_is_rejected():
    settings = Settings(
        llm_provider="not-a-provider",
        database_url="postgresql+psycopg://unused/unused",
    )
    with pytest.raises(ProviderError, match="Unknown"):
        build_provider(settings)


def test_every_provider_satisfies_the_full_interface():
    """A provider missing `stream` would break only at runtime, under load."""
    for provider_type in (MockProvider, OpenRouterProvider, AnthropicProvider):
        assert callable(getattr(provider_type, "complete", None))
        assert callable(getattr(provider_type, "stream", None))
        assert isinstance(getattr(provider_type, "name", None), str)
