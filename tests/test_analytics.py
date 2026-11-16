"""Real MongoDB + ClickHouse checks. Run after docker compose up -d databases."""

from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.analytics import build_summary, create_analytics_router, report_period
from app.events import clickhouse_request, event_row, record_event
from app.worker import ensure_analytics_table, publish_event_batch


class AnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_name = f"pulse_analytics_test_{uuid.uuid4().hex[:12]}"
        cls.environment = patch.dict(os.environ, {
            "CLICKHOUSE_URL": os.getenv("CLICKHOUSE_URL", "http://localhost:8123"),
            "CLICKHOUSE_DATABASE": cls.database_name,
        })
        cls.environment.start()
        cls.addClassCleanup(cls.environment.stop)
        cls.mongo = MongoClient(
            os.getenv("TEST_MONGODB_URI", os.getenv("MONGODB_URI", "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true")),
            tz_aware=True, serverSelectionTimeoutMS=10000,
        )
        cls.addClassCleanup(cls.mongo.close)
        cls.db = cls.mongo[cls.database_name]
        cls.addClassCleanup(cls.mongo.drop_database, cls.database_name)
        for module in ["app.analytics", "app.events", "app.worker"]:
            mocked = patch(f"{module}.get_db", return_value=cls.db)
            mocked.start()
            cls.addClassCleanup(mocked.stop)
        cls._server_query(f"CREATE DATABASE {cls.database_name}")
        cls.addClassCleanup(cls._server_query, f"DROP DATABASE IF EXISTS {cls.database_name}")
        ensure_analytics_table()
        trainer_id, session_id, cancelled_id = ObjectId(), ObjectId(), ObjectId()
        cls.db.users.insert_one({"_id": trainer_id, "firstName": "Test", "lastName": "Trainer"})
        cls.db.training_sessions.insert_many([
            {"_id": session_id, "trainerId": trainer_id, "title": "Analitikos patikra", "capacity": 10, "startsAt": datetime(2026, 1, 20, 10, tzinfo=timezone.utc), "status": "scheduled"},
            {"_id": cancelled_id, "trainerId": trainer_id, "title": "Atšaukta treniruotė", "capacity": 20, "startsAt": datetime(2026, 1, 21, 10, tzinfo=timezone.utc), "status": "cancelled"},
        ])
        base = {"sessionId": str(session_id), "trainerId": str(trainer_id), "capacity": 10}
        booking1, booking2, booking3, plan = (str(ObjectId()) for _ in range(4))
        event_specs = [
            ("membership_purchased", {"planId": plan, "planName": "Neribota", "pricePaid": 30}),
            ("membership_purchased", {"planId": plan, "planName": "Neribota", "pricePaid": 30}),
            ("booking_created", {**base, "bookingId": booking1, "lifecycleVersion": 1}),
            ("booking_cancelled", {**base, "bookingId": booking1, "lifecycleVersion": 2, "reason": "member"}),
            ("booking_created", {**base, "bookingId": booking1, "lifecycleVersion": 3}),
            ("attendance_marked", {**base, "bookingId": booking1, "attendanceVersion": 1, "status": "attended"}),
            ("attendance_marked", {**base, "bookingId": booking1, "attendanceVersion": 2, "status": "no_show"}),
            ("attendance_marked", {**base, "bookingId": booking1, "attendanceVersion": 3, "status": "attended"}),
            ("review_created", {**base, "bookingId": booking1, "rating": 5}),
            ("booking_created", {**base, "bookingId": booking2, "lifecycleVersion": 1}),
            ("booking_cancelled", {**base, "bookingId": booking2, "lifecycleVersion": 2, "reason": "member"}),
            ("booking_created", {**base, "bookingId": booking3, "lifecycleVersion": 1}),
            ("attendance_marked", {**base, "bookingId": booking3, "attendanceVersion": 1, "status": "no_show"}),
            ("booking_created", {**base, "sessionId": str(cancelled_id), "bookingId": str(ObjectId()), "lifecycleVersion": 1}),
        ]
        start = datetime(2026, 1, 10, tzinfo=timezone.utc)
        cls.fixtures = [{"_id": ObjectId(), "type": kind, "payload": payload, "occurredAt": start + timedelta(milliseconds=index), "publishedAt": None, "attempts": 0} for index, (kind, payload) in enumerate(event_specs)]
        cls.db.events.insert_many(cls.fixtures)
        delivered = publish_event_batch()
        assert delivered == len(cls.fixtures), f"Expected {len(cls.fixtures)} events, delivered {delivered}"
        # Simulate duplicate delivery after a process dies between CH and Mongo acknowledgements.
        rows = "\n".join(json.dumps(event_row(item)) for item in cls.fixtures)
        clickhouse_request(f"INSERT INTO analytics_events FORMAT JSONEachRow\n{rows}\n")
        cls.period = report_period({"from": "2026-01-01", "to": "2026-01-31"})

    @classmethod
    def _server_query(cls, sql):
        response = httpx.post(os.environ["CLICKHOUSE_URL"], content=sql, timeout=10, headers={
            "X-ClickHouse-User": os.getenv("CLICKHOUSE_USER", "gym"),
            "X-ClickHouse-Key": os.getenv("CLICKHOUSE_PASSWORD", "gym-dev-password"),
        })
        response.raise_for_status()

    def test_lithuanian_calendar_dates_and_dst(self):
        spring = report_period({"from": "2026-03-29", "to": "2026-03-29"})
        self.assertEqual(spring["from"], "2026-03-28T22:00:00.000Z")
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.assertEqual(parse(spring["to"]) - parse(spring["from"]), timedelta(hours=23))
        autumn = report_period({"from": "2026-10-25", "to": "2026-10-25"})
        self.assertEqual(parse(autumn["to"]) - parse(autumn["from"]), timedelta(hours=25))
        with self.assertRaises(ValueError):
            report_period({"from": "2026-02-30", "to": "2026-03-01"})
        with self.assertRaises(ValueError):
            report_period({"from": "2026-02-01", "to": "2026-01-01"})

    def test_outbox_participates_in_the_business_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "transaction"):
            record_event("membership_purchased", {}, None)
        with self.mongo.start_session() as session:
            def rollback(session):
                record_event("membership_purchased", {"rollbackTest": True}, session)
                raise RuntimeError("intentional rollback")
            with self.assertRaisesRegex(RuntimeError, "intentional rollback"):
                session.with_transaction(rollback)
        self.assertEqual(self.db.events.count_documents({"payload.rollbackTest": True}), 0)

    def test_duplicate_deliveries_rebooking_and_attendance_corrections(self):
        self.assertEqual(self.db.events.count_documents({"publishedAt": None}), 0)
        self.assertEqual(publish_event_batch(), 0)
        report = build_summary(self.period)
        expected = {
            "membershipsSold": 2, "revenue": 60, "sessions": 1, "cancelledSessions": 1,
            "registrations": 4, "activeRegistrations": 2, "cancellations": 2,
            "attended": 1, "noShows": 1, "occupancyRate": 20, "cancellationRate": 50, "noShowRate": 50,
        }
        for key, value in expected.items():
            self.assertEqual(report["totals"][key], value, key)
        self.assertEqual(report["trainers"][0]["averageRating"], 5)
        self.assertEqual(report["trainers"][0]["reviewCount"], 1)

    def test_cache_date_validation_and_clickhouse_outage(self):
        class Cache:
            def __init__(self):
                self.values = {}
            def get(self, key):
                return self.values.get(key)
            def setex(self, key, ttl, value):
                self.values[key] = value

        app = FastAPI()
        app.include_router(create_analytics_router(Cache()), prefix="/analytics")
        with TestClient(app) as client:
            path = "/analytics/summary?from=2026-01-01&to=2026-01-31"
            first = client.get(path)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertFalse(first.json()["cached"])
            second = client.get(path)
            self.assertTrue(second.json()["cached"])
            self.assertEqual(first.json()["generatedAt"], second.json()["generatedAt"])
            self.assertEqual(client.get("/analytics/summary?from=invalid").status_code, 400)
            with patch.dict(os.environ, {"CLICKHOUSE_URL": "http://127.0.0.1:1"}):
                self.assertEqual(client.get(path).status_code, 200)
                self.assertEqual(client.get("/analytics/summary?from=2026-02-01&to=2026-02-28").status_code, 503)


if __name__ == "__main__":
    unittest.main()
