"""Durable local spool for log events.

The gap this closes: the wrapper POSTs log events to the ingestion endpoint, and
if that POST fails the event is gone. Everything *downstream* of `/ingest` is
durable — validated, queued, retried, idempotently written — but the first hop
was best-effort, so an ingestion outage silently lost observability data.

Design: write the event to disk **before** attempting delivery, delete the file
on success, and replay leftovers in the background. A crash between write and
delivery leaves a file behind, which is the recoverable outcome; the alternative
is a lost event with no trace.

Deliberately a directory of JSON files rather than SQLite or a real WAL. One
file per event is atomic via `os.rename`, needs no schema or locking, survives
`kill -9`, and is trivially inspectable when debugging. The cost is filesystem
overhead per event, which is irrelevant at the volume a single API process
generates.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Stop unbounded growth if ingestion is down for a long time. Oldest entries are
# dropped first: recent telemetry is more actionable than stale telemetry.
MAX_SPOOLED_EVENTS = 10_000


class EventSpool:
    """A crash-safe on-disk queue of pending log events."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: dict) -> Path | None:
        """Persist an event before delivery is attempted.

        Written to a temp file then renamed: `os.rename` is atomic on POSIX, so
        a reader never observes a half-written file.
        """
        try:
            self._enforce_capacity()

            # Monotonic prefix keeps filenames sortable by age; the event id
            # makes them unique and traceable back to a specific call.
            name = f"{time.time_ns()}-{event.get('id', 'unknown')}.json"
            target = self._dir / name

            fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(event, handle)
                    handle.flush()
                    # fsync so the event survives power loss, not just a crash.
                    os.fsync(handle.fileno())
                os.rename(tmp_path, target)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise

            return target
        except OSError as exc:
            # A broken spool must not break chat. Delivery still proceeds; the
            # event simply loses its durability guarantee.
            logger.warning("Could not spool event %s: %s", event.get("id"), exc)
            return None

    def discard(self, path: Path | None) -> None:
        """Remove an event after confirmed delivery."""
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove spooled event %s: %s", path.name, exc)

    def pending(self) -> list[Path]:
        """Spooled events, oldest first."""
        try:
            return sorted(self._dir.glob("*.json"))
        except OSError:
            return []

    def read(self, path: Path) -> dict | None:
        """Load a spooled event, discarding it if it is unreadable.

        A corrupt file can never become valid, so retrying it forever would
        block the spool. Dropped loudly rather than silently.
        """
        try:
            with path.open() as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            logger.error("Discarding unreadable spooled event %s: %s", path.name, exc)
            self.discard(path)
            return None

    def _enforce_capacity(self) -> None:
        with self._lock:
            entries = self.pending()
            excess = len(entries) - MAX_SPOOLED_EVENTS
            if excess <= 0:
                return

            logger.warning(
                "Spool over capacity (%d); dropping %d oldest event(s)",
                len(entries),
                excess,
            )
            for path in entries[:excess]:
                self.discard(path)
