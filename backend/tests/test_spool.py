"""Durable spool tests.

The spool exists so an ingestion outage or a crash cannot silently lose
telemetry. Its failure mode is invisible at runtime — events just stop arriving
— so the guarantees are pinned down here.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from llmlog.client import LogClient
from llmlog.config import LogConfig
from llmlog.spool import EventSpool


@pytest.fixture
def spool_dir(tmp_path):
    return tmp_path / "spool"


@pytest.fixture
def client(spool_dir) -> LogClient:
    """A throwaway client per test — no module-global state to reset."""
    return LogClient(
        LogConfig(
            ingest_url="http://ingest.invalid/ingest",
            spool_enabled=True,
            spool_dir=str(spool_dir),
        )
    )


def _event(**overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "conversation_id": str(uuid.uuid4()),
        "provider": "mock",
        "model": "m",
        "status": "success",
        "latency_ms": 5,
    }
    return {**base, **overrides}


def _wait_for_delivery(client: LogClient):
    """Block until the client's background delivery pool has drained."""
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        if client._pool._work_queue.empty():
            time.sleep(0.1)
            return
        time.sleep(0.02)


# --------------------------------------------------------------------------
# The core guarantee: a failed delivery leaves a recoverable record.
# --------------------------------------------------------------------------


def test_event_survives_a_failed_delivery(client, spool_dir, monkeypatch):
    """This is the whole point: ingestion down must not mean data lost."""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down"))
    )

    client.emit(_event())
    _wait_for_delivery(client)

    pending = list(spool_dir.glob("*.json"))
    assert len(pending) == 1, "a failed delivery must leave the event on disk"


def test_spool_entry_is_removed_after_successful_delivery(client, spool_dir, monkeypatch):
    """The spool must not grow without bound when everything is healthy."""

    class OK:
        status_code = 202
        text = ""

    monkeypatch.setattr(httpx, "post", lambda *a, **k: OK())

    client.emit(_event())
    _wait_for_delivery(client)

    assert list(spool_dir.glob("*.json")) == []


def test_event_is_written_before_delivery_is_attempted(client, spool_dir, monkeypatch):
    """Write-then-send, not send-then-write: a crash mid-POST must be recoverable."""
    observed: list[int] = []

    def post_spy(*args, **kwargs):
        # By the time the POST happens, the file must already exist.
        observed.append(len(list(spool_dir.glob("*.json"))))
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", post_spy)

    client.emit(_event())
    _wait_for_delivery(client)

    assert observed == [1]


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def test_replay_delivers_pending_events_once_ingestion_recovers(
    client, spool_dir, monkeypatch
):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down"))
    )
    for _ in range(3):
        client.emit(_event())
    _wait_for_delivery(client)
    assert len(list(spool_dir.glob("*.json"))) == 3

    class OK:
        status_code = 202
        text = ""

    monkeypatch.setattr(httpx, "post", lambda *a, **k: OK())

    assert client.replay() == 3
    assert list(spool_dir.glob("*.json")) == []


def test_replay_stops_early_while_ingestion_is_still_down(client, spool_dir, monkeypatch):
    """Don't hammer a downed endpoint with the entire backlog every cycle."""
    spool = client._spool
    for _ in range(5):
        spool.write(_event())

    attempts = {"n": 0}

    def failing_post(*args, **kwargs):
        attempts["n"] += 1
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(httpx, "post", failing_post)

    assert client.replay() == 0
    assert attempts["n"] == 1, "should stop after the first failure, not retry all 5"
    assert len(list(spool_dir.glob("*.json"))) == 5


def test_permanently_rejected_event_is_discarded_not_retried_forever(
    client, spool_dir, monkeypatch
):
    """A 422 will never become valid; keeping it would block the whole spool."""

    class Rejected:
        status_code = 422
        text = "validation error"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Rejected())

    client.emit(_event())
    _wait_for_delivery(client)

    assert list(spool_dir.glob("*.json")) == [], "terminal rejection must clear the entry"


def test_transient_5xx_keeps_the_event_for_retry(client, spool_dir, monkeypatch):
    """A 503 is the server's problem, not the payload's — keep it."""

    class Unavailable:
        status_code = 503
        text = "unavailable"

    monkeypatch.setattr(httpx, "post", lambda *a, **k: Unavailable())

    client.emit(_event())
    _wait_for_delivery(client)

    assert len(list(spool_dir.glob("*.json"))) == 1


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


def test_corrupt_spool_file_is_dropped_rather_than_blocking_replay(client, spool_dir):
    spool = client._spool
    spool.write(_event())

    corrupt = spool_dir / "9999999999-corrupt.json"
    corrupt.write_text("{not valid json")

    # read() discards unreadable entries instead of raising.
    assert spool.read(corrupt) is None
    assert not corrupt.exists()


def test_spool_write_is_atomic_no_partial_files_visible(client, spool_dir):
    """Readers must never see a half-written event."""
    spool = client._spool
    for _ in range(20):
        spool.write(_event())

    for path in spool_dir.glob("*.json"):
        # Every visible .json file parses; temp files use a .tmp suffix.
        json.loads(path.read_text())

    assert list(spool_dir.glob("*.tmp")) == []


def test_spool_enforces_a_capacity_ceiling(client, spool_dir, monkeypatch):
    """A long outage must not fill the disk."""
    import llmlog.spool as spool_module

    monkeypatch.setattr(spool_module, "MAX_SPOOLED_EVENTS", 5)
    spool = EventSpool(spool_dir)

    for _ in range(12):
        spool.write(_event())

    # Capacity is enforced before each write, so the ceiling holds.
    assert len(list(spool_dir.glob("*.json"))) <= 6


def test_spooling_can_be_disabled(tmp_path, monkeypatch):
    """Opt-out must not break delivery."""
    client = LogClient(
        LogConfig(
            ingest_url="http://ingest.invalid/ingest",
            spool_enabled=False,
            spool_dir=str(tmp_path / "unused"),
        )
    )

    class OK:
        status_code = 202
        text = ""

    monkeypatch.setattr(httpx, "post", lambda *a, **k: OK())

    client.emit(_event())
    _wait_for_delivery(client)

    assert client._spool is None
    assert not (tmp_path / "unused").exists()


def test_a_broken_spool_directory_does_not_break_chat(client, monkeypatch):
    """Logging failures must never propagate into the request path."""
    import llmlog.spool as spool_module

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(spool_module.tempfile, "mkstemp", explode)

    class OK:
        status_code = 202
        text = ""

    monkeypatch.setattr(httpx, "post", lambda *a, **k: OK())

    # Must not raise.
    client.emit(_event())
    _wait_for_delivery(client)
