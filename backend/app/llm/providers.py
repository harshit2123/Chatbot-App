"""Provider adapters behind a single `get_completion()` interface.

Adding a provider means adding one class here. Nothing in the logging layer,
the API layer, or the frontend changes — which is what makes the wrapper in
`logging.py` "automatic" instrumentation rather than per-provider glue.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
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


@dataclass(frozen=True)
class StreamChunk:
    """One token/delta from a streaming response.

    The final chunk carries `usage`, since token counts are only known once the
    upstream stream completes.
    """

    delta: str
    provider: str
    model: str
    is_final: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class Provider(Protocol):
    name: str

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult: ...

    def stream(self, model: str, messages: list[ChatMessage]) -> Iterator[StreamChunk]: ...


class MockProvider:
    """Deterministic local provider so the stack runs with no API key.

    Echoes back a summary of the conversation so multi-turn context is visibly
    working in the UI without spending tokens.
    """

    name = "mock"

    # Simulated per-token delay so streaming is visibly incremental in the UI
    # and cancellation has a window in which to take effect.
    chunk_delay_seconds = 0.04

    def _build_reply(self, model: str, messages: list[ChatMessage]) -> str:
        user_messages = [m for m in messages if m.role == "user"]
        latest = user_messages[-1].content if user_messages else ""
        turn_count = len(user_messages)

        return (
            f'You said: "{latest}"\n\n'
            f"This is turn {turn_count} of our conversation, and I can see "
            f"{len(messages)} message(s) of context. "
            "I'm the mock provider — set LLM_PROVIDER=openrouter with an API key "
            "for real completions."
        )

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult:
        content = self._build_reply(model, messages)

        # Rough word-based estimate; real providers report exact usage.
        prompt_tokens = sum(len(m.content.split()) for m in messages)
        return CompletionResult(
            content=content,
            provider=self.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=len(content.split()),
        )

    def stream(self, model: str, messages: list[ChatMessage]) -> Iterator[StreamChunk]:
        import time

        content = self._build_reply(model, messages)
        words = content.split(" ")
        prompt_tokens = sum(len(m.content.split()) for m in messages)

        for index, word in enumerate(words):
            # Preserve spacing so the reassembled text matches `complete()`.
            delta = word if index == 0 else f" {word}"
            yield StreamChunk(delta=delta, provider=self.name, model=model)
            time.sleep(self.chunk_delay_seconds)

        yield StreamChunk(
            delta="",
            provider=self.name,
            model=model,
            is_final=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=len(words),
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

    def stream(self, model: str, messages: list[ChatMessage]) -> Iterator[StreamChunk]:
        """Parse OpenRouter's SSE stream into normalized chunks."""
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        resolved_provider = self.name
        resolved_model = model
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(
                        f"OpenRouter returned {response.status_code}: {response.text[:200]}"
                    )

                for line in response.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue

                    data = line[len("data: ") :].strip()
                    if data == "[DONE]":
                        break

                    try:
                        event = json.loads(data)
                    except ValueError:
                        # Keepalive or partial frame; skip rather than fail the stream.
                        continue

                    resolved_provider = event.get("provider") or resolved_provider
                    resolved_model = event.get("model") or resolved_model

                    if usage := event.get("usage"):
                        prompt_tokens = usage.get("prompt_tokens")
                        completion_tokens = usage.get("completion_tokens")

                    choices = event.get("choices") or []
                    if not choices:
                        continue

                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield StreamChunk(
                            delta=delta, provider=resolved_provider, model=resolved_model
                        )
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenRouter stream failed: {exc}") from exc

        yield StreamChunk(
            delta="",
            provider=resolved_provider,
            model=resolved_model,
            is_final=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


class AnthropicProvider:
    """Anthropic's native Messages API.

    Deliberately not OpenAI-shaped, and that is the point: it proves the adapter
    interface abstracts a genuinely different API rather than assuming every
    provider speaks the same dialect. Differences handled here:

      - auth via `x-api-key` + `anthropic-version`, not a Bearer token
      - `system` is a top-level field, not a message with role="system"
      - `max_tokens` is required, not optional
      - responses carry a `content` block list, not `choices[].message.content`
      - usage keys are `input_tokens`/`output_tokens`
      - streaming emits typed events (`content_block_delta`), not `choices` deltas
    """

    name = "anthropic"

    API_VERSION = "2023-06-01"
    # Anthropic requires this; the value bounds the reply, it does not reserve cost.
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

    def _payload(self, model: str, messages: list[ChatMessage], stream: bool) -> dict:
        # Anthropic rejects role="system" inside messages; it must be hoisted.
        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]

        payload: dict = {
            "model": model,
            "messages": turns,
            "max_tokens": self.DEFAULT_MAX_TOKENS,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    def complete(self, model: str, messages: list[ChatMessage]) -> CompletionResult:
        try:
            response = httpx.post(
                f"{self._base_url}/messages",
                json=self._payload(model, messages, stream=False),
                headers=self._headers(),
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Anthropic returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"Anthropic returned non-JSON body: {exc}") from exc

        # Content is a list of typed blocks; join the text ones.
        try:
            content = "".join(
                block.get("text", "")
                for block in body.get("content", [])
                if block.get("type") == "text"
            )
        except (AttributeError, TypeError) as exc:
            raise ProviderError(f"Unexpected Anthropic response shape: {str(body)[:200]}") from exc

        usage = body.get("usage") or {}
        return CompletionResult(
            content=content,
            provider=self.name,
            model=body.get("model") or model,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
        )

    def stream(self, model: str, messages: list[ChatMessage]) -> Iterator[StreamChunk]:
        resolved_model = model
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/messages",
                json=self._payload(model, messages, stream=True),
                headers=self._headers(),
                timeout=self._timeout,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise ProviderError(
                        f"Anthropic returned {response.status_code}: {response.text[:200]}"
                    )

                for line in response.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue

                    try:
                        event = json.loads(line[len("data: ") :])
                    except ValueError:
                        continue

                    event_type = event.get("type")

                    if event_type == "message_start":
                        message = event.get("message") or {}
                        resolved_model = message.get("model") or resolved_model
                        prompt_tokens = (message.get("usage") or {}).get("input_tokens")
                    elif event_type == "content_block_delta":
                        text = (event.get("delta") or {}).get("text")
                        if text:
                            yield StreamChunk(
                                delta=text, provider=self.name, model=resolved_model
                            )
                    elif event_type == "message_delta":
                        # Output tokens are only final on this event.
                        completion_tokens = (event.get("usage") or {}).get("output_tokens")
                    elif event_type == "message_stop":
                        break
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic stream failed: {exc}") from exc

        yield StreamChunk(
            delta="",
            provider=self.name,
            model=resolved_model,
            is_final=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
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

    if name == "anthropic":
        api_key = settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ProviderError(
                "LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set."
            )
        return AnthropicProvider(api_key=api_key, base_url=settings.anthropic_base_url)

    raise ProviderError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
