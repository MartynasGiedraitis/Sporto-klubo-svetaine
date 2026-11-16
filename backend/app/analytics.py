"""Admin reports: ClickHouse aggregates, MongoDB session dimensions, Redis cache."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .db import get_db
from .events import clickhouse_request, iso_utc

logger = logging.getLogger(__name__)
TIMEZONE = "Europe/Vilnius"
CACHE_TTL_SECONDS = 60


class ReportRangeError(ValueError):
    """A valid period contains more rows than this report can safely return."""


def _round(value) -> float:
    return round(float(value or 0), 2)


def _rate(numerator, denominator) -> float:
    return _round(100 * numerator / denominator) if denominator else 0


def report_period(query: Mapping | None = None, now: datetime | None = None) -> dict:
    query = query or {}
    zone = ZoneInfo(TIMEZONE)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    from_text = query.get("from") or (today - timedelta(days=29)).isoformat()
    to_text = query.get("to") or today.isoformat()

    def parse(value, is_to):
        if not isinstance(value, str):
            raise ValueError("Neteisingas laikotarpis.")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
            if is_to:
                day += timedelta(days=1)
            return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError("Naudokite YYYY-MM-DD arba ISO datą su laiko juosta.")
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    start, end = parse(from_text, False), parse(to_text, True)
    if start >= end or end - start > timedelta(days=3660):
        raise ValueError("Laikotarpis turi būti teigiamas ir ne ilgesnis nei 10 metų.")
    return {"from": iso_utc(start), "to": iso_utc(end), "timezone": TIMEZONE}


MEMBERSHIPS_SQL = """
  SELECT
    JSONExtractString(payload, 'planId') AS planId,
    argMax(JSONExtractString(payload, 'planName'), tuple(occurred_at, event_id)) AS planName,
    count() AS count,
    sum(if(JSONExtractString(payload, 'pricePaid') != '',
      toFloat64OrZero(JSONExtractString(payload, 'pricePaid')),
      JSONExtractFloat(payload, 'pricePaid'))) AS revenue
  FROM analytics_events FINAL
  WHERE event_type = 'membership_purchased'
    AND occurred_at >= {from:DateTime64(3, 'UTC')}
    AND occurred_at < {to:DateTime64(3, 'UTC')}
  GROUP BY planId
  ORDER BY count DESC, planName ASC
  FORMAT JSON
"""

SESSIONS_SQL = """
  SELECT session_id AS sessionId,
    sum(registration_count) AS registrations,
    countIf(lifecycle = 'booking_created') AS activeRegistrations,
    countIf(attendance = 'attended') AS attended,
    countIf(attendance = 'no_show') AS noShows,
    sum(member_cancellations) AS cancellations,
    sum(rating) AS ratingSum,
    countIf(rating > 0) AS reviewCount
  FROM (
    SELECT session_id, booking_id,
      countIf(event_type = 'booking_created') AS registration_count,
      argMaxIf(event_type,
        tuple(JSONExtractUInt(payload, 'lifecycleVersion'), occurred_at, event_id),
        event_type IN ('booking_created', 'booking_cancelled')) AS lifecycle,
      argMaxIf(JSONExtractString(payload, 'status'),
        tuple(JSONExtractUInt(payload, 'attendanceVersion'), occurred_at, event_id),
        event_type = 'attendance_marked') AS attendance,
      countIf(event_type = 'booking_cancelled' AND JSONExtractString(payload, 'reason') != 'session_cancelled') AS member_cancellations,
      argMaxIf(JSONExtractFloat(payload, 'rating'), tuple(occurred_at, event_id), event_type = 'review_created') AS rating
    FROM analytics_events FINAL
    WHERE session_id IN {sessionIds:Array(String)} AND booking_id != ''
    GROUP BY session_id, booking_id
  )
  GROUP BY session_id
  FORMAT JSON
