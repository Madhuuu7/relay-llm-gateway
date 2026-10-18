"""HTTP surface: an OpenAI-compatible endpoint plus the observability views.

The completion endpoint deliberately speaks ``POST /v1/chat/completions`` with
the OpenAI request and response shape. That is the whole adoption story -- an
existing application changes one base URL and gains fail-over, health-aware
routing and cost accounting without touching its own code. A bespoke schema
would be nicer to design and would mean rewriting every caller.

The response carries one addition: an ``x_relay`` block saying which provider
served it, what the attempts were, and what it cost. Anything that does not
understand the extra key ignores it, and anyone debugging a slow request can
see the fail-over chain without reading server logs.

Providers come from the environment. With no API keys configured the app
boots a **demo** set of synthetic providers so the dashboard and the deployed
link work without credentials -- a gateway with nothing to route to is not a
demonstration of anything.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..breaker import BreakerConfig
from ..pricing import PriceBook
from ..providers.base import ChatMessage, CompletionRequest
from ..providers.synthetic import Profile, SyntheticProvider, real_sleep
from ..routing.gateway import Gateway, GatewayConfig, GatewayError
from ..routing.policies import POLICIES, Adaptive, AdaptiveConfig, build_policy
from ..store import RequestStore

WEB_ROOT = Path(__file__).resolve().parents[3] / "web"


# ----------------------------------------------------------------------
# wire models (OpenAI-compatible)
# ----------------------------------------------------------------------
class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    model: str | None = None
    max_tokens: int = Field(default=1024, ge=1, le=200_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stream: bool = False


class Choice(BaseModel):
    index: int = 0
    message: Message
    finish_reason: str = "stop"


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class RelayMeta(BaseModel):
    """The gateway's own account of how the request was served."""

    provider: str
    policy: str
    latency_ms: float
    cost_usd: float
    failovers: int
    attempts: list[dict[str, object]]


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: UsageOut
    x_relay: RelayMeta


# ----------------------------------------------------------------------
# wiring
# ----------------------------------------------------------------------
class Runtime:
    """Everything the app holds for its lifetime."""

    def __init__(self) -> None:
        self.prices = PriceBook.from_env()
        self.store = RequestStore(os.environ.get("RELAY_DB", "relay.db"))
        self.demo = False
        providers = self._build_providers()
        policy_name = os.environ.get("RELAY_POLICY", "adaptive")
        policy = (
            Adaptive(config=AdaptiveConfig(), prices=self.prices)
            if policy_name == "adaptive"
            else build_policy(policy_name)
        )
        self.gateway = Gateway(
            providers,
            policy,
            prices=self.prices,
            config=GatewayConfig(
                attempt_timeout=float(os.environ.get("RELAY_ATTEMPT_TIMEOUT", 20.0)),
                total_budget=float(os.environ.get("RELAY_TOTAL_BUDGET", 45.0)),
                max_attempts=int(os.environ.get("RELAY_MAX_ATTEMPTS", 3)),
            ),
            breaker_config=BreakerConfig(
                failure_threshold=int(os.environ.get("RELAY_FAILURE_THRESHOLD", 5)),
                cooldown_seconds=float(os.environ.get("RELAY_COOLDOWN", 10.0)),
            ),
            on_result=self.store.record,
        )

    def _build_providers(self) -> list:
        """Real providers when keys exist, synthetic ones otherwise."""
        providers: list = []

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            from ..providers.anthropic import AnthropicProvider

            providers.append(AnthropicProvider(anthropic_key, prices=self.prices))

        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            from ..providers.openai_compatible import OpenAICompatibleProvider

            providers.append(
                OpenAICompatibleProvider(
                    openai_key,
                    name=os.environ.get("OPENAI_NAME", "openai"),
                    models=tuple(
                        m.strip()
                        for m in os.environ.get("OPENAI_MODELS", "gpt-4o-mini").split(",")
                        if m.strip()
                    ),
                    base_url=os.environ.get(
                        "OPENAI_BASE_URL", "https://api.openai.com/v1"
                    ),
                    prices=self.prices,
                )
            )

        if providers:
            return providers

        # No credentials: the demo set. Latency is really slept here, so the
        # dashboard shows something that behaves like traffic.
        self.demo = True
        return [
            SyntheticProvider(Profile.fast_but_flaky(), seed=1, sleep=real_sleep, prices=self.prices),
            SyntheticProvider(Profile.slow_but_solid(), seed=1, sleep=real_sleep, prices=self.prices),
            SyntheticProvider(Profile.cheap_with_a_bad_tail(), seed=1, sleep=real_sleep, prices=self.prices),
        ]

    async def aclose(self) -> None:
        await self.gateway.aclose()
        self.store.close()


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def reset_runtime() -> None:
    """Drop the process-wide runtime. For tests and for a config reload."""
    global _runtime
    _runtime = None


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


