"""Provider adapters behind a single `get_completion()` interface.

Adding a provider means adding one class here. Nothing in the logging layer,
the API layer, or the frontend changes — which is what makes the wrapper in
`logging.py` "automatic" instrumentation rather than per-provider glue.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class CompletionResult:
    """Normalized provider response.

    `provider` is what actually served the request, which for an aggregator
    like OpenRouter is not the same as the adapter name.
    """

    content: str
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class ProviderError(RuntimeError):
    """Raised when a provider call fails. Carries a message safe to log."""


class Provider(Protocol):
    name: str

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult: ...


class MockProvider:
    """Deterministic local provider so the stack runs with no API key.

    Echoes back a summary of the conversation so multi-turn context is visibly
    working in the UI without spending tokens.
    """

    name = "mock"

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult:
        user_messages = [m for m in messages if m.role == "user"]
        latest = user_messages[-1].content if user_messages else ""
        turn_count = len(user_messages)

        content = (
            f'You said: "{latest}"\n\n'
            f"This is turn {turn_count} of our conversation, and I can see "
            f"{len(messages)} message(s) of context. "
            "I'm the mock provider — set LLM_PROVIDER=openrouter with an API key "
            "for real completions."
        )

        # Rough word-based estimate; real providers report exact usage.
        prompt_tokens = sum(len(m.content.split()) for m in messages)
        return CompletionResult(
            content=content,
            provider=self.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=len(content.split()),
        )


class OpenRouterProvider:
    """OpenAI-compatible aggregator. One key, many upstream models."""

    name = "openrouter"

    def __init__(self, api_key: str, base_url: str, timeout: float = 60.0) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult:
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"OpenRouter returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenRouter request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"OpenRouter returned non-JSON body: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Unexpected OpenRouter response shape: {str(body)[:200]}") from exc

        usage = body.get("usage") or {}
        return CompletionResult(
            content=content,
            # OpenRouter reports the upstream provider that served the request.
            provider=body.get("provider") or self.name,
            model=body.get("model") or model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


def build_provider(settings: Settings) -> Provider:
    """Resolve the configured provider, failing loudly on bad configuration."""
    name = settings.llm_provider.lower()

    if name == "mock":
        return MockProvider()

    if name == "openrouter":
        api_key = settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ProviderError(
                "LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY to be set."
            )
        return OpenRouterProvider(api_key=api_key, base_url=settings.openrouter_base_url)

    raise ProviderError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
