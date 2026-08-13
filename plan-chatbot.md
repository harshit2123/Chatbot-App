# LLM Inference Logging & Ingestion System — Build Plan

## 0. Reality check

Full spec + every bonus is 4-6 focused days. Core deliverables done well is ~2 days.
Priority order: nail the 4 core deliverables first, then add bonuses in order of
effort-to-signal ratio. Skip self-hosted k8s unless everything else is done with
a full day to spare.

---

## 1. Requirement coverage checklist

### Core deliverables

| # | Requirement | Covered by | Status |
|---|---|---|---|
| 1a | Multi-turn chatbot | React chat UI + `conversations`/`messages` tables, full history sent as context on each turn | Planned |
| 1b | Short conversational context | Backend trims history to last N turns (or token budget) before calling the model | Planned |
| 1c | Simple UI | React (Vite) chat page | Planned |
| 2a | SDK/wrapper capturing metadata (model, provider, latency, tokens, timestamps, status/errors, session id, input/output preview) | Python wrapper around every `get_completion()` call (OpenRouter behind an adapter interface) | Planned |
| 2b | Auto-instrument (note) | Wrapper is a single decorator/context manager applied at the one call site — not per-provider code, so it's "auto" in the sense that adding a new provider needs zero new logging code | Planned |
| 2c | Sends logs near real time | Wrapper pushes to ingestion endpoint immediately after each call returns (or streams per-chunk for streaming calls) | Planned |
| 3a | Ingestion service receives logs | FastAPI `POST /ingest` endpoint | Planned |
| 3b | Validates/parses payloads | Pydantic schema on the endpoint | Planned |
| 3c | Extracts metadata | Done in the consumer worker (see event flow below) | Planned |
| 3d | Stores processed data in DB | Postgres | Planned |
| 4a | Store chat messages | `messages` table | Planned |
| 4b | Store inference logs | `inference_logs` table | Planned |
| 4c | Store extracted metadata | Same table, structured columns (not a JSON blob dump) | Planned |

### Deliverables

| Deliverable | Plan |
|---|---|
| GitHub repo | Monorepo, structure below |
| README | Setup, architecture, schema decisions, tradeoffs, future work — write this last, from the actual code, not up front |
| Architecture notes | Ingestion flow / logging strategy / scaling / failure handling — can reuse most of this plan doc |
| Demo | Loom walkthrough + a few screenshots (chat, dashboard, resume flow). Hosted link only if you deploy somewhere free like Render/Fly — don't burn time on this unless core is done early |

### Frontend requirements

| Requirement | Plan | Status |
|---|---|---|
| Cancel a conversation | `AbortController` on the frontend fetch + backend checks a per-request cancel flag (Redis key) inside the streaming loop and stops early | Planned |
| List conversations | `GET /conversations` → sidebar list | Planned |
| Resume a conversation | `GET /conversations/:id/messages` → hydrate chat state, continue posting to same conversation id | Planned |

### Bonus items — do / skip decision

| Bonus | Decision | Why |
|---|---|---|
| Multi-provider support | **Do** | OpenRouter gives this almost for free — one endpoint, swap the `model` string |
| Streaming responses | **Do** | SSE, moderate effort, high visible payoff |
| Latency/throughput/error dashboards | **Do** | SQL aggregation endpoints + Recharts in the same React app, not a separate Grafana stack |
| Docker Compose one-command setup | **Do** | Non-negotiable, low effort, expected baseline |
| Event-based architecture | **Do** | Celery + Redis broker decouples ingestion from processing — real async architecture, though a task queue rather than a durable event log; note the distinction in the README rather than overselling it as Kafka-grade streaming |
| PII redaction | **Do** | Regex pass (email/phone/card patterns) on input/output previews in the worker, before persist. Cheap, high signal |
| Deploy on self-hosted k8s | **Skip unless spare time** | Lowest ROI for a take-home; write a strong "how I'd do this" paragraph in the README instead |

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | React (Vite) + TypeScript + Tailwind + shadcn/ui + Recharts + React Router | CSR is fine here, no SSR needed for an internal tool; matches your existing React experience |
| Streaming to frontend | SSE via `EventSource`/`fetch` + `ReadableStream` | Simpler than websockets for one-directional token streaming |
| Backend / API | Python + FastAPI | Async-native, Pydantic gives free payload validation, natural fit for LLM call orchestration |
| LLM abstraction | OpenRouter (single API key, OpenAI-compatible endpoint) behind a thin adapter interface — `litellm` is the fallback if you want native provider SDKs instead | OpenRouter routes to Claude/GPT/Gemini/Llama/DeepSeek/Grok by just changing the `model` string, and returns which provider actually served the request (fills the `provider` metadata field). Wrap it behind `get_completion(provider, model, messages)` so it's swappable — reads as deliberate multi-provider engineering, not just "we called one aggregator," in case an evaluator wants to see you handle providers directly |
| Queue | Celery + Redis (broker) | You already know Celery + Redis from your Watchlist system at Uniqode — near-zero new-tool risk, battle-tested retries and task routing, Flower gives a monitoring dashboard for free. Tradeoff: Celery is a task queue, not a durable replayable event log like Redis Streams/Kafka — call this out in the README as "queue-based async processing," not literal event streaming, so it doesn't oversell the mechanism |
| Worker | Celery worker process (separate container) consuming the log-processing task | Task: validate → redact PII → extract metadata → write to Postgres |
| Database | Postgres | Relational joins across conversations/messages/logs, good for aggregation queries |
| PII redaction | Regex (email/phone/card) or Presidio if time allows | Applied in worker before persist |
| Dashboards | SQL aggregation endpoints in FastAPI + Recharts in React | Faster than standing up Prometheus/Grafana, shows product sense |
| Containerization | Docker Compose (frontend, backend, worker, redis, postgres) | Required deliverable |
| Auth to LLM providers | `.env` API keys, never committed | Basic hygiene |

