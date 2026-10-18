# relay

An LLM gateway that picks a provider from **live latency, error rate and queue depth** instead of a
static fallback list — and a benchmark that shows the difference is real.

OpenAI-compatible: point an existing client at `/v1` and it gains fail-over, health-aware routing
and per-request cost accounting without touching its own code.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
client.chat.completions.create(model="claude-sonnet-5-5", messages=[...])
```

---

## The claim, measured

Nearly every gateway ships the same routing rule: an ordered list, try the next one when the current
one fails. That rule is fine when a provider is *down* and bad when a provider is *degraded* — a
provider that is slow but still returning 200s stays at the head of the list forever, and every
request pays its tail.

`scripts/replay.py` tests that. One trace of 4,000 requests is replayed through all four policies
against **identical provider behaviour** — latency and failure for a given `(provider, position)`
are hash-derived, so the only difference between two runs is where each request was sent. Three
providers, drawn so no static order is right for all of them; the fast one takes an outage over
positions 1600–2400.

| policy | p50 | p95 | p99 | served | fail-overs | cost | breaker trips |
|---|---|---|---|---|---|---|---|
| static | 280 ms | 4640 ms | 4917 ms | 99.98% | 404 | $3.395 | 209 |
| round_robin | 617 ms | 4776 ms | 5522 ms | 99.98% | 372 | $3.273 | 103 |
| random | 609 ms | 4663 ms | 5855 ms | 99.98% | 344 | $3.112 | 84 |
| **adaptive** | **296 ms** | **1308 ms** | 5168 ms | 99.98% | **271** | $3.210 | **40** |

**adaptive vs. the list everyone ships: p95 −71.8%, fail-overs −33%, cost −5.5%, for +5.5% on the
median.** Reproduce with `python scripts/replay.py --requests 4000 --concurrency 16 --markdown`.

Two things worth noting rather than hiding:

- **p99 is 5% worse than static.** Exploration (ε = 5% of requests ignore the ranking) and the
  occasional probe into a half-open breaker both land in the far tail. The policy buys a large p95
  win with a small p99 loss, which is the right trade for most traffic and the wrong one for a
  hard-deadline workload — so it is a knob, not a verdict.
- **`tail_weight = 0.25` is measured, not guessed.** `--sweep` walks the knob over the trace; 0.25
  is where the curve turns, keeping ~96% of the p95 improvement that full tail-weighting buys while
  giving back most of the median latency and nearly all of the extra cost.

---

## How the routing works

The score is in milliseconds throughout, so every term is comparable and the number on the
dashboard means something: *expected cost of sending the next request here*.

```
score = tail_weight·p95 + (1 − tail_weight)·p50        latency, tail-weighted
      + latency · error_rate_ewma · error_penalty      a failure costs its own time, then a retry
      + in_flight · queue_penalty_ms                   queue depth, so bursts spread
      + cost_per_request · ms_per_dollar               price, in the same unit as time
```

- **Error cost is priced in the provider's own latency**, not as a flat constant. That is what keeps
  "fast but flaky" comparable to "slow but solid" instead of a tuning exercise.
- **Under-measured providers get optimism** (`min_samples = 8`): an untried provider is cheap to try
  once and expensive to never try at all.
- **ε-exploration** keeps the leader's crown defended rather than inherited — without it, a provider
  that had a bad minute at startup is never measured again.
- **A three-state circuit breaker** sits in front of each provider, so an outage costs one timeout
  and then nothing, instead of one timeout per request. Half-open admits a **bounded** number of
  probes, which is the part naive implementations get wrong: an unbounded half-open state sends the
  entire recovered load at a provider that has proven nothing yet.
- **Health stats are streaming** — P² quantile estimation and EWMA error rates, O(1) memory and
  O(1) per update per provider, because this runs on every request. The dashboard's percentiles are
  computed exactly from stored rows instead; different constraints, different algorithm.

## What's in here

```
src/relay/
  stats.py                 P² streaming quantiles, EWMA error rate, in-flight counters
  breaker.py               three-state circuit breaker with bounded half-open probes
  pricing.py               per-model price book, token usage → USD
  store.py                 SQLite request log + rollups (recent, by-provider, timeline)
  providers/
    base.py                normalised provider interface, usage/cost/error taxonomy
    synthetic.py           hash-derived latency/failure profiles — deterministic fake traffic
    anthropic.py           Anthropic Messages API adapter
    openai_compatible.py   OpenAI / Groq / Together / any compatible endpoint
  routing/
    policies.py            static · round_robin · random · adaptive (strategy objects)
    gateway.py             rank → gate on breaker → call with timeout → record → fail over
  api/app.py               OpenAI-compatible /v1 + /stats + /requests + /admin + /demo
