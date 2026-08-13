# Kubernetes deployment

Manifests for running the stack on a self-hosted cluster (k3s, kind, minikube, or
a managed cluster).

```bash
# Build images into the cluster's registry first. For kind:
docker build -t llm-logs-backend:latest -f backend/Dockerfile .
docker build -t llm-logs-worker:latest  -f worker/Dockerfile .
docker build -t llm-logs-frontend:latest ./frontend
kind load docker-image llm-logs-backend:latest llm-logs-worker:latest llm-logs-frontend:latest

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
| Stateless services | `Deployment`s for backend, worker, frontend |
| Health | `readinessProbe` on `/health` so traffic only reaches ready pods; `livenessProbe` to restart wedged ones |
| Ordering | `initContainer` waits for Postgres rather than relying on restart loops |
| Autoscaling | `HorizontalPodAutoscaler` on the worker |

## The one decision worth explaining

**The worker's HPA scales on queue depth, not CPU.**

CPU is the wrong signal for a queue consumer. A worker blocked on a slow database
uses almost no CPU while the backlog grows — CPU-based autoscaling would sit at one
replica through exactly the incident where you need more. The metric that matters is
how many log events are waiting in Redis.

That requires a custom metrics adapter (KEDA is the usual answer, with its Redis
scaler). The manifest here includes the KEDA `ScaledObject` and a CPU-based HPA
commented out beside it, so the intent is explicit either way.

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
- No `NetworkPolicy`. Postgres and Redis should only accept traffic from the backend
  and worker.
- Secrets are plain `Secret` manifests. Use External Secrets Operator or Sealed
  Secrets so they are not committed.
- No Ingress/TLS — port-forward is assumed for local clusters.
