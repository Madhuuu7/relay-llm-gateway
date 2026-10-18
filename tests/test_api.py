from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.api.app import create_app, get_runtime, reset_runtime


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A demo-mode app: no API keys, synthetic providers, temp database."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("RELAY_POLICY", "adaptive")
    reset_runtime()
    with TestClient(create_app()) as test_client:
        # The synthetic providers sleep for real in the app; shortcut that so
        # the suite does not wait for simulated latency.
        runtime = get_runtime()
        for candidate in runtime.gateway.candidates:
            candidate.provider.sleep = _instant
        yield test_client
    reset_runtime()


async def _instant(_seconds: float) -> None:
    return None


def completion(client: TestClient, **overrides):
    body = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 32}
    body.update(overrides)
    return client.post("/v1/chat/completions", json=body)


# ----------------------------------------------------------------------
def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_demo_mode_boots_without_credentials(client: TestClient) -> None:
    """A gateway with nothing to route to demonstrates nothing."""
    assert client.get("/stats").json()["demo_mode"] is True


def test_completion_uses_the_openai_shape(client: TestClient) -> None:
    body = completion(client).json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["usage"]["total_tokens"] > 0
    assert body["id"].startswith("chatcmpl_")


def test_the_response_says_who_served_it(client: TestClient) -> None:
    meta = completion(client).json()["x_relay"]
    assert meta["provider"].startswith("sim-")
    assert meta["policy"] == "adaptive"
    assert meta["latency_ms"] > 0
    assert isinstance(meta["attempts"], list)
    assert meta["attempts"][-1]["ok"] is True


def test_messages_are_required(client: TestClient) -> None:
    response = client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 422


def test_max_tokens_is_bounded(client: TestClient) -> None:
    assert completion(client, max_tokens=0).status_code == 422
    assert completion(client, max_tokens=10**7).status_code == 422


def test_streaming_is_refused_honestly(client: TestClient) -> None:
    """Rather than a fake one-chunk stream that hides the real problem."""
    response = completion(client, stream=True)
    assert response.status_code == 501
    assert "mid-stream" in response.json()["detail"]


def test_models_lists_providers_and_prices(client: TestClient) -> None:
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    ids = {entry["id"] for entry in body["data"]}
    assert "sim-fast" in ids
    priced = next(e for e in body["data"] if e["id"] == "sim-fast")
    assert priced["input_per_mtok"] == pytest.approx(0.50)
    assert priced["owned_by"] == "sim-fast"


def test_stats_reports_live_state_and_history(client: TestClient) -> None:
    for _ in range(5):
        completion(client)
    body = client.get("/stats").json()

    assert body["totals"]["requests"] == 5
    assert body["live"]["policy"] == "adaptive"
    assert len(body["live"]["providers"]) == 3
    assert len(body["live"]["ranking"]) == 3
    assert body["by_provider"]
    assert len(body["timeline"]) == 30
    assert "adaptive" in body["policies"]


def test_stats_window_filters_history(client: TestClient) -> None:
    completion(client)
    assert client.get("/stats", params={"window": 3600}).json()["totals"]["requests"] == 1
    assert client.get("/stats", params={"window": 0}).json()["totals"]["requests"] == 0


def test_request_log(client: TestClient) -> None:
    completion(client)
    rows = client.get("/requests", params={"limit": 5}).json()
    assert len(rows) == 1
    assert rows[0]["ok"] is True
    assert isinstance(rows[0]["attempts"], list)

    assert client.get("/requests", params={"failures_only": True}).json() == []


def test_policy_can_be_swapped_at_runtime(client: TestClient) -> None:
    """Why the policy is a strategy object and not a branch in the gateway."""
    assert client.post("/admin/policy/round_robin").json() == {"policy": "round_robin"}
    assert completion(client).json()["x_relay"]["policy"] == "round_robin"
    assert client.post("/admin/policy/vibes").status_code == 404


def test_breaker_can_be_reset_by_an_operator(client: TestClient) -> None:
    body = client.post("/admin/breaker/sim-fast/reset").json()
    assert body["provider"] == "sim-fast"
    assert body["state"] == "closed"
    assert client.post("/admin/breaker/nope/reset").status_code == 404


def test_demo_load_generates_traffic(client: TestClient) -> None:
    body = client.post("/demo/load", params={"requests": 12, "concurrency": 4}).json()
    assert body["generated"] == 12
    assert body["served"] + body["failed"] == 12
    assert client.get("/stats").json()["totals"]["requests"] >= 12


def test_demo_load_is_disabled_with_real_providers(client: TestClient) -> None:
    """An unauthenticated endpoint must not be able to spend real money."""
    get_runtime().demo = False
    assert client.post("/demo/load").status_code == 403


def test_openapi_is_served(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/v1/chat/completions" in schema["paths"]
    assert "/stats" in schema["paths"]


def test_dashboard_is_served_at_the_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
