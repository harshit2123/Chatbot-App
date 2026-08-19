"""Event delivery: spool, POST, replay.

The durability contract, which is the whole reason this layer exists:

1. **Delivery must not block the caller.** The host application may be POSTing
   to an endpoint served by its own process; doing that synchronously from
   inside a streaming request deadlocks, since the worker busy streaming cannot
   answer itself. Delivery therefore runs on a bounded background pool.

2. **Delivery must not lose data.** The event is written to a durable on-disk
   spool *before* the POST is attempted, and removed only once the endpoint
   confirms acceptance. If the collector is down, the process crashes, or the
   machine loses power, the event is still on disk and the replay loop picks it
   up.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from llmlog.config import LogConfig
from llmlog.spool import EventSpool

logger = logging.getLogger(__name__)

# Status codes that mean "try again later" rather than "this payload is bad".
_RETRYABLE_CLIENT_CODES = (408, 429)


class LogClient:
    """Owns the spool, the delivery pool, and the replay thread.

    An instance rather than module globals: two clients pointed at different
    collectors can coexist, and tests can build a throwaway one instead of
    monkeypatching module state.
    """

    def __init__(self, config: LogConfig | None = None) -> None:
        self._config = config or LogConfig()
        self._spool = (
            EventSpool(Path(self._config.spool_dir))
            if self._config.spool_enabled
            else None
        )
        # Bounded so a stuck collector cannot spawn threads without limit;
        # daemon threads so delivery never blocks interpreter shutdown.
        self._pool = ThreadPoolExecutor(
            max_workers=self._config.delivery_workers,
            thread_name_prefix="llmlog-delivery",
        )
        self._replay_thread: threading.Thread | None = None

    @property
    def config(self) -> LogConfig:
        return self._config

    def emit(self, event: dict) -> None:
        """Spool, then ship — without blocking the caller."""
        spooled = self._spool.write(event) if self._spool is not None else None

        try:
            self._pool.submit(self._deliver, event, spooled)
        except RuntimeError as exc:
            # Pool shut down (interpreter exiting). Send inline so an event
            # emitted during shutdown is not left for a replay that may never run.
            logger.debug("Delivery pool unavailable, sending inline: %s", exc)
            self._deliver(event, spooled)

    def post(self, event: dict) -> bool:
        """POST one event. Returns True only on confirmed acceptance."""
        try:
            response = httpx.post(
                self._config.ingest_url,
                json=event,
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            logger.warning("Failed to deliver log event %s: %s", event.get("id"), exc)
            return False

        if response.status_code < 400:
            return True

        # A 4xx that is not 408/429 means the payload itself is unacceptable —
        # retrying forever would block the spool behind a permanently bad event.
        if (
            400 <= response.status_code < 500
            and response.status_code not in _RETRYABLE_CLIENT_CODES
        ):
            logger.error(
                "Collector permanently rejected log %s: HTTP %s %s",
                event.get("id"),
                response.status_code,
                response.text[:200],
            )
            return True  # terminal, so the spool entry is discarded

        logger.warning(
            "Collector rejected log %s: HTTP %s %s",
            event.get("id"),
            response.status_code,
            response.text[:200],
        )
        return False

    def _deliver(self, event: dict, spooled: Path | None) -> None:
        """Deliver an event and clear its spool entry on success.

        A failure deliberately leaves the file in place: the replay loop will
        retry it, and a crash mid-delivery is indistinguishable from a failure,
        which is the behavior we want.
        """
        if self.post(event) and self._spool is not None:
            self._spool.discard(spooled)

    def replay(self) -> int:
        """Retry every pending spooled event. Returns the number delivered."""
        if self._spool is None:
            return 0

        delivered = 0
        for path in self._spool.pending():
            event = self._spool.read(path)
            if event is None:
                continue  # unreadable; already discarded
            if not self.post(event):
                # Collector is still down. Stop rather than hammering it with
                # the whole backlog on every pass.
                break
            self._spool.discard(path)
            delivered += 1

        if delivered:
            logger.info("Replayed %d spooled log event(s)", delivered)
        return delivered

    def start_replay_worker(self) -> threading.Thread | None:
        """Background loop that drains the spool once the collector recovers."""
        if self._spool is None or self._replay_thread is not None:
            return self._replay_thread

        def loop() -> None:
            while True:
                time.sleep(self._config.replay_interval_seconds)
                try:
                    self.replay()
                except Exception as exc:  # noqa: BLE001 - the loop must never die
                    logger.warning("Spool replay failed: %s", exc)

        # Daemon: replay is best-effort catch-up, never a reason to block shutdown.
        thread = threading.Thread(target=loop, name="llmlog-replay", daemon=True)
        thread.start()
        self._replay_thread = thread
        return thread