# ----------------------------------------------------------------------
def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if _runtime is not None:
            await _runtime.aclose()

    app = FastAPI(
        title="relay",
        version="0.1.0",
        description=(
            "An LLM gateway that routes on live latency and error rates. "
            "OpenAI-compatible: point an existing client at /v1 and it gains "
            "fail-over, health-aware routing and cost accounting."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
    async def chat_completions(
        body: ChatCompletionRequest, runtime: RuntimeDep
    ) -> ChatCompletionResponse:
        if body.stream:
            # Honest 501 rather than a fake single-chunk stream: routing a
            # stream means deciding what to do when a provider fails after
            # the first token has already been sent to the client, and that
            # is a design question, not a parameter.
            raise HTTPException(
                501,
                "streaming is not implemented: a provider that fails mid-stream "
                "cannot be failed over transparently once bytes have been sent",
            )

        request = CompletionRequest(
            messages=[ChatMessage(m.role, m.content) for m in body.messages],
            model=body.model,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
        )
        try:
            result = await runtime.gateway.complete(request)
        except GatewayError as error:
            raise HTTPException(
                502, {"message": "every provider failed", "attempts": error.result.as_dict()["attempts"]}
            ) from None

        completion = result.completion
        assert completion is not None
        return ChatCompletionResponse(
            id=f"chatcmpl_{uuid.uuid4().hex[:20]}",
            created=int(time.time()),
            model=completion.model,
            choices=[
                Choice(
                    message=Message(role="assistant", content=completion.text),
                    finish_reason=completion.finish_reason,
                )
            ],
            usage=UsageOut(
                prompt_tokens=completion.usage.input_tokens,
                completion_tokens=completion.usage.output_tokens,
                total_tokens=completion.usage.total_tokens,
            ),
            x_relay=RelayMeta(
                provider=completion.provider,
                policy=result.policy,
                latency_ms=round(result.latency_ms, 1),
                cost_usd=round(result.cost_usd, 8),
                failovers=result.failovers,
                attempts=[
                    {
                        "provider": a.provider,
                        "ok": a.ok,
                        "latency_ms": round(a.latency_ms, 1),
                        "error": a.error,
                        "skipped": a.skipped,
                    }
                    for a in result.attempts
                ],
            ),
        )

    @app.get("/v1/models")
    def models(runtime: RuntimeDep) -> dict[str, object]:
        data = []
        for candidate in runtime.gateway.candidates:
            for model in candidate.provider.models:
                price = runtime.prices.price_of(model)
                data.append(
                    {
                        "id": model,
                        "object": "model",
                        "owned_by": candidate.name,
                        "input_per_mtok": price.input_per_mtok if price else None,
                        "output_per_mtok": price.output_per_mtok if price else None,
                    }
                )
        return {"object": "list", "data": data}

    @app.get("/stats")
    def stats(
        runtime: RuntimeDep,
        window: float | None = Query(None, description="seconds; omit for all time"),
    ) -> dict[str, object]:
        return {
            "demo_mode": runtime.demo,
            "live": runtime.gateway.snapshot(),
            "totals": runtime.store.totals(since_seconds=window),
            "by_provider": [
                {
                    "provider": r.provider,
                    "requests": r.requests,
                    "failures": r.failures,
                    "p50_ms": r.p50_ms,
                    "p95_ms": r.p95_ms,
                    "cost_usd": r.cost_usd,
                    "tokens": r.tokens,
                }
                for r in runtime.store.by_provider(since_seconds=window)
            ],
            "timeline": runtime.store.timeline(buckets=30, bucket_seconds=10.0),
            "policies": sorted(POLICIES),
        }

    @app.get("/requests")
    def requests_log(
        runtime: RuntimeDep,
        limit: int = Query(50, ge=1, le=500),
        failures_only: bool = False,
    ) -> list[dict[str, object]]:
        return (
            runtime.store.failures(limit)
            if failures_only
            else runtime.store.recent(limit)
        )

    @app.post("/admin/policy/{name}")
    def set_policy(name: str, runtime: RuntimeDep) -> dict[str, str]:
        """Swap the routing policy at runtime.

        Useful for a live A/B on the dashboard, and the reason the policy is a
        strategy object rather than a branch inside the gateway.
        """
        try:
            runtime.gateway.policy = (
                Adaptive(config=AdaptiveConfig(), prices=runtime.prices)
                if name == "adaptive"
                else build_policy(name)
            )
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        return {"policy": runtime.gateway.policy.name}

    @app.post("/admin/breaker/{provider}/reset")
    def reset_breaker(provider: str, runtime: RuntimeDep) -> dict[str, object]:
        try:
            candidate = runtime.gateway.candidate(provider)
        except KeyError:
            raise HTTPException(404, f"unknown provider: {provider}") from None
        candidate.breaker.reset()
        return candidate.breaker.snapshot()

    @app.post("/demo/load")
    async def demo_load(
        runtime: RuntimeDep,
        requests: int = Query(25, ge=1, le=500),
        concurrency: int = Query(5, ge=1, le=50),
    ) -> dict[str, object]:
        """Generate traffic, so the dashboard has something to show.

        Available in demo mode only -- on a deployment with real keys this
        would spend real money, which is not a thing an unauthenticated
        endpoint should be able to do.
        """
        if not runtime.demo:
            raise HTTPException(
                403, "load generation is disabled when real providers are configured"
            )

        semaphore = asyncio.Semaphore(concurrency)
        served = 0
        failed = 0

        async def one(index: int) -> None:
            nonlocal served, failed
            async with semaphore:
                request = CompletionRequest(
                    messages=[ChatMessage("user", f"demo request {index}")],
                    max_tokens=128,
                    request_id=f"demo{int(time.time())}{index:04d}",
                )
                try:
                    await runtime.gateway.complete(request)
                    served += 1
                except GatewayError:
                    failed += 1

        await asyncio.gather(*(one(i) for i in range(requests)))
        return {"generated": requests, "served": served, "failed": failed}

    if WEB_ROOT.is_dir():
        app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")

    return app


app = create_app()
