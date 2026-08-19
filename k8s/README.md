# Kubernetes deployment

Manifests for running the stack on a self-hosted cluster (k3s, kind, minikube, or
a managed cluster).

```bash
# Build images into the cluster's registry first. For kind:
docker build -t llm-logs-backend:latest -f backend/Dockerfile .
docker build -t llm-logs-frontend:latest ./frontend
kind load docker-image llm-logs-backend:latest llm-logs-frontend:latest

kubectl apply -k k8s/
kubectl -n llm-logs get pods -w
```

Then port-forward, or point an ingress at the frontend service:

```bash
kubectl -n llm-logs port-forward svc/frontend 5173:5173
kubectl -n llm-logs port-forward svc/backend 8000:8000
```

## What is modelled here

| Concern | Approach |
|---|---|
| Config | `ConfigMap` for non-secret settings, `Secret` for the provider API key |
| Stateful services | Postgres and Redis as `StatefulSet`s with `PersistentVolumeClaim`s |
| Stateless services | `Deployment`s for backend and frontend |
| Health | `readinessProbe` on `/health` so traffic only reaches ready pods; `livenessProbe` to restart wedged ones |
| Ordering | `initContainer` waits for Postgres rather than relying on restart loops |
| Autoscaling | `HorizontalPodAutoscaler` on the backend, on CPU |

## The one decision worth explaining

**Telemetry durability lives in the producer, not in the cluster.**

An earlier revision ran a Celery worker consuming log events from Redis, with a
KEDA `ScaledObject` scaling it on queue depth — the right signal for a queue
consumer, since a worker blocked on a slow database burns no CPU while the
backlog grows.

That queue is gone. The `llmlog` SDK writes each event to an on-disk spool
*before* attempting delivery and replays it until the collector confirms
acceptance, so durability is guaranteed one hop earlier than a broker could
guarantee it — including against a total collector outage, which a broker
sitting behind the collector cannot help with. Ingestion is now a synchronous
idempotent insert inside the API.

The consequence for this cluster: there is no backlog to scale on and no
consumer to scale, so the backend is a plain request-serving deployment and CPU
is an honest signal for it. Under ingestion pressure producers spool and replay,
so a slow scale-up delays events rather than dropping them.

## Honest status

These manifests are **written but not applied to a live cluster** — no cluster was
provisioned for this exercise. They are structurally complete and internally
consistent (`kubectl apply --dry-run=client` passes), but treat them as a design
artifact rather than a verified deployment. The Compose stack is the path that has
been run end to end.

Known gaps for a production cluster:

- Postgres and Redis run as single-replica StatefulSets. Real deployments should use
  managed services or an operator (CloudNativePG, Redis Operator) for backups,
  failover, and upgrades.
- No `NetworkPolicy`. Postgres and Redis should only accept traffic from the backend.
- Secrets are plain `Secret` manifests. Use External Secrets Operator or Sealed
  Secrets so they are not committed.
- No Ingress/TLS — port-forward is assumed for local clusters.
