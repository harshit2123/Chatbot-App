# LLM Inference Logging & Ingestion System

A multi-turn streaming chatbot where **every model call is instrumented**. Each call
emits a structured log event — provider, model, latency, token counts, status,
errors, timestamps, and truncated input/output previews — which is validated,
queued, PII-redacted, and persisted to Postgres, then surfaced on an observability
dashboard.

Current state: **Phase 1 and Phase 2 complete** (see [Roadmap](#roadmap)).

---

## Quick start

One command, no API key required:

```bash
docker compose up --build
```

- Chat UI → http://localhost:5173
- Dashboard → http://localhost:5173 → "Observability"
- API docs → http://localhost:8000/docs

Five services come up: `postgres`, `redis`, `backend`, `worker`, `frontend`.

The default provider is `mock`, which needs no credentials and streams a reply that
reports how much conversation context it received. To use real models, see
[Using real providers](#using-real-providers).

**Port conflicts.** Every host port is overridable:

```bash
BACKEND_PORT=8010 FRONTEND_PORT=5174 REDIS_PORT=6380 docker compose up --build
```

### Running without Docker

```bash
docker compose up -d postgres redis

cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload

# Worker, in a second shell, from the repo root
cd backend && PYTHONPATH=..:. .venv/bin/celery -A worker.celery_app.celery_app worker --loglevel=info

# Frontend, in a third
cd frontend && npm install && npm run dev
```

Set `INGEST_SYNC=true` to skip the queue entirely and write logs inline — useful
when you want the API without a broker.

### Tests

```bash
cd backend && .venv/bin/python -m pytest
```

59 tests. Test databases are created automatically; Postgres must be reachable.

| Suite | Covers |
|---|---|
| `test_sdk_logging.py` | Wrapper: success, provider failure, unexpected exception, preview truncation, transport failure, non-2xx delivery |
| `test_streaming.py` | Stream completion, cancellation with partial output, mid-stream errors, stream/complete parity |
| `test_pii.py` | Redaction of email/phone/card/SSN/IP, Luhn validation, and **over**-redaction guards |
| `test_worker.py` | Task persistence, redact-before-write, malformed payloads, redelivery idempotency, broker binding |
| `test_metrics.py` | Aggregation correctness, fractional error rates, bucket ordering, empty windows |
| `test_streaming_api.py` | SSE frame sequence, persistence, resume, cancel, 404s, error frames |
| `test_api.py` | CRUD, multi-turn ordering, validation, ingest idempotency |

---

## Architecture

```
React frontend (streaming chat + dashboard)
      |
      | SSE
      v
FastAPI backend --- get_completion()/stream() ---> Provider adapter (mock | OpenRouter)
      |                                                   |
      |                                          SDK wrapper times the call and
      |                                          builds a log event
      v                                                   |
POST /ingest  <-------------------------------------------+
      |
      | Pydantic validation, then enqueue (does NOT touch Postgres)
      v
Redis broker --> Celery worker: validate -> redact PII -> persist
                                                   |
                                                   v
                                    Postgres (conversations, messages, inference_logs)
                                                   ^
                                                   |
                        /metrics/* aggregation endpoints --> dashboard
```

### Ingestion flow

1. A chat turn persists the user message, then trims history to the last N messages.
2. `instrumented_stream()` wraps the provider call, timing it and accumulating output.
3. When the stream terminates — completed, failed, **or cancelled** — one log event
   is emitted and POSTed to `/ingest`.
4. `/ingest` validates with Pydantic and enqueues a Celery task. It never writes to
   Postgres on the happy path.
5. The worker redacts PII and writes the row.

**Why decouple.** If Postgres is slow or briefly down, `/ingest` still returns in
milliseconds and the event waits in the broker. The API's latency is no longer
coupled to database health.

### Why the instrumentation is "automatic"

There is one instrumented call site per mode (blocking and streaming), and both are
provider-agnostic. Adding a provider means adding one class implementing `complete()`
and `stream()` in [`providers.py`](backend/app/sdk/providers.py) — no new logging
code, no API changes, no frontend changes.

### Failure handling

| Failure | Behavior |
|---|---|
| Malformed log payload | `422` at the endpoint; if it reaches the worker, dropped as `dropped:invalid` rather than retried forever |
| Worker dies mid-task | `acks_late` + `reject_on_worker_lost` redeliver it; the insert is idempotent on the producer-supplied id, so redelivery cannot double-write |
| Postgres down | Task retries with backoff (max 3); the event survives in the broker meanwhile |
| **Broker down** | `/ingest` degrades to a synchronous write rather than dropping the event, and the response reports `queued: false` |
| LLM call fails | Logged `status=error` with the provider message, surfaced as an SSE `error` frame |
| Generation cancelled | Logged `status=cancelled` **with partial output**, and the partial reply is persisted so the conversation stays coherent |
| Ingestion endpoint rejects | Warned and dropped — logging must never break chat. See [Known limitations](#known-limitations) |

### Cancellation

Two-sided, because either half alone is insufficient:

- **Client**: `AbortController` closes the connection.
- **Server**: `POST /conversations/{id}/cancel` sets a Redis flag that the streaming
  loop checks between chunks.

Aborting only on the client would leave the server generating tokens into a closed
socket. The flag lives in Redis rather than process memory so a cancel request that
lands on a different API worker than the open stream still works — the normal case
behind a load balancer. It carries a TTL so an abandoned flag cannot cancel a future
generation.

This stops the stream **between chunks**; it does not abort the upstream provider's
in-flight HTTP request.

---

## Database schema

Three tables — see [`models.py`](backend/app/db/models.py): `conversations`,
`messages`, and `inference_logs` (one row per model call).

### Schema decisions

**Metadata lives in typed columns, not a JSON blob.** The dashboard aggregations
(`avg(latency_ms)`, `percentile_cont` for p95, error rate per bucket) are plain SQL
against indexed columns rather than JSON extraction.

**`id` is supplied by the producer, not the database.** This is what makes ingestion
idempotent under at-least-once delivery. With `acks_late` enabled, redelivery is
expected, and the primary-key conflict makes it a no-op.

**Previews are truncated (500 chars) and redacted, not full payloads.** A deliberate
storage-and-privacy tradeoff.

**`provider`/`model` record what actually served the request.** An aggregator may
route elsewhere than requested, so the log stores resolved values — otherwise the
per-provider dashboard breakdown would be fiction.

**`status` has three values**: `success`, `error`, `cancelled`. Cancelled calls are
neither successes nor failures, and folding them into either would distort the
error rate.

**`inference_logs` is 1:1 with an assistant message.** Correct for single-shot chat;
it does not model multi-hop tool-call traces. A known limitation, not an oversight.

**No `users` table**, since the spec requires no auth. First thing to add for a real
deployment.

---

## PII redaction

Applied in the worker, before anything reaches durable storage
([`pii.py`](worker/pii.py)). Covers email, phone (international and NANP), credit
cards, SSN, and IPv4.

Two design points worth stating:

- **Card matches are Luhn-validated**, so a 16-digit request id is not mangled into
  `[REDACTED_CARD]`.
- **Over-redaction is tested as a failure mode.** Previews exist to debug bad
  responses; a redactor that eats ordinary prose destroys their value. Tests assert
  that normal text passes through untouched.

**Scope boundary:** redaction applies to `inference_logs` previews — the telemetry
copy of the data. It does **not** apply to `messages`, which is the user's own chat
history rendered back to them verbatim. If this system stored logs in a separate
analytics domain with a broader audience than the chat user, message-level redaction
would need its own decision.

Regexes catch structured identifiers, not names, addresses, or free-form disclosure.
Presidio would be the upgrade path.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/conversations` | Create |
| GET | `/conversations` | List (sidebar) |
| GET | `/conversations/{id}/messages` | Resume — full history |
| POST | `/conversations/{id}/messages` | Send, blocking response |
| POST | `/conversations/{id}/messages/stream` | Send, **SSE streaming** response |
| POST | `/conversations/{id}/cancel` | Cancel an in-flight generation |
| POST | `/ingest` | Receive a log event from the SDK wrapper |
| GET | `/logs` | Read stored logs |
| GET | `/metrics/summary` | Totals, error rate, avg/p95 latency, tokens |
| GET | `/metrics/latency` | Latency time series |
| GET | `/metrics/errors` | Error rate time series |
| GET | `/metrics/throughput` | Calls per minute |
| GET | `/metrics/providers` | Breakdown by provider and model |

SSE frames: `start`, `delta`, `done`, `cancelled`, `error`.

---

## Using real providers

```bash
# backend/.env
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-3.5-sonnet
OPENROUTER_API_KEY=sk-or-...
```

OpenRouter routes to Claude, GPT, Gemini, Llama and others by changing `LLM_MODEL`
alone. Both blocking and streaming paths are implemented against its API.

**Verification status:** the OpenRouter adapter is verified against real API error
responses (401 handling confirmed end to end) but has not been run with a valid key,
so its streaming happy path is tested against a stub rather than live traffic. The
mock provider's streaming is verified end to end through the browser.

---

## Known limitations

Deliberate scoping calls, not unknowns.

- **Log delivery from the wrapper is best-effort.** If `/ingest` is unreachable the
  event is warned and dropped. Everything downstream of `/ingest` is durable; this
  first hop is not. A local spool would close it.
- **No dead-letter queue.** Malformed tasks are logged and dropped.
- **Celery is a task queue, not a durable replayable event log.** Kafka or Redis
  Streams is the purer event-driven answer; this is queue-based decoupling, chosen
  for delivery speed. Stated plainly rather than sold as streaming.
- **Schema via `create_all()`**, not Alembic migrations.
- **No auth or rate limiting** anywhere, including `/ingest`.
- **`latency_ms` on streamed calls measures full stream duration.** Time-to-first-token
  is the more useful streaming metric and is not yet captured.
- **The frontend image runs the Vite dev server** so `VITE_API_URL` stays runtime-
  configurable. Production would build the bundle and serve it behind nginx.

---

## Roadmap

**Phase 1 — complete**
Schema · CRUD · multi-turn chat · provider adapters · instrumentation wrapper ·
validated ingestion · React chat UI with resume · Docker Compose · tests

**Phase 2 — complete**
Celery + Redis + worker · PII redaction · SSE streaming · two-sided cancellation ·
metrics aggregation endpoints · observability dashboard · expanded test suite

**Phase 3 — not started**
Verified multi-provider runs across Claude/GPT/Gemini · k8s manifests · time-to-first-token ·
DLQ · Alembic migrations · auth

### Scaling notes

Workers scale horizontally — add replicas or raise `--concurrency`; Redis fans tasks
out automatically. Beyond a single Redis instance, RabbitMQ or Kafka gives durability
and replay. On k8s, an HPA keyed on queue depth is the natural autoscaling signal.

The next real bottleneck is `inference_logs` growth: the metrics queries scan by
`created_at`, so a time-based index (and eventually partitioning or rollup tables)
is what keeps the dashboard fast as volume grows.
