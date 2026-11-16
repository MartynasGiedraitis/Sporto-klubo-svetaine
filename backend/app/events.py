"""Transactional analytics outbox and a small ClickHouse HTTP transport."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from .db import get_db

EVENT_TYPES = frozenset({
    "membership_purchased", "booking_created", "booking_cancelled",
    "attendance_marked", "review_created", "session_cancelled", "session_completed",
})


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_event(event_type: str, payload: dict, session: Any) -> dict:
    """Use the exact MongoDB session of the corresponding business mutation."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}")
    if session is None or not session.in_transaction:
        raise RuntimeError("Analytic events require an active MongoDB transaction.")
    now = datetime.now(timezone.utc)
    event = {
        "type": event_type, "payload": payload, "occurredAt": now,
        "publishedAt": None, "attempts": 0, "lastError": "",
        "createdAt": now, "updatedAt": now,
    }
    result = get_db().events.insert_one(event, session=session)
    event["_id"] = result.inserted_id
    return event


def clickhouse_config() -> dict[str, str]:
    database = os.getenv("CLICKHOUSE_DATABASE", "gym")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", database):
        raise ValueError("Invalid CLICKHOUSE_DATABASE.")
    return {
        "url": os.getenv("CLICKHOUSE_URL", "http://clickhouse:8123"),
        "user": os.getenv("CLICKHOUSE_USER", "gym"),
        "password": os.getenv("CLICKHOUSE_PASSWORD", "gym-dev-password"),
        "database": database,
    }


def clickhouse_request(
    query: str, *, parameters: Mapping[str, Any] | None = None,
    json_response: bool = False,
) -> Any:
    """Credentials stay in headers; report inputs are typed ClickHouse parameters."""
    config = clickhouse_config()
    params = {
        "database": config["database"], "wait_end_of_query": "1",
        "date_time_input_format": "best_effort",
        "output_format_json_quote_64bit_integers": "0",
    }
    params.update({f"param_{name}": str(value) for name, value in (parameters or {}).items()})
    response = httpx.post(
        config["url"], params=params, content=query.encode("utf-8"),
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "X-ClickHouse-User": config["user"], "X-ClickHouse-Key": config["password"],
        }, timeout=15.0,
    )
    if not response.is_success:
        raise RuntimeError(f"ClickHouse HTTP {response.status_code}: {response.text[:300]}")
    return response.json() if json_response else response.text


def event_row(event: Mapping[str, Any]) -> dict:
    payload = event.get("payload") or {}
    return {
        "event_id": str(event["_id"]), "event_type": event["type"],
        "occurred_at": iso_utc(event["occurredAt"]),
        "session_id": str(payload.get("sessionId") or ""),
        "booking_id": str(payload.get("bookingId") or ""),
        "trainer_id": str(payload.get("trainerId") or ""),
        "payload": json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False, separators=(",", ":")),
    }
