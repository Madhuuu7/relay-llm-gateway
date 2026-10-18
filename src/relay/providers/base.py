"""The provider interface every backend implements.

A gateway's whole value is that one request shape works across vendors, so
adapters speak **raw HTTP** rather than each vendor's SDK. Three reasons:

1. Installing several vendor SDKs in one process means several HTTP stacks,
   several retry policies arguing with the gateway's own, and a dependency
   conflict every time one of them bumps a pin.
2. The gateway needs the wire-level facts the SDKs smooth over -- the exact
   status code, the ``retry-after`` header, time-to-first-byte -- because
   those are what routing decisions are made of.
3. An adapter is ~60 lines. An SDK wrapper around an SDK is not simpler.

Errors are classified once, here, into retryable and not. That distinction is
the contract the router depends on: retrying a malformed request burns the
latency budget to produce the same 400, while *not* retrying a 503 throws away
the only reason the gateway exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class CompletionRequest:
    """One logical completion, independent of which provider serves it."""

    messages: Sequence[ChatMessage]
    model: str | None = None
    """A concrete model id, or None to let the gateway pick per provider."""

    max_tokens: int = 1024
    temperature: float | None = None
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:16]}")

    def system_prompt(self) -> str | None:
        parts = [m.content for m in self.messages if m.role == "system"]
        return "\n\n".join(parts) if parts else None

    def turns(self) -> list[ChatMessage]:
        """Messages minus system turns, which some APIs carry separately."""
        return [m for m in self.messages if m.role != "system"]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Completion:
    """A provider's answer, normalised."""

    text: str
    model: str
    provider: str
    usage: Usage
    latency_ms: float
    cost_usd: float = 0.0
    finish_reason: str = "stop"


class ProviderError(Exception):
    """Base for provider failures.

    ``retryable`` decides whether the gateway may fail over. It is a property
    of the *error*, not of the provider: a 400 from the fastest provider is
    still a 400 everywhere else.
    """

    retryable: bool = True

    fatal: bool = False
    """True when no provider can serve this request -- the gateway gives up
    instead of failing over, because the fault is in the request."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = None,
        retry_after: float | None = None,
        elapsed_ms: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retry_after = retry_after
        self.elapsed_ms = elapsed_ms
        """Time this failure burned. Carried on the error because a failed
        attempt still costs latency, and a simulated run has no wall clock to
        measure it with."""

    def __str__(self) -> str:
        where = f"{self.provider}: " if self.provider else ""
        code = f" (HTTP {self.status})" if self.status else ""
        return f"{where}{super().__str__()}{code}"


class BadRequest(ProviderError):
    """The request itself is wrong. Failing over cannot help."""

    retryable = False
    fatal = True


class AuthError(ProviderError):
    """Bad or missing credentials. Another provider may still work, but
    retrying *this* one will not."""

    retryable = False


class RateLimited(ProviderError):
    """429. Retryable elsewhere, and immediately."""


class Unavailable(ProviderError):
    """5xx, connection refused, DNS failure."""


class ProviderTimeout(ProviderError):
    """The provider did not answer inside the per-attempt budget."""


@runtime_checkable
class Provider(Protocol):
    """What the router needs from a backend."""

    name: str

    @property
    def models(self) -> Sequence[str]:
        """Model ids this provider serves, best first."""

    async def complete(
        self, request: CompletionRequest, *, timeout: float
    ) -> Completion:
        """Run one completion or raise a :class:`ProviderError`.

        Implementations must not retry internally. Retrying is the gateway's
        job, and a provider that quietly retries three times turns one
        attempt's latency budget into three and hides the failure from the
        health tracker.
        """

    async def aclose(self) -> None:
        """Release connections."""
