from __future__ import annotations

import httpx
import pytest

from relay.pricing import PriceBook
from relay.providers.anthropic import AnthropicProvider
from relay.providers.base import (
    AuthError,
    BadRequest,
    ChatMessage,
    CompletionRequest,
    ProviderTimeout,
    RateLimited,
    Unavailable,
)
from relay.providers.openai_compatible import OpenAICompatibleProvider

REQUEST = CompletionRequest(
    messages=[
        ChatMessage("system", "be terse"),
        ChatMessage("user", "hello"),
    ],
    max_tokens=64,
    request_id="t00001",
)


def anthropic_client(handler) -> AnthropicProvider:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="https://api.anthropic.test",
        transport=transport,
        headers={"x-api-key": "k", "anthropic-version": "2023-06-01"},
    )
    return AnthropicProvider("k", client=client, prices=PriceBook())


def openai_client(handler, **kwargs) -> OpenAICompatibleProvider:
    client = httpx.AsyncClient(
        base_url="https://api.openai.test/v1", transport=httpx.MockTransport(handler)
    )
    kwargs.setdefault("name", "openai")
    kwargs.setdefault("models", ("gpt-test",))
    return OpenAICompatibleProvider("k", client=client, prices=PriceBook(), **kwargs)


# ----------------------------------------------------------------------
# Anthropic
# ----------------------------------------------------------------------
async def test_anthropic_parses_a_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-5-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "hi there"}],
                "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
            },
        )

    completion = await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert completion.text == "hi there"
    assert completion.model == "claude-haiku-5-5"
    assert completion.usage.total_tokens == 2_000_000
    # haiku 5.5 is $0.10 in / $0.50 out per 1M tokens
    assert completion.cost_usd == pytest.approx(0.60)
    assert completion.latency_ms > 0


async def test_anthropic_sends_the_system_prompt_as_its_own_field() -> None:
    """Sending it inline as a message is a 400 from this API."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert seen["system"] == "be terse"
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert seen["max_tokens"] == 64


async def test_anthropic_joins_multiple_text_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5-5",
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "part one "},
                    {"type": "text", "text": "part two"},
                ],
                "usage": {"input_tokens": 5, "output_tokens": 4},
            },
        )

    completion = await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert completion.text == "part one part two"


async def test_a_refusal_is_a_completion_not_a_failure() -> None:
    """Failing over would ask three providers the same declined question."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-5-5",
                "stop_reason": "refusal",
                "content": [],
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    completion = await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert completion.finish_reason == "refusal"
    assert completion.text == ""


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, BadRequest),
        (404, BadRequest),
        (422, BadRequest),
        (401, AuthError),
        (403, AuthError),
        (429, RateLimited),
        (500, Unavailable),
        (503, Unavailable),
        (408, Unavailable),
    ],
)
async def test_anthropic_classifies_status_codes(status: int, expected: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"boom {status}"}})

    with pytest.raises(expected) as caught:
        await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert caught.value.status == status
    assert caught.value.elapsed_ms is not None


async def test_only_bad_requests_are_fatal() -> None:
    """Fatal means "no provider can serve this", which 429 and 503 are not."""

    def make(status: int):
        return lambda request: httpx.Response(status, json={"error": {"message": "x"}})

    with pytest.raises(BadRequest) as bad:
        await anthropic_client(make(400)).complete(REQUEST, timeout=5.0)
    assert bad.value.fatal is True

    for status in (429, 503, 401):
        with pytest.raises(Exception) as other:
            await anthropic_client(make(status)).complete(REQUEST, timeout=5.0)
        assert other.value.fatal is False


async def test_retry_after_is_carried_off_the_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error": {"message": "slow down"}}, headers={"retry-after": "7"}
        )

    with pytest.raises(RateLimited) as caught:
        await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert caught.value.retry_after == 7.0


async def test_a_timeout_is_its_own_error_kind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ProviderTimeout):
        await anthropic_client(handler).complete(REQUEST, timeout=0.5)


async def test_a_transport_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(Unavailable, match="transport error"):
        await anthropic_client(handler).complete(REQUEST, timeout=5.0)


async def test_the_adapter_never_retries_by_itself() -> None:
    """Retrying is the gateway's decision; a hidden retry would triple the
    latency budget and hide the failure from the health tracker."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    with pytest.raises(Unavailable):
        await anthropic_client(handler).complete(REQUEST, timeout=5.0)
    assert calls == 1


async def test_anthropic_requires_a_key() -> None:
    with pytest.raises(ValueError, match="api key"):
        AnthropicProvider("")


async def test_aclose_is_safe() -> None:
    provider = anthropic_client(lambda r: httpx.Response(200, json={}))
    await provider.aclose()


# ----------------------------------------------------------------------
# OpenAI-compatible
# ----------------------------------------------------------------------
async def test_openai_parses_a_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [
                    {"message": {"content": "hello back"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    completion = await openai_client(handler).complete(REQUEST, timeout=5.0)
    assert completion.text == "hello back"
    assert completion.usage.input_tokens == 12
    assert completion.finish_reason == "stop"


async def test_openai_keeps_the_system_prompt_inline() -> None:
    """The opposite convention from Anthropic -- the adapter owns that."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    await openai_client(handler).complete(REQUEST, timeout=5.0)
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert "system" not in seen


async def test_the_max_tokens_field_name_is_configurable() -> None:
    """Newer OpenAI models renamed it; compatible gateways did not."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = openai_client(handler, max_tokens_field="max_completion_tokens")
    await provider.complete(REQUEST, timeout=5.0)
    assert seen["max_completion_tokens"] == 64
    assert "max_tokens" not in seen


async def test_an_empty_choices_array_is_an_outage_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "gpt-test", "choices": []})

    with pytest.raises(Unavailable, match="no choices"):
        await openai_client(handler).complete(REQUEST, timeout=5.0)


@pytest.mark.parametrize(
    "status,expected",
    [(400, BadRequest), (401, AuthError), (429, RateLimited), (502, Unavailable)],
)
async def test_openai_classifies_status_codes(status: int, expected: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "nope"}})

    with pytest.raises(expected):
        await openai_client(handler).complete(REQUEST, timeout=5.0)


async def test_openai_needs_at_least_one_model() -> None:
    with pytest.raises(ValueError, match="model id"):
        OpenAICompatibleProvider("k", name="x", models=())


async def test_a_non_json_error_body_is_still_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(Unavailable, match="bad gateway"):
        await openai_client(handler).complete(REQUEST, timeout=5.0)
