"""Anthropic Messages API adapter.

Raw HTTP on purpose -- see :mod:`relay.providers.base` for why a gateway does
not want a stack of vendor SDKs. The adapter's whole job is to translate, in
both directions, and to classify failures so the router can act on them.

Two details that are easy to get wrong:

* **The system prompt is a separate field**, not a message with
  ``role: "system"``. Sending it inline is a 400.
* **A refusal is a successful HTTP response.** ``stop_reason: "refusal"``
  arrives as a 200 with a policy decision attached. Treating it as an error
  would make the gateway fail over and ask three providers the same question
  it has already been declined once -- so it is returned as a completion whose
  ``finish_reason`` says what happened, and the caller decides.
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

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")


class AnthropicProvider:
    """One Anthropic account, serving one or more models."""

    def __init__(
        self,
        api_key: str,
        *,
        name: str = "anthropic",
        models: Sequence[str] = DEFAULT_MODELS,
        base_url: str = DEFAULT_BASE_URL,
        prices: PriceBook | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("anthropic provider needs an api key")
        self.name = name
        self._models = tuple(models)
        self._prices = prices or PriceBook()
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
            # No transport-level retries: retrying is the gateway's decision,
            # and a silent retry here would hide a failure from the router and
            # spend the attempt's latency budget three times over.
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
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.turns()
            ],
        }
        system = request.system_prompt()
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/v1/messages", json=payload, timeout=timeout
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
        text = "".join(
            block.get("text", "")
            for block in body.get("content", [])
            if block.get("type") == "text"
        )
        usage_body = body.get("usage", {})
        usage = Usage(
            input_tokens=int(usage_body.get("input_tokens", 0)),
            output_tokens=int(usage_body.get("output_tokens", 0)),
        )
        served_model = body.get("model", model)
        return Completion(
            text=text,
            model=served_model,
            provider=self.name,
            usage=usage,
            latency_ms=elapsed_ms,
            cost_usd=self._prices.cost(served_model, usage),
            finish_reason=body.get("stop_reason") or "end_turn",
        )

    def _classify(self, response: httpx.Response, elapsed_ms: float):
        try:
            detail = response.json().get("error", {}).get("message", response.text)
        except ValueError:
            detail = response.text[:400]

        status = response.status_code
        retry_after = _retry_after(response)
        kwargs = {
            "provider": self.name,
            "status": status,
            "retry_after": retry_after,
            "elapsed_ms": elapsed_ms,
        }
        if status in (401, 403):
            return AuthError(detail, **kwargs)
        if status == 429:
            return RateLimited(detail, **kwargs)
        if status >= 500:
            return Unavailable(detail, **kwargs)
        if status in (408, 409):
            return Unavailable(detail, **kwargs)
        # 400, 404, 413, 422: the request is the problem, so no provider can
        # serve it and failing over would just buy the same error twice.
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