---

## 3. Architecture (matches the diagram shown earlier)

```
React frontend (chat + dashboard)
      |
      v
FastAPI backend (SSE chat endpoint, calls get_completion())
      |         \
      |          -> LLM providers (Claude / GPT / Gemini via OpenRouter)
      v
SDK wrapper emits log event
      v
Celery queue (Redis broker)
      v
Celery worker (validate -> redact PII -> extract metadata)
      v
Postgres (conversations, messages, inference_logs)
      ^
      |
Dashboard queries hit Postgres directly via aggregation endpoints
```

**Ingestion flow:** every LLM call goes through the wrapper → wrapper builds a log
event (model, provider, latency, tokens, timestamps, status, session id, truncated
input/output) → pushes to `POST /ingest` → ingestion endpoint validates shape with
Pydantic and enqueues a Celery task (broker: Redis) instead of writing to Postgres
directly → Celery worker picks up the task, redacts PII, extracts/normalizes
metadata, writes to Postgres.

**Why decouple ingestion API from DB writes:** if Postgres is slow or briefly down,
the ingestion endpoint still returns fast and nothing is lost — the task sits in
the broker until a worker is free. This is the actual point of the async-processing
bonus. Worth noting honestly: this is queue-based decoupling, not a durable
replayable event log — Redis Streams or Kafka would be the "purer" event-driven
answer, and that's the tradeoff being made for familiarity and speed here.

**Failure handling assumptions:**
- Ingestion endpoint: malformed payload → 422, task never gets enqueued (dead-letter
  logging to stdout for now, not a real DLQ — call this out as a known gap).
- Worker crash mid-task: set `acks_late=True` and `worker_prefetch_multiplier=1` so
  an unfinished task is redelivered to another worker rather than lost; writes are
  idempotent on log id so a redelivered task doesn't double-insert.
- LLM call failure: still logged (status=error, error_message populated), so the
  dashboard can show error rate, not just successful calls.

**Scaling considerations (for README):** Celery + Redis broker → Celery with a
RabbitMQ or Kafka-backed broker if volume grows past a single Redis instance and
you need durable replay; worker scales horizontally by adding more Celery worker
replicas/concurrency; Docker Compose → k8s with HPA on worker pods keyed on queue depth.

---

## 4. Database schema

```sql
create table conversations (
  id uuid primary key default gen_random_uuid(),
  title text,
  status text not null default 'active', -- active | cancelled
  created_at timestamptz not null default now()
);

create table messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id),
  role text not null, -- user | assistant
  content text not null,
  created_at timestamptz not null default now()
);

create table inference_logs (
  id uuid primary key default gen_random_uuid(),
  message_id uuid references messages(id),
  conversation_id uuid not null references conversations(id),
  provider text not null,
  model text not null,
  latency_ms integer,
  prompt_tokens integer,
  completion_tokens integer,
  status text not null, -- success | error
  error_message text,
  input_preview text,   -- truncated + PII-redacted
  output_preview text,  -- truncated + PII-redacted
  created_at timestamptz not null default now()
);
```

**Tradeoffs to state in README:**
- Storing truncated, redacted previews, not full payloads — deliberate storage
  and privacy tradeoff.
