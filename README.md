# LLM Inference Logging & Ingestion System

A multi-turn chatbot where **every model call is instrumented**. Each call emits a
structured log event — provider, model, latency, token counts, status, errors,
timestamps, and truncated input/output previews — which is validated, ingested,
and persisted to Postgres.

Current state: **Phase 1 complete** (see [Roadmap](#roadmap)). Chat, instrumentation,
ingestion, storage, and UI all work end to end.

---

## Quick start

One command, no API key required:

```bash
docker compose up --build
```

- Chat UI → http://localhost:5173
- API docs → http://localhost:8000/docs

The default provider is `mock`, which needs no credentials and echoes back
conversation state so multi-turn context is visibly working. To use real models,
see [Using real providers](#using-real-providers).

**Port conflicts.** If something already occupies a port, override it:

```bash
BACKEND_PORT=8010 FRONTEND_PORT=5174 docker compose up --build
```

### Running without Docker

```bash
# Postgres (or point DATABASE_URL at your own)
docker compose up -d postgres

# Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload

# Frontend
cd frontend && npm install && npm run dev
```

### Tests

```bash
cd backend
docker exec chatbotapp-postgres-1 psql -U postgres -c "CREATE DATABASE llmlogs_test;"
.venv/bin/python -m pytest
```

16 tests cover the instrumentation wrapper (success, provider failure, unexpected
exception, preview truncation, transport failure, non-2xx delivery) and the API
(multi-turn ordering, resume, 404s, validation, ingest idempotency).

---

## Architecture

```
React frontend (chat + log panel)
      |
      v
FastAPI backend  --- get_completion() ---> Provider adapter (mock | OpenRouter)
      |                                          |
      |                                    SDK wrapper times the call and
      |                                    builds a log event
      v                                          |
POST /ingest  <----------------------------------+
      |
      v
Pydantic validation  ->  Postgres (conversations, messages, inference_logs)
```

### Ingestion flow

1. A chat turn persists the user message, then trims history to the last N messages.
2. `instrumented_completion()` wraps the provider call — it times it, and emits a
   log event whether the call succeeds **or** fails.
3. The event is POSTed to `/ingest` over HTTP, exactly as it would be from a
   separate service.
4. `/ingest` validates the payload with Pydantic and writes to Postgres.

The wrapper posts over HTTP rather than calling the database directly on purpose:
the SDK stays transport-identical to how it would behave running inside a
different process, which is what makes the Phase 2 swap to a queue a one-function
change.

### Why the instrumentation is "automatic"

There is exactly one instrumented call site, and it is provider-agnostic. Adding a
new provider means adding one class implementing `complete()` in
[`providers.py`](backend/app/sdk/providers.py) — no new logging code, no changes
to the API layer, no changes to the frontend. The logging layer never learns
which provider it is wrapping.

### Failure handling

| Failure | Behavior |
|---|---|
| Malformed log payload | `422` at the endpoint; nothing partially written |
| Redelivered log event | `ON CONFLICT DO NOTHING` on the producer-supplied `id` — idempotent, no duplicate rows |
| LLM call fails | Logged with `status=error` and the provider's message, then surfaced as `502`. Error rate is measurable, not invisible |
| Ingestion endpoint down or rejecting | Warned and dropped, so observability never breaks chat. **This is a real gap** — see [Known limitations](#known-limitations) |
| Provider fails mid-turn | The user's message is already committed, so nothing typed is lost |

---

## Database schema

Three tables — see [`models.py`](backend/app/db/models.py).

- **`conversations`** — id, title, status, created_at. Title is derived from the
  first user message.
- **`messages`** — id, conversation_id, role, content, created_at. Ordered by
  `created_at`; resume depends on this ordering.
- **`inference_logs`** — one row per model call: provider, model, latency_ms,
  prompt/completion tokens, status, error_message, input/output previews,
  started_at, completed_at.

### Schema decisions

**Metadata lives in typed columns, not a JSON blob.** Aggregations for the Phase 2
dashboards (`avg(latency_ms)`, error rate over time, tokens by model) are plain
SQL against indexed columns rather than JSON extraction.

**`id` is supplied by the producer, not the database.** This is what makes ingestion
idempotent. When Phase 2 moves to a queue with at-least-once delivery, a
redelivered task hits the primary-key conflict and no-ops instead of double-inserting.

**Previews are truncated (500 chars default), not full payloads.** A deliberate
storage-and-privacy tradeoff: enough to debug a bad response, not a complete
transcript duplicated across two tables.

**`provider`/`model` record what actually served the request.** An aggregator may
route to a different upstream than requested, so the log stores the resolved
values, not the requested ones — otherwise the multi-provider metadata is fiction.

**`inference_logs` is 1:1 with an assistant message.** Simple and correct for
single-shot chat, but it does not model multi-hop tool-call traces. A known
limitation, not an oversight.

**No `users` table**, since the spec requires no auth. It is the first thing I would
add for a real deployment.

---

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/conversations` | Create a conversation |
| GET | `/conversations` | List conversations (sidebar) |
| GET | `/conversations/{id}/messages` | Resume — full history |
| POST | `/conversations/{id}/messages` | Send a message, get the reply |
| POST | `/ingest` | Receive a log event from the SDK wrapper |
| GET | `/logs` | Read stored logs, optionally by conversation |
| GET | `/health` | Liveness |

---

## Using real providers

```bash
# backend/.env
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-3.5-sonnet
OPENROUTER_API_KEY=sk-or-...
```

OpenRouter is OpenAI-compatible and routes to Claude, GPT, Gemini, Llama, and
others by changing `LLM_MODEL` alone. It is wrapped behind the same adapter
interface as the mock provider, so nothing else changes.

Keys are read from the environment and `.env` is gitignored.

---

## Known limitations

These are deliberate Phase 1 scoping calls, not unknowns.

- **Log delivery is best-effort.** If `/ingest` is down or rejects a payload, the
  event is logged locally and dropped. The wrapper checks for non-2xx responses
  and warns — an earlier version treated any response as success, which silently
  lost every log when the endpoint was misrouted. The real fix is a durable local
  spool or the Phase 2 queue.
- **Schema is created via `create_all()`,** not migrations. Alembic is the
  production answer.
- **No PII redaction yet.** Previews are stored as-is. Phase 2 adds a regex pass
  before persist.
- **No auth or rate limiting** on any endpoint, including `/ingest`.
- **The frontend Docker image runs the Vite dev server,** so `VITE_API_URL` stays
  runtime-configurable. Production would build the static bundle and serve it
  behind nginx.

---

## Roadmap

**Phase 1 — complete**
Postgres schema · conversation/message CRUD · multi-turn chat with trimmed history ·
provider adapter interface · instrumentation wrapper · `/ingest` with validation ·
React chat UI with list, resume, and live log panel · Docker Compose · tests

**Phase 2 — event architecture and bonuses**
Celery + Redis (swap the body of `ingest_log()` for an enqueue) · SSE streaming ·
cancel in-flight generation · PII redaction in the worker · latency/error/throughput
dashboards

**Phase 3**
Multi-provider verification across Claude/GPT/Gemini · k8s manifests

### Scaling notes

The current bottleneck is that ingestion writes synchronously to Postgres inside
the request. Phase 2's queue removes that: `/ingest` returns as soon as the task is
enqueued, and the worker absorbs database slowness. Beyond a single Redis instance,
a durable broker (RabbitMQ, or Kafka for replayable history) would be the next step,
with workers scaled horizontally on queue depth.

Worth being precise: Celery + Redis is **queue-based decoupling, not a durable
replayable event log**. Kafka or Redis Streams is the purer event-driven answer;
the tradeoff here is familiarity and speed of delivery.