"""


def build_summary(period: dict) -> dict:
    db = get_db()
    date_filter = {"startsAt": {
        "$gte": datetime.fromisoformat(period["from"].replace("Z", "+00:00")),
        "$lt": datetime.fromisoformat(period["to"].replace("Z", "+00:00")),
    }}
    sessions = list(db.training_sessions.find({**date_filter, "status": {"$ne": "cancelled"}}).sort("startsAt", 1).limit(10001))
    if len(sessions) > 10000:
        raise ReportRangeError("Per daug treniruočių. Pasirinkite trumpesnį laikotarpį.")
    cancelled_sessions = db.training_sessions.count_documents({**date_filter, "status": "cancelled"})
    session_ids = [str(session["_id"]) for session in sessions]
    if any(not re.fullmatch(r"[a-f0-9]{24}", item, re.IGNORECASE) for item in session_ids):
        raise RuntimeError("Invalid session identifier.")
    trainer_ids = list({session["trainerId"] for session in sessions})
    users = {str(user["_id"]): user for user in db.users.find({"_id": {"$in": trainer_ids}}, {"firstName": 1, "lastName": 1})}
    memberships_result = clickhouse_request(MEMBERSHIPS_SQL, parameters={
        "from": period["from"].replace("T", " ").replace("Z", ""),
        "to": period["to"].replace("T", " ").replace("Z", ""),
    }, json_response=True)
    sessions_result = clickhouse_request(SESSIONS_SQL, parameters={
        "sessionIds": "[" + ",".join(f"'{item}'" for item in session_ids) + "]",
    }, json_response=True)
    pending_events = db.events.count_documents({"publishedAt": None})
    memberships_by_plan = [{**row, "count": int(row["count"]), "revenue": _round(row["revenue"])} for row in memberships_result["data"]]
    stats_by_id = {row["sessionId"]: row for row in sessions_result["data"]}
    trainers_by_id = {}
    all_sessions = []
    for session in sessions:
        session_id, trainer_id = str(session["_id"]), str(session["trainerId"])
        stats = stats_by_id.get(session_id, {})
        user = users.get(trainer_id, {})
        trainer_name = f"{user.get('firstName', '')} {user.get('lastName', '')}".strip() or "Treneris"
        item = {
            "sessionId": session_id, "title": session["title"], "trainerId": trainer_id,
            "trainerName": trainer_name, "startsAt": iso_utc(session["startsAt"]),
            "capacity": int(session["capacity"]),
            **{key: int(stats.get(key) or 0) for key in [
                "registrations", "activeRegistrations", "attended", "noShows", "cancellations", "reviewCount",
            ]},
            "ratingSum": float(stats.get("ratingSum") or 0),
        }
        item["occupancyRate"] = _rate(item["activeRegistrations"], item["capacity"])
        trainer = trainers_by_id.setdefault(trainer_id, {
            "trainerId": trainer_id, "trainerName": trainer_name, "sessions": 0,
            "registrations": 0, "attended": 0, "noShows": 0, "reviewCount": 0, "ratingSum": 0,
        })
        trainer["sessions"] += 1
        for key in ["registrations", "attended", "noShows", "reviewCount", "ratingSum"]:
            trainer[key] += item[key]
        all_sessions.append(item)
    totals = {
        "membershipsSold": sum(row["count"] for row in memberships_by_plan),
        "revenue": _round(sum(row["revenue"] for row in memberships_by_plan)),
        "sessions": len(sessions), "cancelledSessions": cancelled_sessions,
        **{key: sum(row[key] for row in all_sessions) for key in [
            "registrations", "activeRegistrations", "attended", "noShows", "cancellations", "capacity",
        ]},
    }
    totals["occupancyRate"] = _rate(totals["activeRegistrations"], totals["capacity"])
    totals["cancellationRate"] = _rate(totals["cancellations"], totals["registrations"])
    totals["noShowRate"] = _rate(totals["noShows"], totals["attended"] + totals["noShows"])
    trainers = []
    for trainer in trainers_by_id.values():
        rating_sum = trainer.pop("ratingSum")
        trainer["averageRating"] = _round(rating_sum / trainer["reviewCount"]) if trainer["reviewCount"] else None
        trainer["attendanceRate"] = _rate(trainer["attended"], trainer["attended"] + trainer["noShows"])
        trainers.append(trainer)
    return {
        "period": period, "totals": totals, "membershipsByPlan": memberships_by_plan,
        "popularSessions": sorted(all_sessions, key=lambda row: (-row["registrations"], -row["attended"]))[:20],
        "trainers": sorted(trainers, key=lambda row: (-row["attended"], row["trainerName"].casefold())),
        "generatedAt": iso_utc(datetime.now(timezone.utc)), "cached": False,
        "delivery": {"pendingEvents": pending_events, "eventualConsistency": True, "cacheTtlSeconds": CACHE_TTL_SECONDS},
    }


def create_analytics_router(redis_client) -> APIRouter:
    """The application mounts this router with its administrator dependency."""
    router = APIRouter()

    @router.get("/summary")
    def summary(from_date: str | None = Query(default=None, alias="from"), to: str | None = None):
        try:
            period = report_period({"from": from_date, "to": to})
        except (ValueError, OverflowError) as error:
            return JSONResponse(status_code=400, content={"error": str(error)})
        key = f"analytics:summary:v1:{period['from']}:{period['to']}"
        try:
            if redis_client is not None:
                try:
                    cached = redis_client.get(key)
                    if cached:
                        return {**json.loads(cached), "cached": True}
                except Exception:
                    pass  # Redis outages do not hide an authoritative report.
            report = build_summary(period)
            if redis_client is not None:
                try:
                    redis_client.setex(key, CACHE_TTL_SECONDS, json.dumps(report, ensure_ascii=False))
                except Exception:
                    pass
            return report
        except ReportRangeError as error:
            return JSONResponse(status_code=400, content={"error": str(error)})
        except Exception as error:
            logger.warning("Analytics report unavailable: %s", error)
            return JSONResponse(status_code=503, content={"error": "Analitikos paslauga šiuo metu nepasiekiama. Pabandykite vėliau."})

    return router
