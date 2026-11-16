"""At-least-once MongoDB outbox delivery; duplicate event IDs are harmless."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from .db import get_db
from .events import clickhouse_config, clickhouse_request, event_row

logger = logging.getLogger(__name__)
_schema_lock = threading.Lock()
_schema_ready_for: set[tuple[str, str, str]] = set()


def ensure_analytics_table() -> None:
    config = clickhouse_config()
    identity = (config["url"], config["database"], config["user"])
    with _schema_lock:
        if identity in _schema_ready_for:
            return
        # The ClickHouse image creates CLICKHOUSE_DATABASE. This also supports
        # configured database names other than the init.sql default, gym.
        clickhouse_request("""CREATE TABLE IF NOT EXISTS analytics_events (
            event_id String, event_type LowCardinality(String), occurred_at DateTime64(3, 'UTC'),
            session_id String DEFAULT '', booking_id String DEFAULT '', trainer_id String DEFAULT '', payload String,
            INDEX event_type_index event_type TYPE set(32) GRANULARITY 1,
            INDEX session_id_index session_id TYPE bloom_filter(0.01) GRANULARITY 1
        ) ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(occurred_at) ORDER BY event_id""")
        _schema_ready_for.add(identity)


def publish_event_batch(batch_size: int = 100) -> int:
    ensure_analytics_table()
    events_collection = get_db().events
    events = list(events_collection.find({"publishedAt": None}).sort("_id", 1).limit(batch_size))
    if not events:
        return 0
    ids = [event["_id"] for event in events]
    pending_filter = {"_id": {"$in": ids}, "publishedAt": None}
    try:
        rows = "\n".join(json.dumps(event_row(event), ensure_ascii=False, allow_nan=False) for event in events)
        clickhouse_request(f"INSERT INTO analytics_events FORMAT JSONEachRow\n{rows}\n")
        events_collection.update_many(pending_filter, {
            "$set": {"publishedAt": datetime.now(timezone.utc), "lastError": ""},
            "$inc": {"attempts": 1},
        })
        return len(events)
    except Exception as error:
        try:
            events_collection.update_many(pending_filter, {
                "$inc": {"attempts": 1}, "$set": {"lastError": str(error)[:500]},
            })
        except Exception:
            logger.exception("Could not update outbox retry diagnostics")
        raise


def start_event_worker(*, poll_interval: float = 2.0, batch_size: int = 100):
    """Start a daemon thread and return a synchronous graceful-stop function."""
    stopped = threading.Event()

    def run():
        failures = 0
        delay = 0.1
        while not stopped.wait(delay):
            try:
                count = publish_event_batch(batch_size)
                failures = 0
                delay = 0.025 if count == batch_size else poll_interval
            except Exception as error:
                failures += 1
                delay = min(60.0, poll_interval * 2 ** min(failures, 5))
                logger.warning("Analytics delivery failed; retry in %.1fs: %s", delay, error)

    thread = threading.Thread(target=run, name="analytics-outbox", daemon=True)
    thread.start()

    def stop():
        stopped.set()
        thread.join(timeout=35)

    return stop


if __name__ == "__main__":
    import signal
    from .db import init_database, mongo_client

    logging.basicConfig(level=logging.INFO)
    init_database()
    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: shutdown.set())
    signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
    stop_worker = start_event_worker()
    try:
        shutdown.wait()
    finally:
        stop_worker()
        mongo_client.close()
