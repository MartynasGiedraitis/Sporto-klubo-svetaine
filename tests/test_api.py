"""Integration tests against disposable MongoDB database and local Redis.

Run the database services from compose.yaml before invoking pytest.
"""
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true")
os.environ.setdefault("JWT_SECRET", "integration-test-secret-with-more-than-32-characters")
os.environ.setdefault("CLICKHOUSE_URL", "http://localhost:8123")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from redis import Redis
from app import db as database
from app.main import create_app
from app.security import hash_password


@pytest.fixture(scope="module")
def api():
    original_name = database.MONGODB_DATABASE
    test_name = "pulse_test_" + uuid.uuid4().hex
    database.MONGODB_DATABASE = test_name
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    redis.ping()
    try:
        with TestClient(create_app(redis_client=redis, run_worker=False)) as client:
            db = database.get_db()
            password_hash = hash_password("Sportas123!")
            actors = {}
            for role in ("admin", "trainer", "member", "other"):
                user = {"firstName": role, "lastName": "Testas", "birthYear": 1995,
                        "email": f"{role}@test.lt", "passwordHash": password_hash,
                        "role": role if role != "other" else "member", "isActive": True,
                        "bookingVersion": 0, "scheduleVersion": 0}
                user["_id"] = db.users.insert_one(user).inserted_id
                response = client.post("/api/auth/login", json={"email": user["email"], "password": "Sportas123!"})
                assert response.status_code == 200, response.text
                actors[role] = {"user": user, "cookie": response.cookies.get("pulse_session")}
            client.cookies.clear()

            def call(actor, method, path, **kwargs):
                headers = dict(kwargs.pop("headers", {}))
                if actor:
                    headers["Cookie"] = "pulse_session=" + actors[actor]["cookie"]
                return client.request(method, "/api" + path, headers=headers, **kwargs)

            room = call("admin", "POST", "/rooms", json={"name": "Testų salė", "capacity": 20, "location": "Testai"}).json()["item"]
            plan = call("admin", "POST", "/plans", json={"name": "Neribota", "price": 29.9, "durationDays": 365}).json()["item"]
            exercise = call("admin", "POST", "/exercises", json={"name": "Pritūpimai", "muscleGroup": "Kojos", "category": "Jėga"}).json()["item"]
            for role in ("member", "other"):
                assert call(role, "POST", "/memberships", json={"planId": plan["_id"]}).status_code == 201
            yield {"client": client, "call": call, "db": db, "actors": actors, "room": room, "plan": plan, "exercise": exercise}
    finally:
        # Only the database allocated above may be dropped.
        if test_name.startswith("pulse_test_") and database.MONGODB_DATABASE == test_name:
            database.mongo_client.drop_database(test_name)
        database.MONGODB_DATABASE = original_name
        redis.close()


def training(api, day, capacity=10, **overrides):
    start = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=day)
    data = {"title": f"Testinė {day}", "type": "Jėga", "roomId": api["room"]["_id"],
            "trainerId": str(api["actors"]["trainer"]["user"]["_id"]),
            "startsAt": start.isoformat(), "endsAt": (start + timedelta(hours=1)).isoformat(), "capacity": capacity, **overrides}
    response = api["call"]("admin", "POST", "/sessions", json=data)
    assert response.status_code == 201, response.text
    return response.json()["item"]


def test_registration_cannot_choose_privileged_role(api):
    response = api["call"](None, "POST", "/auth/register", json={"firstName": "Naujas", "lastName": "Narys", "birthYear": 2000,
        "email": "registration@test.lt", "password": "Sportas123!", "role": "admin"})
    assert response.status_code in (201, 422), response.text
    if response.status_code == 201:
        assert response.json()["user"]["role"] == "member"
        assert "passwordHash" not in response.json()["user"]
    api["client"].cookies.clear()


