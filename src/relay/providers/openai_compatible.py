"""Adapter for any OpenAI-compatible ``/chat/completions`` endpoint.

One adapter, many providers. OpenAI's chat-completions shape became the de
facto wire format, so the same code serves OpenAI, Groq, Together,
OpenRouter, Fireworks, DeepInfra, vLLM and a local Ollama -- the only
differences are the base URL, the key and the model ids, which are all
configuration. Writing one adapter per vendor would be copying this file and
changing a string.

That is also why the gateway is worth building: providers that are drop-in
compatible on the wire are *not* interchangeable in behaviour, and which one
is fastest right now is an empirical question.
"""

from __future__ import annotations

import time
from typing import Sequence

import httpx

from ..pricing import PriceBook
from .base import (
    AuthError,
    BadRequest,
    Completion,
    CompletionRequest,
    ProviderTimeout,
    RateLimited,
    Unavailable,
    Usage,
)


class OpenAICompatibleProvider:
    """Any endpoint that speaks ``POST /chat/completions``."""

    def __init__(
        self,
        api_key: str,
        *,
        name: str,
        models: Sequence[str],
        base_url: str = "https://api.openai.com/v1",
        prices: PriceBook | None = None,
        client: httpx.AsyncClient | None = None,
        max_tokens_field: str = "max_tokens",
    ) -> None:
        if not models:
            raise ValueError(f"{name}: at least one model id is required")
        self.name = name
        self._models = tuple(models)
        self._prices = prices or PriceBook()
        self._max_tokens_field = max_tokens_field
        """Newer OpenAI models renamed this to ``max_completion_tokens`` while
        compatible gateways kept ``max_tokens``; which one an endpoint wants is
        configuration, not something to guess at per request."""

        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    @property
    def models(self) -> Sequence[str]:
        return self._models

    async def complete(
        self, request: CompletionRequest, *, timeout: float
    ) -> Completion:
        model = request.model or self._models[0]
        payload: dict[str, object] = {
            "model": model,
            # This family carries the system prompt inline as a message, which
            # is the opposite of Anthropic's separate `system` field.
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            self._max_tokens_field: request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/chat/completions", json=payload, timeout=timeout
            )
        except httpx.TimeoutException as error:
            raise ProviderTimeout(
                str(error) or "request timed out",
                provider=self.name,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            ) from error
        except httpx.HTTPError as error:
            raise Unavailable(
                f"transport error: {error}",
                provider=self.name,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            ) from error

        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 400:
            raise self._classify(response, elapsed_ms)

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise Unavailable(
                "response contained no choices",
                provider=self.name,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
        message = choices[0].get("message") or {}
        usage_body = body.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_body.get("prompt_tokens", 0)),
            output_tokens=int(usage_body.get("completion_tokens", 0)),
        )
        served_model = body.get("model", model)
        return Completion(
            text=message.get("content") or "",
            model=served_model,
            provider=self.name,
            usage=usage,
            latency_ms=elapsed_ms,
            cost_usd=self._prices.cost(served_model, usage),
            finish_reason=choices[0].get("finish_reason") or "stop",
        )

    def _classify(self, response: httpx.Response, elapsed_ms: float):
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text[:400]

        status = response.status_code
        kwargs = {
            "provider": self.name,
            "status": status,
            "retry_after": _retry_after(response),
            "elapsed_ms": elapsed_ms,
        }
        if status in (401, 403):
            return AuthError(detail, **kwargs)
        if status == 429:
            return RateLimited(detail, **kwargs)
        if status >= 500 or status in (408, 409):
            return Unavailable(detail, **kwargs)
        return BadRequest(detail, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
