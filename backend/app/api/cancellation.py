"""Cancellation flags for in-flight generations.

Kept in Redis rather than process memory so the cancel request works even when
it lands on a different API worker than the one holding the open stream — which
is the normal case behind any load balancer.

Falls back to an in-process set when Redis is unavailable, so the feature still
works for single-process local runs instead of failing outright.
"""

from __future__ import annotations

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "cancel:conversation:"

# Fallback store. Correct only for a single process; Redis is the real path.
_local_flags: set[str] = set()


def _redis_client():
    """Return a Redis client, or None if unreachable."""
    try:
        import redis

        client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=1)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 - any failure means use the fallback
        logger.debug("Redis unavailable for cancellation flags: %s", exc)
        return None


def request_cancel(conversation_id: str) -> None:
    settings = get_settings()
    client = _redis_client()

    if client is None:
        _local_flags.add(conversation_id)
        return

    # TTL so an abandoned flag cannot cancel a future generation.
    client.setex(f"{_KEY_PREFIX}{conversation_id}", settings.cancel_flag_ttl_seconds, "1")


def is_cancelled(conversation_id: str) -> bool:
    client = _redis_client()
    if client is None:
        return conversation_id in _local_flags

    return client.exists(f"{_KEY_PREFIX}{conversation_id}") == 1


def clear_cancel(conversation_id: str) -> None:
    """Clear the flag so the next generation on this conversation starts clean."""
    _local_flags.discard(conversation_id)

    client = _redis_client()
    if client is not None:
        client.delete(f"{_KEY_PREFIX}{conversation_id}")
