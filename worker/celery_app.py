"""Celery application shared by the API (producer) and the worker (consumer)."""

import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("llm_logs", broker=REDIS_URL, backend=REDIS_URL)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Redeliver rather than lose work if a worker dies mid-task. Safe here only
    # because the DB write is idempotent on the producer-supplied log id.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # Bound retries so a permanently malformed task cannot loop forever.
    task_default_retry_delay=5,
    task_annotations={"*": {"max_retries": 3}},
    # Silences a startup warning and keeps the worker retrying if the broker
    # is not up yet (compose starts them concurrently).
    broker_connection_retry_on_startup=True,
)

# Registers the task on import for both producer and consumer. Imported at the
# bottom to avoid a circular import, since tasks.py imports celery_app above.
celery_app.autodiscover_tasks(["worker.tasks"], force=True)
