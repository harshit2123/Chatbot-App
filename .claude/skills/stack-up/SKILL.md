---
name: stack-up
description: Bring up the local docker compose stack (postgres, redis, backend, worker, frontend), wait for healthchecks, and print the service URLs.
disable-model-invocation: true
---

```bash
docker compose up --build -d
```

Then wait for health. Poll until `postgres` and `redis` report healthy:

```bash
docker compose ps
```

Do not use a fixed `sleep` — both services declare healthchecks, so poll `docker
compose ps` until their status column shows `healthy` (or until 90s have passed,
then report what is still not healthy along with `docker compose logs --tail 30`
for that service).

Once healthy, print exactly:

- Chat UI → http://localhost:5173
- Dashboard → http://localhost:5173 → "Observability"
- API docs → http://localhost:8000/docs

Provider defaults to `mock`, so no API key is required. If `$ARGUMENTS` names a
provider (e.g. `openrouter`), set `LLM_PROVIDER` in the compose invocation and
confirm the matching key is present in the environment before starting — do not
read `backend/.env` to check.