def test_roles_and_csrf_are_enforced(api):
    call = api["call"]
    assert call(None, "GET", "/bookings/me").status_code == 401
    assert call("member", "POST", "/rooms", json={"name": "Ne", "capacity": 1}).status_code == 403
    assert call("trainer", "GET", "/analytics/summary").status_code == 403
    assert call("member", "POST", "/memberships", json={"planId": api["plan"]["_id"]}, headers={"Origin": "https://foreign.example"}).status_code == 403


def test_membership_is_unlimited_and_preserves_purchase_snapshot(api):
    membership = api["call"]("member", "GET", "/memberships/me").json()["items"][0]
    assert membership["planNameSnapshot"] == "Neribota"
    assert membership["pricePaid"] == 29.9
    assert "remainingVisits" not in membership
    assert "visitLimit" not in api["plan"]


def test_last_place_cannot_be_double_booked(api):
    item = training(api, 3, capacity=1)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda actor: api["call"](actor, "POST", "/bookings", json={"sessionId": item["_id"]}), ["member", "other"]))
    assert sorted(response.status_code for response in responses) == [201, 409], [r.text for r in responses]
    db = api["db"]
    assert db.bookings.count_documents({"sessionId": ObjectId(item["_id"]), "status": "registered"}) == 1
    assert db.training_sessions.find_one({"_id": ObjectId(item["_id"])})["bookedCount"] == 1


def test_duplicate_registration_cancellation_and_rebooking(api):
    call = api["call"]
    item = training(api, 4)
    first = call("member", "POST", "/bookings", json={"sessionId": item["_id"]})
    assert first.status_code == 201, first.text
    assert call("member", "POST", "/bookings", json={"sessionId": item["_id"]}).status_code == 409
    booking_id = first.json()["item"]["_id"]
    assert call("other", "DELETE", f"/bookings/{booking_id}").status_code in (403, 404)
    assert call("member", "DELETE", f"/bookings/{booking_id}").status_code == 200
    assert call("member", "DELETE", f"/bookings/{booking_id}").status_code == 200
    assert call("member", "POST", "/bookings", json={"sessionId": item["_id"]}).status_code == 201
    assert api["db"].bookings.count_documents({"sessionId": ObjectId(item["_id"])}) == 1
    assert api["db"].training_sessions.find_one({"_id": ObjectId(item["_id"])})["bookedCount"] == 1


def test_parallel_member_overlap_is_rejected(api):
    first = training(api, 5)
    trainer = dict(api["actors"]["trainer"]["user"])
    trainer.pop("_id")
    trainer["email"] = "trainer2@test.lt"
    trainer_id = api["db"].users.insert_one(trainer).inserted_id
    room = api["call"]("admin", "POST", "/rooms", json={"name": "Antra", "capacity": 20}).json()["item"]
    second = training(api, 5, trainerId=str(trainer_id), roomId=room["_id"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda item: api["call"]("member", "POST", "/bookings", json={"sessionId": item["_id"]}), [first, second]))
    assert sorted(response.status_code for response in responses) == [201, 409], [r.text for r in responses]


def test_coach_room_overlap_and_room_capacity(api):
    item = training(api, 6)
    data = {k: item[k] for k in ("title", "type", "startsAt", "endsAt", "capacity")}
    data.update(roomId=api["room"]["_id"], trainerId=str(api["actors"]["trainer"]["user"]["_id"]))
    assert api["call"]("admin", "POST", "/sessions", json=data).status_code == 409
    assert api["call"]("admin", "PATCH", f'/rooms/{api["room"]["_id"]}', json={"capacity": 1}).status_code == 409


def test_expired_or_short_membership_cannot_book(api):
    item = training(api, 8)
    user = dict(api["actors"]["other"]["user"])
    user.pop("_id")
    user["email"] = "without@test.lt"
    user_id = api["db"].users.insert_one(user).inserted_id
    response = api["client"].post("/api/auth/login", json={"email": user["email"], "password": "Sportas123!"})
    cookie = response.cookies.get("pulse_session")
    api["client"].cookies.clear()
    headers = {"Cookie": "pulse_session=" + cookie}
    response = api["client"].post("/api/bookings", json={"sessionId": item["_id"]}, headers=headers)
    assert response.status_code == 409
    api["db"].memberships.insert_one({"userId": user_id, "planId": ObjectId(api["plan"]["_id"]), "status": "active",
        "startsAt": datetime.now(timezone.utc), "expiresAt": datetime.fromisoformat(item["startsAt"].replace("Z", "+00:00")) + timedelta(minutes=30)})
    assert api["client"].post("/api/bookings", json={"sessionId": item["_id"]}, headers=headers).status_code == 409


