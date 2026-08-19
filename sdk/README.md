# llmlog

Drop-in durable telemetry for LLM calls. Provider-agnostic.

`llmlog` never makes a model call and never imports a provider SDK. You make the
call; it records that you made it, how long it took, and how it ended — then
guarantees the record survives a collector outage, a crash, or a power cut.

## Install

```bash
pip install -e ./sdk
```

## Use

```python
import llmlog

llmlog.configure(ingest_url="http://collector:8000/ingest")
llmlog.start_replay_worker()

with llmlog.record(model="gpt-4", input_text=prompt) as span:
    reply = my_client.complete(prompt)
    span.succeeded(output=reply.text, completion_tokens=reply.usage.output)
```

Streaming, including cancellation and client-disconnect:

```python
def to_delta(span, chunk):
    if chunk.done:
        span.succeeded(completion_tokens=chunk.usage)
        return None          # swallow the usage-only frame
    return chunk.text

for chunk in llmlog.record_stream(provider_iter, model="gpt-4", on_chunk=to_delta):
    yield chunk
```

## Durability

Events are written to an on-disk spool **before** delivery is attempted and
removed only on confirmed acceptance. Delivery runs on a bounded background
pool, so it never blocks the call it measures. A background replay loop drains
the spool once the collector recovers.

A generation can end four ways — completion, provider error, caller
cancellation, and the consumer walking away mid-stream. All four emit exactly
one event. An unlogged model call is the one outcome this library must never
have.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| `ingest_url` | `LLMLOG_INGEST_URL` | `http://127.0.0.1:8000/ingest` |
| `spool_enabled` | `LLMLOG_SPOOL_ENABLED` | `true` |
| `spool_dir` | `LLMLOG_SPOOL_DIR` | `/tmp/llmlog-spool` |
| `replay_interval_seconds` | `LLMLOG_REPLAY_INTERVAL_SECONDS` | `30` |
| `preview_max_chars` | `LLMLOG_PREVIEW_MAX_CHARS` | `500` |

`LogConfig()` alone is enough to start — every field has a working default.

## Collector contract

Events are POSTed as JSON to `ingest_url`. Any 2xx means accepted. A 4xx other
than 408/429 is treated as terminal and the event is dropped rather than
retried forever; 5xx, 408, and 429 are retried from the spool.