- `inference_logs` is 1:1 with an assistant message — simple, but doesn't model
  multi-hop tool-call traces. Known limitation, not an oversight.
- No separate `users` table since the spec doesn't require auth — call out as
  the first thing you'd add for a real deployment.

---

## 5. API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/conversations` | Create a conversation |
| GET | `/conversations` | List conversations |
| GET | `/conversations/:id/messages` | Resume — fetch history |
| POST | `/conversations/:id/messages` | Send a message, SSE stream back the response |
| POST | `/conversations/:id/cancel` | Cancel an in-flight generation |
| POST | `/ingest` | SDK wrapper posts log events here |
| GET | `/metrics/latency` | Aggregated latency for dashboard |
| GET | `/metrics/errors` | Error rate over time |
| GET | `/metrics/throughput` | Requests per minute |

---

## 6. Repo structure

```
llm-log-system/
  frontend/            # React + Vite
    src/
      pages/            (Chat, Dashboard, ConversationList)
      components/
      hooks/
  backend/              # FastAPI
    app/
      api/              (routes: chat, conversations, ingest, metrics)
      sdk/              (the logging wrapper, provider-agnostic via the adapter interface)
      models/           (Pydantic schemas)
      db/               (SQLAlchemy models, migrations)
  worker/               # Celery worker
    celery_app.py
    tasks.py
    pii.py
  docker-compose.yml
  README.md
```

---

## 7. Build order

**Phase 1 — core, no fanciness**
1. Postgres schema + FastAPI CRUD for conversations/messages
2. Chatbot: multi-turn, session id, single provider via OpenRouter
3. SDK wrapper capturing full metadata list, posting to `/ingest`
4. `/ingest` validates + writes straight to Postgres (swap to queue in phase 2)
5. React chat UI: send message, list conversations, resume a conversation

**Phase 2 — event architecture + bonuses, in order**
1. Swap direct DB write for Celery task + Redis broker + worker
2. SSE streaming responses
3. Cancel conversation (AbortController + backend cancel flag)
4. Docker Compose wiring everything together
5. PII redaction in the worker
6. Dashboard: 3 charts (latency, error rate, throughput) via Recharts

**Phase 3 — only with time left**
- Multi-provider polish (test all 3+ providers actually work end to end)
- Self-hosted k8s manifests, or just a strong README paragraph on the approach

---

## 8. Hosting (fully free path, no expiry surprises)

Don't try to run the whole docker-compose stack on one box for a demo — split it
across free-tier PaaS platforms instead:

| Piece | Where | Why |
|---|---|---|
| Frontend (React/Vite) | Vercel | Free, purpose-built, zero config |
| Backend API | Render free web service | Cold-starts after inactivity — fine for a recorded demo |
| Worker | Render background worker if the free tier includes one at deploy time (check the dashboard — sources disagree on this); if not, a second free Render web service running `celery -A worker worker` with a dummy health-check route works as a workaround, since Celery needs its own long-running process and can't be folded into the API's request/response cycle the way an asyncio loop could |
| Redis | Upstash free tier | Serverless, generous free quota, no expiration — safer than Render's free Key Value, whose data isn't guaranteed to persist across restarts |
| Postgres | Supabase or Neon free tier | Free and does not expire — Render's own free Postgres has a hard expiration window (reported anywhere from 30-90 days depending on source), which is a bad surprise mid-project |

**Known tradeoff:** Render's free web service cold-starts after ~15 minutes of
inactivity. Irrelevant for a recorded Loom demo where you control timing; annoying
if someone clicks a live link cold. If a hosted link is part of the deliverable,
mention the cold start in the README so it doesn't read as broken.

**Steps:**
1. Push repo to GitHub.
2. Vercel → import repo → point at `frontend/`, set `VITE_API_URL` env var to the Render backend URL once it exists.
3. Supabase/Neon → create free Postgres → copy connection string.
4. Upstash → create free Redis → copy connection string.
5. Render → new web service → point at `backend/` (Dockerfile or native Python build) → set env vars: `DATABASE_URL`, `REDIS_URL`, `OPENROUTER_API_KEY` (or individual provider keys).
6. Render → second service (background worker if available, else another free web service) → point at `worker/` → start command `celery -A worker worker --loglevel=info` → same env vars as the backend.

---

## 9. README skeleton (write last, from actual code)

1. Setup instructions
2. Architecture overview (reuse the diagram/flow above)
3. Schema design decisions
4. Tradeoffs made
5. What you'd improve with more time (name k8s / full Kafka here explicitly —
   shows deliberate scoping, not running out of time)
