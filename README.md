# DEMO LINK
https://www.loom.com/share/c52103f08d4d477fa04ec899ce3d168f

# LLM Inference Logging & Ingestion System

An **LLM observability platform** with a chatbot attached.

The chatbot is the traffic generator; the system around it is the actual product.
Every model call is automatically instrumented — provider, model, latency, token
counts, status, errors, timestamps, and truncated input/output previews are captured,
validated, queued, PII-redacted, persisted, and surfaced on a dashboard.

The design goal driving every decision below: **telemetry must never degrade the
product it measures.** Logging cannot slow chat down, cannot break it when the
database is slow, and cannot be something a developer has to remember to add.

---

## Contents

- [Quick start](#quick-start)
- [How it maps to the requirements](#how-it-maps-to-the-requirements)
- [Architecture](#architecture)
  - [The path of a single message](#the-path-of-a-single-message)
  - [Component responsibilities](#component-responsibilities)
  - [Why instrumentation is automatic](#why-instrumentation-is-automatic)
  - [Durable log delivery](#durable-log-delivery)
  - [Why ingestion is decoupled from storage](#why-ingestion-is-decoupled-from-storage)
  - [Streaming](#streaming)
  - [Cancellation](#cancellation)
  - [Failure handling](#failure-handling)
- [Database schema](#database-schema)
- [PII redaction](#pii-redaction)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Using real providers](#using-real-providers)
- [Testing](#testing)
- [Verification audit](#verification-audit)
- [Known limitations](#known-limitations)
- [What I would do next](#what-i-would-do-next)

---

## Quick start

One command, no API key required:

```bash
docker compose up --build
```

- Chat UI → http://localhost:5173
- Dashboard → http://localhost:5173 → "Observability"
- API docs → http://localhost:8000/docs

Five services start: `postgres`, `redis`, `backend`, `worker`, `frontend`.

The default provider is `mock` — no credentials, and it streams a reply reporting how
much conversation context it received, so multi-turn behavior is visible immediately.

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

# Worker, second shell
cd backend && PYTHONPATH=..:. .venv/bin/celery -A worker.celery_app.celery_app worker --loglevel=info

# Frontend, third shell
cd frontend && npm install && npm run dev
```

`INGEST_SYNC=true` bypasses the queue entirely and writes logs inline — useful for
running the API without a broker.

**Migrations** run automatically on startup (`alembic upgrade head`). To manage them
by hand:

```bash
cd backend
.venv/bin/alembic upgrade head                       # apply
.venv/bin/alembic revision --autogenerate -m "..."   # create after a model change
.venv/bin/alembic downgrade -1                       # roll back one
```

---

## How it maps to the requirements

| # | Requirement | Implementation |
|---|---|---|
| 1 | Multi-turn chatbot, short context, simple UI | React chat with streaming, conversation list, and resume. History trimmed to the last `HISTORY_TURN_LIMIT` messages (default 20) before each call |
| 2 | SDK capturing inference metadata, **auto-instrumenting** | One wrapper wraps every call. Provider adapters contain zero logging code — a new provider is instrumented for free |
| 2 | Sends logs in near real time | Pushed over HTTP the moment a call terminates, not batched |
| 3 | Ingestion: receive → validate → extract → store | `/ingest` receives and validates, then enqueues. The worker extracts, redacts, and stores — the four verbs are genuinely separate stages |
| 4 | Store messages, logs, extracted metadata | `conversations`, `messages`, `inference_logs`, with metadata in typed columns |

### Bonus items

| Bonus | Status | Evidence |
|---|---|---|
| Multi-provider support | **Done** | Three adapters (`mock`, `openrouter`, `anthropic`) behind one interface. See [Multi-provider: the evidence](#multi-provider-the-evidence) |
| Streaming responses | **Done** | SSE, token-by-token; verified incremental in a real browser |
| Latency / throughput / error dashboards | **Done** | 5 aggregation endpoints + 3 charts + provider breakdown, aggregated in SQL |
| Docker Compose one-command setup | **Done** | `docker compose up --build` brings up all 5 services from a wiped volume |
| Event-based architecture | **Done** | `/ingest` enqueues to Redis and returns `queued: true`; Celery worker consumes |
| PII redaction | **Done** | Applied in the worker before persistence; Luhn-validated cards, over-redaction guarded by tests |
| Deploy on self-hosted k8s | **Manifests written, not applied** | [`k8s/`](k8s/) — 15 resources, YAML-validated. No cluster was provisioned; treat as a design artifact. See [k8s/README.md](k8s/README.md) |

### Frontend requirements

| Requirement | Status | Notes |
|---|---|---|
| Cancel a conversation | **Done** | Stop button; two-sided (AbortController + Redis flag). Produces a `cancelled` log with partial output |
| List conversations | **Done** | Sidebar, newest first |
| Resume a conversation | **Done** | Full history rehydrated, survives reload |
| Rename / delete (added) | **Done** | Inline rename, two-step delete confirmation |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  React frontend                                                  │
│  chat (SSE) · conversation list · resume · observability charts  │
└───────────────┬─────────────────────────────────┬───────────────┘
                │ SSE stream                      │ GET /metrics/*
                ▼                                 │
┌─────────────────────────────────────┐           │
│  FastAPI backend                    │           │
│                                     │           │
│  ┌───────────────────────────────┐  │           │
│  │ SDK wrapper (instrumentation) │  │           │
│  │  times call, builds log event │  │           │
│  └──────────────┬────────────────┘  │           │
│                 │ calls             │           │
│  ┌──────────────▼────────────────┐  │           │
│  │ Provider adapter              │──┼──────────────► LLM provider
│  │  mock │ OpenRouter            │  │           │    (Nvidia, Claude,
│  │  no logging code inside       │  │           │     GPT, Gemini…)
│  └───────────────────────────────┘  │           │
│                                     │           │
│  POST /ingest ── validate (Pydantic)│           │
│         │                           │           │
└─────────┼───────────────────────────┘           │
          │ enqueue (returns immediately)         │
          ▼                                       │
┌─────────────────────┐                           │
│  Redis (broker)     │                           │
└─────────┬───────────┘                           │
          │ consume                               │
          ▼                                       │
┌─────────────────────────────────────┐           │
│  Celery worker                      │           │
│   validate → redact PII → persist   │           │
└─────────┬───────────────────────────┘           │
          │ INSERT ... ON CONFLICT DO NOTHING     │
          ▼                                       │
┌─────────────────────────────────────┐           │
│  Postgres                           │◄──────────┘
│  conversations · messages           │   SQL aggregation
│  inference_logs                     │   (avg, p95, error rate)
└─────────────────────────────────────┘
```

Redis serves two unrelated purposes: Celery's broker, and the store for
cancellation flags.

### The path of a single message

1. **User sends a message.** The frontend POSTs to the SSE endpoint with an
   `AbortController` attached.
2. **User message is persisted first**, before any model call — so a provider
   failure can never lose what the user typed.
3. **Context is trimmed** to the last N messages, newest-first via `LIMIT`, then
   re-reversed to chronological order.
4. **The wrapper starts a timer** and calls the provider adapter.
5. **Tokens stream back.** Each chunk is forwarded to the browser as an SSE `delta`
   frame and accumulated. Between chunks, the loop checks the cancellation flag.
6. **The stream terminates** — completed, failed, or cancelled. All three paths emit
   exactly one log event with the resolved provider/model, latency, tokens, status,
   and truncated previews.
7. **The event is POSTed to `/ingest`**, which validates it and enqueues a Celery
   task. No database write happens on the request path.
8. **The worker** picks up the task, redacts PII, and inserts idempotently.
9. **The assistant reply is persisted** and the final SSE frame closes the stream.
10. **The dashboard** aggregates in Postgres on demand.

### Component responsibilities

| Component | Owns | Deliberately does not |
|---|---|---|
| [`providers.py`](backend/app/sdk/providers.py) | Talking to LLM APIs, normalizing responses | Know that logging exists |
| [`logging.py`](backend/app/sdk/logging.py) | Timing, metadata capture, log delivery | Know which provider it wraps |
| [`conversations.py`](backend/app/api/conversations.py) | Chat orchestration, history trimming, SSE | Write log rows |
| [`ingest.py`](backend/app/api/ingest.py) | Validating and enqueueing log events | Write to Postgres on the happy path |
| [`tasks.py`](worker/tasks.py) | Redaction, normalization, persistence | Run on the request path |
| [`metrics.py`](backend/app/api/metrics.py) | SQL aggregation | Aggregate in Python |

### Why instrumentation is automatic

There is **one instrumented call site per mode** (blocking and streaming), and both
are provider-agnostic. Adding a provider means writing one class with `complete()`
and `stream()`:

```python
class AnthropicProvider:
    name = "anthropic"
    def complete(self, model, messages) -> CompletionResult: ...
    def stream(self, model, messages) -> Iterator[StreamChunk]: ...
```

No logging code. No API changes. No frontend changes. The new provider's calls are
captured with full metadata automatically.

This matters because **manual instrumentation rots**. Any approach where a developer
must remember to log at each call site eventually has unlogged call sites. Here,
forgetting is not possible — the only path to the model runs through the wrapper.

### Durable log delivery

The wrapper writes each event to an on-disk spool **before** attempting the POST,
and removes it only once the endpoint confirms acceptance. Without that ordering,
an ingestion outage silently discarded telemetry — the exact blind spot this system
exists to prevent.

```
build event ──► write spool file (fsync + atomic rename)
                      │
                      ▼
                POST /ingest ──► 2xx ──► delete spool file
                      │
                      └──► failure ──► file stays; replay loop retries
```

Details that matter:

- **Write-then-send, not send-then-write.** A crash between the two is
  indistinguishable from a failed delivery, and both leave a recoverable file.
- **4xx is terminal, 5xx is not.** A validation error will never become valid, so
  retrying it forever would block the spool behind a permanently bad event. `408`
  and `429` are treated as retryable because they are about timing, not payload.
- **Bounded.** The spool drops its oldest entries past a ceiling, so a long outage
  cannot fill the disk. Recent telemetry is more actionable than stale telemetry.
- **Replay stops at the first failure** rather than hammering a downed endpoint with
  the entire backlog every cycle.

### Why ingestion is decoupled from storage

The naive version writes to Postgres inside the `/ingest` request. That couples your
chat latency to your database health: a slow or briefly-unavailable Postgres makes
the product slow, which is exactly backwards for telemetry.

Instead `/ingest` validates and enqueues, returning `202 Accepted` in milliseconds.
If the database is down, events wait in the broker until a worker drains them.

**Honest framing:** this is queue-based decoupling, **not** a durable replayable
event log. Kafka or Redis Streams would give replay and event sourcing. Celery was
chosen for delivery speed and operational familiarity — a real tradeoff, not a claim
to be Kafka-grade streaming.

### Streaming

Server-Sent Events rather than WebSockets: token streaming is one-directional, and
SSE needs no connection upgrade or extra protocol handling.

Frames: `start` (persisted user message) → `delta` (token) × N → `done` /
`cancelled` / `error`.

The frontend uses `fetch` + `ReadableStream` rather than `EventSource`, because
`EventSource` cannot issue a POST with a JSON body.

### Cancellation

Two-sided, because either half alone is insufficient:

- **Client:** `AbortController` closes the connection.
- **Server:** `POST /conversations/{id}/cancel` sets a Redis flag the streaming loop
  checks between chunks.

Aborting only on the client would leave the server generating tokens into a closed
socket — still paying for them. The flag lives in Redis rather than process memory so
a cancel that lands on a **different API worker** than the open stream still works,
which is the normal case behind a load balancer. It carries a TTL so an abandoned
flag cannot cancel a future generation.

Partial output is persisted and logged with `status=cancelled`. A cancelled call is
neither a success nor a failure; folding it into either would distort the error rate.

This stops the stream *between chunks* — it does not abort the provider's in-flight
HTTP request.

### Failure handling

| Failure | Behavior |
|---|---|
| Malformed payload at the endpoint | `422`, nothing enqueued |
| Malformed payload at the worker | Dropped as `dropped:invalid` — it will never become valid, so retrying would loop forever |
| Worker dies mid-task | `acks_late` + `reject_on_worker_lost` redeliver it; the idempotent insert makes redelivery safe |
| Postgres down | Task retries with backoff (max 3); events wait in the broker |
| **Broker down** | `/ingest` degrades to a synchronous write rather than dropping the event, and reports `queued: false` |
| LLM call fails | Logged `status=error` with the provider's message, surfaced as an SSE `error` frame |
| Generation cancelled | Logged `status=cancelled` with partial output preserved |
| Ingestion endpoint unreachable | The event is already on the durable spool; a background loop replays it once ingestion recovers |
| Process crash / power loss mid-delivery | The spool entry survives on disk (`fsync` before rename) and is replayed on the next run |

---

## Database schema

Three tables — [`models.py`](backend/app/db/models.py).

```
conversations                messages                    inference_logs
─────────────                ────────                    ──────────────
id            uuid PK  ┌────►id             uuid PK ┌───►id            uuid PK
title         text     │     conversation_id fk ────┘    message_id     fk
status        text     └──── role           text        conversation_id fk
created_at    tstz           content        text        provider       text
                             created_at     tstz        model          text
                                                        latency_ms     int
                                                        prompt_tokens  int
                                                        completion_tokens int
                                                        status         text
                                                        error_message  text
                                                        input_preview  text
                                                        output_preview text
                                                        started_at     tstz
                                                        completed_at   tstz
                                                        created_at     tstz
```

### Schema decisions and tradeoffs

**Typed columns, not a JSON blob.** The obvious shortcut is `metadata JSONB`. It
accepts anything and makes every aggregation painful — the dashboard's
`avg(latency_ms)`, `percentile_cont(0.95)`, and per-bucket error rates become JSON
extraction and casting. Typed columns cost a migration when fields change; that cost
is worth paying for queries that stay simple as volume grows.

**`id` supplied by the producer, not the database.** This is what makes ingestion
idempotent. `acks_late` guarantees at-least-once delivery, so redelivery is expected,
not exceptional — `ON CONFLICT DO NOTHING` on a producer-supplied id turns a duplicate
into a no-op. A database-generated id would make every redelivery a duplicate row.

**Truncated, redacted previews rather than full payloads.** Enough to debug a bad
response, without duplicating entire transcripts into a second table or storing raw
user data in telemetry. A storage and privacy tradeoff, made deliberately.

**Resolved provider and model, not requested ones.** An aggregator may route
elsewhere than asked. Requesting `nvidia/nemotron-3.5-lightning:free` through
OpenRouter logs `provider = "Nvidia"` — who actually answered. Storing the request
instead would make the per-provider dashboard fiction.

**Three status values: `success`, `error`, `cancelled`.** Cancelled calls are neither.
Collapsing them into `error` would inflate the error rate with user-initiated stops.

**`inference_logs` is 1:1 with an assistant message.** Correct for single-shot chat.
It does not model multi-hop tool-call traces, where one turn produces several model
calls — that needs a parent trace id and a span model. A known limitation, not an
oversight.

**No `users` table.** The spec requires no auth. It is the first thing to add for a
real deployment, and it is why there is no per-user filtering on the dashboard.

**Deleting a conversation keeps its inference logs.** `inference_logs.conversation_id`
is nullable, and deletion nulls it rather than cascading. Chat content belongs to the
user and should disappear when they delete it; latency, error rate, and token spend
are operational metrics, and letting a sidebar cleanup silently rewrite them would
make the dashboard untrustworthy. The retained previews are already truncated and
PII-redacted, so keeping them does not keep the conversation. Cascade-deleting would
have been the easier default and the wrong one.

---

## PII redaction

Applied **in the worker, before anything reaches durable storage**
([`pii.py`](worker/pii.py)). Covers email, phone (international and NANP), credit
cards, SSN, and IPv4.

Two design points:

**Card matches are Luhn-validated.** A naive `\d{13,19}` redacts any long digit run —
request ids, trace ids, timestamps. Checking the Luhn checksum means real card numbers
are caught and ordinary identifiers survive.

**Over-redaction is tested as a failure mode.** Previews exist to debug bad responses;
a redactor that eats normal prose destroys their value. The test suite asserts that
ordinary text passes through untouched, not just that PII is caught.

**Scope boundary:** redaction applies to `inference_logs` previews — the telemetry
copy. It does **not** apply to `messages`, which is the user's own chat history
rendered back to them verbatim. If logs were shipped to an analytics domain with a
wider audience than the chat user, message-level redaction would need its own
decision.

Regexes catch structured identifiers, not names, addresses, or free-form disclosure.
Presidio is the upgrade path.

---

## API reference

| Method | Path | Purpose |
|---|---|---|
| POST | `/conversations` | Create |
| GET | `/conversations` | List (sidebar) |
| PATCH | `/conversations/{id}` | Rename |
| DELETE | `/conversations/{id}` | Delete (keeps inference logs — see below) |
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
| GET | `/health` | Liveness |

---

## Configuration

All via environment variables; see [`.env.example`](backend/.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local Postgres | Connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Broker and cancellation flags |
| `LLM_PROVIDER` | `mock` | `mock` or `openrouter` |
| `LLM_MODEL` | `mock/echo-1` | Model id passed to the provider |
| `OPENROUTER_API_KEY` | — | Required when provider is `openrouter` |
| `INGEST_URL` | `http://127.0.0.1:8000/ingest` | Where the wrapper posts log events |
| `INGEST_SYNC` | `false` | Bypass the queue, write logs inline |
| `SPOOL_ENABLED` | `true` | Durable on-disk spool for log events |
| `SPOOL_DIR` | `/tmp/llm-log-spool` | Where spooled events are written |
| `SPOOL_REPLAY_INTERVAL_SECONDS` | `30` | How often the replay loop drains the spool |
| `HISTORY_TURN_LIMIT` | `20` | Context budget in messages |
| `PREVIEW_MAX_CHARS` | `500` | Preview truncation length |
| `CANCEL_FLAG_TTL_SECONDS` | `600` | Cancellation flag lifetime |
| `CORS_ORIGINS` | localhost + 127.0.0.1 :5173 | Allowed browser origins |

`INGEST_URL` uses `127.0.0.1` rather than `localhost` deliberately: on macOS
`localhost` resolves to `::1` first, so an unrelated process bound to IPv6 `:8000`
would silently receive the log events instead of this app. That is not hypothetical —
it happened during development, and every log was being swallowed by another
container until the wrapper started checking response status.

---

## Using real providers

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export LLM_PROVIDER=openrouter
export LLM_MODEL=nvidia/nemotron-3.5-lightning:free
docker compose up -d
```

OpenRouter is OpenAI-compatible and routes to Claude, GPT, Gemini, Llama, DeepSeek
and others by changing `LLM_MODEL` alone. Free models carry a `:free` suffix.

**Verification status:** verified end to end against live OpenRouter traffic using
`nvidia/nemotron-3.5-lightning:free` — blocking calls, token-by-token SSE streaming,
queue processing, and dashboard aggregation all confirmed in the browser. Error
handling was separately confirmed against a real 401.

---

## Multi-provider: the evidence

"Multi-provider support" is easy to claim and easy to fake — an aggregator plus a
mock does not prove an abstraction. Here is what actually backs it.

### 1. Three adapters, one interface

| Adapter | Wire format | Purpose |
|---|---|---|
| `MockProvider` | none | Runs the stack with no credentials |
| `OpenRouterProvider` | OpenAI-compatible | One key, many upstream vendors |
| `AnthropicProvider` | Anthropic Messages API | **Deliberately not OpenAI-shaped** |

The Anthropic adapter is the one that matters. If every provider spoke the same
dialect, the interface would be proving nothing. Anthropic differs on six axes the
adapter has to absorb:

| | OpenAI-shaped | Anthropic |
|---|---|---|
| Auth | `Authorization: Bearer` | `x-api-key` + `anthropic-version` |
| System prompt | a message with `role="system"` | top-level `system` field |
| `max_tokens` | optional | **required** |
| Response text | `choices[0].message.content` | `content[]` block list |
| Usage keys | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` |
| Stream events | `choices[].delta` | typed `content_block_delta` |

Callers see none of this. Both return the same `CompletionResult`, and there is a
[test asserting exactly that](backend/tests/test_providers.py) — same content, same
token counts, from two different wire formats.

### 2. Live calls across distinct vendors

Routed through OpenRouter, each logged with the provider that actually served it:

| Requested model | Logged provider | Tokens |
|---|---|---|
| `nvidia/nemotron-3.5-lightning:free` | `Nvidia` | 21 in / 119 out |
| `google/gemma-4-26b-a4b-it:free` | `Darkbloom` | 18 in / 2 out |
| `openai/gpt-oss-20b:free` | `Darkbloom` | 72 in / 41 out |

Note that `provider` records the **serving infrastructure**, which is not always the
model's author — Darkbloom served both the Google and OpenAI models. That is real
information about who handled the request, and it is exactly why the log stores the
resolved value instead of the requested one.

### 3. Real API contact, without a key

The Anthropic adapter was exercised against the live API with a deliberately invalid
key. Both paths returned:

```
Anthropic returned 401: {"type":"error","error":{"type":"authentication_error",
"message":"invalid x-api-key"},"request_id":"req_011Cdznatfakg7qvY84t9daw"}
```

An `authentication_error` — rather than a 404, a 400, or a schema complaint — proves
the endpoint, headers, and request body are correct and that only the credential was
rejected. The error also parses cleanly on both the blocking and streaming paths.

### 4. Tests that would catch a regression

[`test_providers.py`](backend/tests/test_providers.py) — 14 tests, no keys, no
network. They assert the system prompt is hoisted, the right auth header is used,
content blocks are joined, tool-use blocks are excluded, streaming events are parsed,
the factory rejects unknown providers and missing keys, and every adapter implements
the full interface.

### What would make this stronger

A successful call with a valid Anthropic key. The request shape is confirmed correct
by the 401, but the response parser has only been exercised against stubs and the
error path — not a real 200.

Switching providers requires no code change. The logging layer, queue, worker, and
dashboard are all provider-agnostic.

---

## Testing

```bash
cd backend && .venv/bin/python -m pytest
```

**98 tests, no skips.** Each suite creates its own isolated database on demand, so
the suite runs from a clean machine with only Postgres up.

Two testing decisions worth noting. Suites get **separate databases** so seeded row
counts stay deterministic when run together, and a `conftest` snapshots the
environment variables each suite mutates — without that, whichever suite imported
first won and later suites silently ran against the wrong database. Test databases
are also **created rather than skipped-if-missing**: a suite that quietly skips when
its database is absent has stopped protecting anything, which is worse than failing
loudly.

| Suite | Tests | Covers |
|---|---|---|
| `test_pii.py` | 18 | Email/phone/card/SSN/IP redaction, Luhn validation, **over**-redaction guards, redaction artifacts |
| `test_api.py` | 15 | CRUD, rename, delete-keeps-logs, multi-turn ordering, validation, ingest idempotency |
| `test_providers.py` | 14 | Anthropic vs OpenAI request/response shapes, normalized results, stream parsing, factory resolution |
| `test_sdk_logging.py` | 8 | Wrapper: success, provider failure, unexpected exception, preview truncation, transport failure, non-2xx delivery, non-blocking delivery |
| `test_streaming_api.py` | 8 | SSE frame sequence, persistence, resume, cancel, 404s, error frames |
| `test_streaming.py` | 9 | Stream completion, cancellation with partial output, mid-stream errors, abandoned generators, stream/complete parity |
| `test_worker.py` | 7 | Task persistence, redact-before-write, malformed payloads, redelivery idempotency, broker binding |
| `test_metrics.py` | 7 | Aggregation correctness, fractional error rates, bucket ordering, empty windows |
| `test_spool.py` | 12 | Write-before-send ordering, replay, terminal vs retryable status codes, corruption, capacity, disk failure |

Beyond unit and integration tests, the full stack was verified through a real browser
(Playwright) against both the mock and live providers: incremental token rendering,
mid-stream cancellation producing a `cancelled` log, resume after reload, dashboard
rendering, and no console errors at any viewport.

### A bug worth documenting

The queue silently was not being used. `@shared_task` binds to whichever Celery app is
*current*, and the API only imported `worker.tasks` — so the task attached to Celery's
**default** app pointing at `localhost`. Every enqueue from the API failed with
"Connection refused" and fell through to the synchronous fallback.

Redaction and persistence still worked, so the system looked correct end to end. Two
things caught it: the `queued` flag in the ingest response, and checking the worker's
logs for task activity rather than trusting that rows appeared in the database.

Fixed by binding the task to the configured app explicitly. There is now a regression
test asserting the task resolves a real broker URL — the kind of failure that is
invisible without one.

---

## Verification audit

Every claim in this README was re-checked against the running system rather than
assumed from the code. This section records what was tested, the observed result,
and how to reproduce it. Where a claim did **not** hold, that is stated too.

### Automatic instrumentation

The central claim, so it gets a demonstration rather than an assertion: a provider
class written from scratch, containing **no logging code of any kind**, still
produces a complete log entry.

```python
class TotallyNewVendorProvider:
    name = "acme-labs"
    def complete(self, model, messages):
        return CompletionResult(content="hi from acme", provider="acme-labs",
                                model=model, prompt_tokens=7, completion_tokens=3)
    def stream(self, model, messages): ...
```

Captured without modifying anything else:

```
provider           acme-labs
model              acme/v1
latency_ms         0
prompt_tokens      7
completion_tokens  3
status             success
started_at         2026-08-13T13:00:33.083492+00:00
```

Supporting evidence: `grep` for logging calls inside
[`providers.py`](backend/app/sdk/providers.py) and
[`conversations.py`](backend/app/api/conversations.py) returns **no** matches
outside docstrings. Neither the provider layer nor the API layer contains
instrumentation — it lives entirely in the wrapper between them.

### Asynchronous ingestion

The interesting property is not that a row eventually appears, but that it is
**absent immediately after the endpoint returns**:

```
POST /ingest  ->  {"accepted":true,"queued":true}
rows immediately after 202:  0
rows after the worker ran:   1
```

Zero rows at the moment of the `202` proves the endpoint does no database work on
the happy path. Confirmed in code: `ingest_log()` calls `process_log_event.delay()`
and only touches Postgres via `_write_directly()` in sync mode or broker-down
fallback.

### Independent scaling

Workers were scaled without touching the API, then 12 events enqueued:

```
docker compose up -d --scale worker=3 worker

chatbotapp-worker-1 -> 7 tasks
chatbotapp-worker-2 -> 3 tasks
chatbotapp-worker-3 -> 3 tasks
all 12 rows persisted
```

Work distributed across all three replicas from one Redis queue, with the backend
untouched. This is what "ingestion and processing scale independently of the chat
application" means concretely.

### Multi-provider

Three live models through distinct upstream vendors, each logged with the provider
that actually served the request:

| Requested | Logged provider | Tokens |
|---|---|---|
| `nvidia/nemotron-3.5-lightning:free` | `Nvidia` | 21 in / 119 out |
| `google/gemma-4-26b-a4b-it:free` | `Darkbloom` | 18 in / 2 out |
| `openai/gpt-oss-20b:free` | `Darkbloom` | 72 in / 41 out |

Plus the Anthropic adapter reaching the real API (invalid key, so a `401
authentication_error` with a request id — proving endpoint, headers, and body are
correct and only the credential was rejected). Full detail in
[Multi-provider: the evidence](#multi-provider-the-evidence).

### Structured storage

`\d inference_logs` shows **15 typed columns** — `uuid`, `integer`, `text`,
`timestamptz`. A direct query for `json`/`jsonb` columns returns **0**. The metadata
is queryable SQL, not a blob.

### PII redaction

Verified by reading the row back with raw SQL rather than through the API, so the
check cannot be fooled by presentation-layer masking:

```
email [REDACTED_EMAIL] phone [REDACTED_PHONE] card [REDACTED_CARD] ssn [REDACTED_SSN] ip [REDACTED_IP]
```

### Streaming, dashboards, containerization

- One reply produced **33 `event: delta` frames** — genuinely incremental, not a
  single buffered write.
- All five `/metrics/*` endpoints return populated series from real traffic.
- Five services run under Compose: `postgres`, `redis`, `backend`, `worker`,
  `frontend`.

### Durable spool

Verified by breaking ingestion and watching an event survive:

```
ingest_url pointed at a dead port
  -> "Failed to deliver inference log ...: [Errno 111] Connection refused"
  -> pending after failed delivery: 1        (event held on disk)

ingestion restored, replay run
  -> replayed: 1
  -> pending after replay: 0
  -> row present in Postgres, input_preview = "recovered from spool"
```

The event survived a total delivery failure and reached the database on retry.

### Migrations

Startup now runs `alembic upgrade head` instead of `create_all()`:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 948613cdc5dc, initial schema
alembic revision: 948613cdc5dc
```

The test suite applies the same migrations rather than `create_all()`, so a
migration that fails to apply fails the suite — not just the deploy.

### Bugs this audit found

Verification is only worth doing if it can fail. It did, four times:

1. **The queue was silently unused.** `@shared_task` bound to Celery's default app,
   so every enqueue from the API failed with "Connection refused" and fell back to a
   synchronous write. Redaction and persistence still worked, so the system looked
   correct end to end. Caught by inspecting worker logs rather than trusting that
   rows appeared.
2. **Log delivery deadlocked on streaming requests.** The wrapper POSTed to an
   endpoint served by the same single-worker process while that process was busy
   streaming; the request timed out and the log was lost. Delivery now runs on a
   background thread pool.
3. **Two suites silently skipped.** `test_api` and `test_worker` guarded on database
   reachability and skipped — rather than failed — when their database was missing,
   so a "59 passed" run was really 43 passed and 16 skipped. Each suite now creates
   its own database.
4. **Redaction left cosmetic artifacts.** `+91 98765 43210` produced
   `+[REDACTED_PHONE]` (stray `+`), and a card followed by text produced
   `[REDACTED_CARD]ssn` (consumed separator). No PII leaked, but a redactor that
   visibly mangles text invites doubt about what else it gets wrong.

All four have regression tests. The first two were invisible from the outside, which
is the point: an observability system that fails silently is the worst possible
failure mode, and only checking the plumbing — not the output — surfaces it.

### What is *not* verified

- **Kubernetes manifests have never been applied to a cluster.** They are
  YAML-valid and structurally complete; that is all that can be claimed.
- **The Anthropic adapter has never received a real 200.** Request shape is
  confirmed by the live `401`; the success-path response parser is exercised only
  against stubs.
- **Client-disconnect logging is unreliable** — see [Known limitations](#known-limitations).

### Reproducing this

```bash
docker compose up -d --build
cd backend && .venv/bin/python -m pytest        # 98 tests
curl -s localhost:8000/metrics/summary?window_minutes=60
docker compose logs worker | grep succeeded
```

---

## Known limitations

Deliberate scoping calls, stated plainly rather than hidden.

- **No dead-letter queue.** Malformed tasks are logged and dropped rather than
  parked somewhere reviewable.
- **A client that disconnects mid-stream is not reliably logged.** When the browser
  vanishes (tab closed, network drop), the ASGI server abandons the response
  generator instead of closing it. Cleanup lives in a `finally`, which only runs
  when Python finalizes the generator — and a reference cycle can delay that
  indefinitely, so the log is sometimes written and sometimes not. Confirmed by
  observing `/ingest` fire on some abandoned streams and not others.

  Cancel via the **Stop button works reliably** — that path sets a Redis flag the
  stream loop checks, and never depends on finalization. The gap is specifically
  the ungraceful disconnect.

  The real fix is not more cleanup handlers: it is to stop treating in-process
  cleanup as the delivery guarantee. Writing the log event to a durable local
  spool *before* the provider call, then marking it complete afterward, makes an
  abandoned stream a recoverable record rather than a lost one. That is the same
  local-spool work listed above, and it subsumes this.
- **Celery is a task queue, not a durable replayable event log.**
- **No auth or rate limiting** anywhere, including `/ingest`.
- **The frontend image runs the Vite dev server** so `VITE_API_URL` stays
  runtime-configurable. Production would build the bundle and serve it behind nginx.

---

## What I would do next

In priority order, with reasoning:

1. **Auth and per-user scoping.** Requires the `users` table, and turns the dashboard
   into something you could expose to more than one team.
2. **Dead-letter queue.** Malformed tasks are currently logged and dropped; parking
   them somewhere reviewable is the difference between "we dropped it" and "we can
   see what we dropped".
3. **Kafka or Redis Streams** if replay or event sourcing becomes a requirement. Not
   before — the queue is sufficient at current scale, and swapping it is contained to
   one function.
4. **Kubernetes — apply the manifests to a real cluster.** [`k8s/`](k8s/) contains a
   complete set (Deployments, StatefulSets with PVCs, probes, initContainers, KEDA
   queue-depth autoscaling) but they have not been run against a live cluster. The
   remaining work is provisioning one, loading images, and fixing whatever the first
   `kubectl apply` reveals — plus NetworkPolicies, managed Postgres/Redis, and real
   secret management before it is production-shaped.

### Scaling notes

Workers scale horizontally — add replicas or raise `--concurrency`; Redis fans tasks
out automatically. The API scales behind a load balancer, which the Redis-backed
cancellation flags already account for.

The next real bottleneck is `inference_logs` growth. Metrics queries filter on
`created_at`, so a time-based index comes first, then partitioning by time, then
pre-aggregated rollup tables once raw scans stop being viable. The write path scales
before the read path does, which is the usual shape for telemetry systems.