web/index.html             single-file live console: traffic, health, ranking, cost, request log
scripts/replay.py          the controlled policy benchmark above, with --sweep
tests/                     126 tests
```

### The dashboard

Served at `/` by the same process as the API — one service to deploy, no CORS, no build step, no
`node_modules`. It polls `/stats` and `/requests` every 2 s and shows traffic in 10-second buckets,
each provider's live p50/p95/error-EWMA/in-flight with its breaker state, **the current ranking with
every score term broken out** (the point being that the routing decision is legible, not a black
box), cost split by provider, and the recent request log with the full fail-over chain per request.

The policy can be swapped at runtime from the header — which is the reason policies are strategy
objects rather than a branch inside the gateway — and breakers can be reset by hand.

---

## Running it

```bash
python -m venv .venv && .venv/Scripts/activate        # Windows; use bin/activate on POSIX
pip install -e ".[dev]"

python -m pytest                                      # 126 tests
python scripts/replay.py --markdown                   # the benchmark table
python -m uvicorn relay.api.app:app --app-dir src --reload
```

Then open <http://localhost:8000>. **With no API keys set, the gateway boots three synthetic
providers** with real sleeps, and the dashboard's *send 25 requests* button generates traffic — a
gateway with nothing to route to does not demonstrate anything, so the demo path is a first-class
feature rather than an afterthought. Set either key below and the real adapters take over; load
generation then returns 403, because an unauthenticated endpoint should not be able to spend money.

| variable | default | meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | enables the Anthropic adapter |
| `OPENAI_API_KEY` | — | enables the OpenAI-compatible adapter |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | point at Groq, Together, vLLM, … |
| `OPENAI_MODELS` | `gpt-4o-mini` | comma-separated model list |
| `RELAY_POLICY` | `adaptive` | `static` · `round_robin` · `random` · `adaptive` |
| `RELAY_DB` | `relay.db` | SQLite request log |
| `RELAY_PRICING` | — | JSON price overrides, `{model: {input_per_mtok, output_per_mtok}}` |
| `RELAY_ATTEMPT_TIMEOUT` | `20` | seconds per attempt |
| `RELAY_TOTAL_BUDGET` | `45` | seconds for all attempts together |
| `RELAY_MAX_ATTEMPTS` | `3` | providers tried before giving up |
| `RELAY_FAILURE_THRESHOLD` | `5` | consecutive failures that trip a breaker |
| `RELAY_COOLDOWN` | `10` | seconds a tripped breaker stays open |

### API

| endpoint | purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI-shaped, plus an `x_relay` block: provider, policy, latency, cost, every attempt |
| `GET /v1/models` | models across providers, with prices |
| `GET /stats?window=` | live health, rollups, timeline, ranking |
| `GET /requests?limit=&failures_only=` | request log with per-attempt chains |
| `POST /admin/policy/{name}` | swap routing policy at runtime |
| `POST /admin/breaker/{provider}/reset` | close a breaker by hand |
| `POST /demo/load` | generate synthetic traffic (demo mode only) |

## Testing

126 tests, and the ones that matter are not line coverage:

- **Streaming estimators are checked against exact answers.** P² quantiles are compared to a sorted
  reference over adversarial distributions (bimodal, heavy-tailed, sorted, constant) with a stated
  error bound, because an estimator that is merely *plausible* is how a router silently routes wrong.
- **The breaker is tested as a state machine** on a controlled clock — every transition, plus the
  half-open probe bound and the concurrency case where several requests race the same transition.
- **Policies are tested as a comparison**, not in isolation: the adaptive policy must actually
  prefer the healthy provider after an outage, and must not starve an unmeasured one.
- **Adapters are tested against transport-level fakes** (`httpx.MockTransport`), so request shape,
  usage parsing, cost math and the error taxonomy are all covered without a network or a key.
- **The API is tested through `TestClient`**, including the 501 on `stream=true` and the 403 on
  demo load with real providers configured.

## Known limits

Stated rather than discovered:

- **No streaming.** `stream=true` returns an honest 501. Failing over a stream after bytes have
  reached the client is a design question, not a parameter — the client has already seen tokens from
  a provider that then died, and there is no transparent recovery.
- **Single node.** The health stats and breakers live in process memory, and the log is a local
  SQLite file; two instances would route independently and keep separate logs. Sharing state means
  Redis and a different consistency story.
- **The benchmark is synthetic.** Provider behaviour is drawn from profiles, not recorded from real
  endpoints. That is what makes the comparison controlled, and it is also why the numbers are
  evidence about the *policy*, not about any real provider's p95.
- **The live adapters are tested against fakes, not against the real APIs** — no keys were available
  to this build.