def test_attendance_then_one_review_and_atomic_event(api):
    call = api["call"]
    item = training(api, 9)
    booking = call("member", "POST", "/bookings", json={"sessionId": item["_id"]}).json()["item"]
    path = f'/bookings/{booking["_id"]}/attendance'
    assert call("trainer", "PATCH", path, json={"status": "attended"}).status_code == 409
    assert call("member", "POST", "/reviews", json={"bookingId": booking["_id"], "rating": 5}).status_code == 403
    api["db"].training_sessions.update_one({"_id": ObjectId(item["_id"])}, {"$set": {
        "startsAt": datetime.now(timezone.utc)-timedelta(hours=2), "endsAt": datetime.now(timezone.utc)-timedelta(hours=1)}})
    assert call("trainer", "PATCH", path, json={"status": "attended"}).status_code == 200
    response = call("member", "POST", "/reviews", json={"bookingId": booking["_id"], "rating": 5, "comment": "Puiku"})
    assert response.status_code == 201, response.text
    assert call("member", "POST", "/reviews", json={"bookingId": booking["_id"], "rating": 4}).status_code == 409
    assert call("trainer", "PATCH", path, json={"status": "no_show"}).status_code == 409
    assert api["db"].events.count_documents({"type": "review_created", "payload.bookingId": booking["_id"]}) == 1


def test_private_logs_and_conversations(api):
    call = api["call"]
    log = call("member", "POST", "/workout-logs", json={"exerciseId": api["exercise"]["_id"],
        "performedAt": datetime.now(timezone.utc).isoformat(), "sets": 3, "repetitions": 10, "weight": 40}).json()["item"]
    assert call("other", "PATCH", f'/workout-logs/{log["_id"]}', json={"notes": "Ne mano"}).status_code == 404
    assert call("other", "DELETE", f'/workout-logs/{log["_id"]}').status_code == 404
    assert call("member", "PATCH", f'/workout-logs/{log["_id"]}', json={"weight": 42}).status_code == 200
    switched = call("member", "PATCH", f'/workout-logs/{log["_id"]}', json={"weight": None, "repetitions": None, "durationSeconds": 300})
    assert switched.status_code == 200, switched.text
    assert not switched.json()["item"].get("weight")
    assert not switched.json()["item"].get("repetitions")
    assert switched.json()["item"]["durationSeconds"] == 300
    chat = call("member", "POST", "/conversations", json={"trainerId": str(api["actors"]["trainer"]["user"]["_id"])}).json()["item"]
    path = f'/conversations/{chat["_id"]}/messages'
    assert call("member", "POST", path, json={"content": "Labas, treneri!"}).status_code == 201
    assert call("trainer", "GET", path).json()["items"][0]["content"] == "Labas, treneri!"
    assert call("other", "GET", path).status_code == 404
    assert call("admin", "GET", path).status_code == 404
    assert call("other", "POST", path, json={"content": "Svetimas"}).status_code == 404


def test_cancelled_session_cancels_bookings(api):
    item = training(api, 10)
    booking = api["call"]("member", "POST", "/bookings", json={"sessionId": item["_id"]}).json()["item"]
    assert api["call"]("trainer", "DELETE", f'/sessions/{item["_id"]}').status_code == 200
    assert api["db"].bookings.find_one({"_id": ObjectId(booking["_id"])})["status"] == "cancelled"
    assert api["db"].training_sessions.find_one({"_id": ObjectId(item["_id"])})["bookedCount"] == 0
    assert api["call"]("other", "POST", "/bookings", json={"sessionId": item["_id"]}).status_code == 409
