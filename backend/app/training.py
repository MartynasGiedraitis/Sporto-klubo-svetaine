import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Body, Request, Query
from pymongo import ReturnDocument
from .db import get_db, transaction
from .security import current_user, optional_user, require_coach, require_member
from .schemas import SessionInput, BookingInput, AttendanceInput, ReviewInput
from .common import now, oid, fail, timestamps, response, json_value, populate_session, populate_user
from .events import record_event

ACTIVE = ["registered", "attended", "no_show"]

def own(user, training):
    if user["role"] != "admin" and training["trainerId"] != user["_id"]:
        fail(403, "Galite valdyti tik savo treniruotes.")

def snapshot(training, session):
    coach = get_db().users.find_one({"_id": training["trainerId"]}, session=session) or {}
    return {"sessionId": str(training["_id"]), "trainerId": str(training["trainerId"]), "trainerName": f"{coach.get('firstName', '')} {coach.get('lastName', '')}".strip(), "sessionTitle": training["title"], "sessionStartsAt": training["startsAt"].isoformat(), "sessionEndsAt": training["endsAt"].isoformat(), "capacity": training["capacity"]}

def booking_snapshot(booking, training, session):
    return {**snapshot(training, session), "bookingId": str(booking["_id"]), "userId": str(booking["userId"]), "lifecycleVersion": booking.get("lifecycleVersion", 1), "attendanceVersion": booking.get("attendanceVersion", 0)}

def guard_member(user_id, session):
    result = get_db().users.update_one({"_id": user_id, "isActive": True}, {"$inc": {"bookingVersion": 1}}, session=session)
    if not result.matched_count: fail(409, "Vartotojo paskyra neaktyvi.")

def guard_resources(trainer_id, room_id, session, active=True):
    db = get_db(); criteria = {"isActive": True} if active else {}
    coach = db.users.find_one_and_update({"_id": trainer_id, "role": "trainer", **criteria}, {"$inc": {"scheduleVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
    room = db.rooms.find_one_and_update({"_id": room_id, **criteria}, {"$inc": {"scheduleVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
    if not coach or not room: fail(400, "Pasirinkite aktyvų trenerį ir salę.")
    return room

def member_overlap(user_id, start, end, excluded, session):
    db = get_db()
    ids = [b["sessionId"] for b in db.bookings.find({"userId": user_id, "status": {"$in": ACTIVE}, "sessionId": {"$ne": excluded}}, {"sessionId": 1}, session=session)]
    return db.training_sessions.find_one({"_id": {"$in": ids}, "status": {"$ne": "cancelled"}, "startsAt": {"$lt": end}, "endsAt": {"$gt": start}}, session=session)

def validate_schedule(data, excluded, session):
    if data["startsAt"] <= now(): fail(400, "Treniruotė turi prasidėti ateityje.")
    if data["endsAt"] <= data["startsAt"]: fail(400, "Pabaiga turi būti vėlesnė už pradžią.")
    room = guard_resources(data["trainerId"], data["roomId"], session)
    if data["capacity"] > room["capacity"]: fail(400, "Dalyvių limitas viršija salės talpą.")
    criteria = {"status": {"$ne": "cancelled"}, "startsAt": {"$lt": data["endsAt"]}, "endsAt": {"$gt": data["startsAt"]}, "$or": [{"trainerId": data["trainerId"]}, {"roomId": data["roomId"]}]}
    if excluded: criteria["_id"] = {"$ne": excluded}
    if get_db().training_sessions.find_one(criteria, session=session): fail(409, "Treneris arba salė tuo metu užimti.")

def create_training_router(redis_client):
    router = APIRouter(tags=["Treniruotės ir rezervacijos"])

    def invalidate():
        try: redis_client.incr(f"schedule:{get_db().name}:version")
        except Exception: pass

    @router.get("/sessions")
    def sessions(request: Request, from_date: str | None = Query(default=None, alias="from", description="Pradžios data arba ISO laikas (įskaitytinai)."), to_date: str | None = Query(default=None, alias="to", description="Pabaigos data arba ISO laikas (neįskaitytinai)."), trainerId: str | None = None, type: str | None = None, date: str | None = Query(default=None, description="Viena diena YYYY-MM-DD.")):
        query = request.query_params; criteria = {"status": {"$ne": "cancelled"}}
        def parse_date(value):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
            except ValueError: fail(400, "Neteisinga data.")
        if query.get("from") or query.get("date"):
            start = parse_date(query.get("from") or query["date"])
            criteria["startsAt"] = {"$gte": start}
            if query.get("date") and not query.get("to"): criteria["startsAt"]["$lt"] = start + timedelta(days=1)
        if query.get("to"): criteria.setdefault("startsAt", {})["$lt"] = parse_date(query["to"])
        if query.get("trainerId"): criteria["trainerId"] = oid(query["trainerId"])
        if query.get("type"): criteria["type"] = query["type"][:80]
        key = None
        try:
            namespace = f"schedule:{get_db().name}:"
            version = redis_client.get(namespace + "version") or b"0"
            key = namespace + str(version) + ":" + json.dumps(json_value(criteria), sort_keys=True)
            cached = redis_client.get(key)
            if cached: return response({"items": json.loads(cached)})
        except Exception: pass
        items = [populate_session(item) for item in get_db().training_sessions.find(criteria).sort("startsAt", 1).limit(200)]
        if key:
            try: redis_client.setex(key, 30, json.dumps(json_value(items)))
            except Exception: pass
        return response({"items": items})

    @router.get("/sessions/{item_id}")
    def training(item_id: str):
        item = get_db().training_sessions.find_one({"_id": oid(item_id)})
        if not item: fail(404, "Treniruotė nerasta.")
        return response({"item": populate_session(item)})

    @router.post("/sessions", status_code=201)
    def create_session(body: SessionInput, user=Depends(require_coach)):
        data = body.model_dump()
        data["trainerId"] = user["_id"] if user["role"] == "trainer" else oid(data.get("trainerId"))
        data["roomId"] = oid(data["roomId"])
        def work(session):
            validate_schedule(data, None, session)
            item = timestamps({**data, "status": "scheduled", "bookedCount": 0, "scheduleVersion": 0})
            get_db().training_sessions.insert_one(item, session=session); return item
        item = transaction(work); invalidate(); return response({"item": populate_session(item)}, 201)

    @router.patch("/sessions/{item_id}")
    def edit_session(item_id: str, body: dict = Body(...), user=Depends(require_coach)):
        db = get_db()
        def work(session):
            old = db.training_sessions.find_one_and_update({"_id": oid(item_id)}, {"$inc": {"scheduleVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
            if not old: fail(404, "Treniruotė nerasta.")
            own(user, old)
            if old["status"] != "scheduled" or old["startsAt"] <= now(): fail(409, "Prasidėjusios arba atšauktos treniruotės keisti negalima.")
            merged = {**old, **body, "trainerId": str(old["trainerId"]) if "trainerId" not in body else body["trainerId"], "roomId": str(old["roomId"]) if "roomId" not in body else body["roomId"]}
            data = SessionInput.model_validate(merged).model_dump()
            data["trainerId"] = oid(data["trainerId"]); data["roomId"] = oid(data["roomId"])
            if user["role"] == "trainer" and data["trainerId"] != user["_id"]: fail(403, "Negalite perduoti treniruotės kitam treneriui.")
            guard_resources(old["trainerId"], old["roomId"], session, False)
            validate_schedule(data, old["_id"], session)
            if data["capacity"] < old["bookedCount"]: fail(409, "Limitas mažesnis už registracijų skaičių.")
            for booking in db.bookings.find({"sessionId": old["_id"], "status": "registered"}, session=session).sort("userId", 1):
                guard_member(booking["userId"], session)
                if member_overlap(booking["userId"], data["startsAt"], data["endsAt"], old["_id"], session): fail(409, "Naujas laikas persidengia su dalyvio kita treniruote.")
                if not db.memberships.find_one({"_id": booking["membershipId"], "status": "active", "startsAt": {"$lte": data["startsAt"]}, "expiresAt": {"$gte": data["endsAt"]}}, session=session): fail(409, "Dalyvio narystė negalioja nauju treniruotės laiku.")
            data["updatedAt"] = now()
            return db.training_sessions.find_one_and_update({"_id": old["_id"]}, {"$set": data}, session=session, return_document=ReturnDocument.AFTER)
        item = transaction(work); invalidate(); return response({"item": populate_session(item)})

    @router.delete("/sessions/{item_id}")
    def cancel_session(item_id: str, user=Depends(require_coach)):
        db = get_db()
        def work(session):
            item = db.training_sessions.find_one_and_update({"_id": oid(item_id)}, {"$inc": {"scheduleVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
            if not item: fail(404, "Treniruotė nerasta.")
            own(user, item)
            if item["status"] == "cancelled": return item
            if item["startsAt"] <= now(): fail(409, "Prasidėjusios treniruotės atšaukti negalima.")
            guard_resources(item["trainerId"], item["roomId"], session, False)
            for booking in db.bookings.find({"sessionId": item["_id"], "status": "registered"}, session=session):
                booking.update({"status": "cancelled", "cancelledAt": now(), "cancellationReason": "session_cancelled", "lifecycleVersion": booking.get("lifecycleVersion", 1)+1})
                db.bookings.replace_one({"_id": booking["_id"]}, booking, session=session)
                record_event("booking_cancelled", {**booking_snapshot(booking, item, session), "reason": "session_cancelled"}, session)
            item.update({"status": "cancelled", "cancelledAt": now(), "bookedCount": 0, "updatedAt": now()})
            db.training_sessions.replace_one({"_id": item["_id"]}, item, session=session)
            record_event("session_cancelled", snapshot(item, session), session); return item
        item = transaction(work); invalidate(); return response({"item": item})

    @router.get("/sessions/{item_id}/bookings")
    def session_bookings(item_id: str, user=Depends(require_coach)):
        db = get_db(); training = db.training_sessions.find_one({"_id": oid(item_id)})
        if not training: fail(404, "Treniruotė nerasta.")
        own(user, training); items = list(db.bookings.find({"sessionId": training["_id"]}).sort("bookedAt", 1))
        for item in items: item["userId"] = db.users.find_one({"_id": item["userId"]}, {"firstName": 1, "lastName": 1, "email": 1})
        return response({"items": items})

    @router.get("/bookings/me")
    def bookings(user=Depends(current_user)):
        db = get_db(); items = list(db.bookings.find({"userId": user["_id"]}).sort("bookedAt", -1).limit(500))
        for item in items:
            item["sessionId"] = populate_session(db.training_sessions.find_one({"_id": item["sessionId"]}))
            item["review"] = db.reviews.find_one({"bookingId": item["_id"]})
        return response({"items": items})

    @router.post("/bookings", status_code=201)
    def book(body: BookingInput, user=Depends(require_member)):
        db = get_db()
        def work(session):
            guard_member(user["_id"], session)
            training = db.training_sessions.find_one_and_update({"_id": oid(body.sessionId), "status": "scheduled", "startsAt": {"$gt": now()}, "$expr": {"$lt": ["$bookedCount", "$capacity"]}}, {"$inc": {"bookedCount": 1}}, session=session, return_document=ReturnDocument.AFTER)
            if not training: fail(409, "Treniruotė neprieinama arba laisvų vietų nebėra.")
            existing = db.bookings.find_one({"userId": user["_id"], "sessionId": training["_id"]}, session=session)
            if existing and existing["status"] != "cancelled": fail(409, "Jau esate užsiregistravę į šią treniruotę.")
            membership = db.memberships.find_one({"userId": user["_id"], "status": "active", "startsAt": {"$lte": training["startsAt"]}, "expiresAt": {"$gte": training["endsAt"]}}, session=session, sort=[("expiresAt", 1)])
            if not membership: fail(409, "Reikalinga visą treniruotę galiojanti narystė.")
            if member_overlap(user["_id"], training["startsAt"], training["endsAt"], training["_id"], session): fail(409, "Tuo metu jau esate užsiregistravę į kitą treniruotę.")
            booking = timestamps({"userId": user["_id"], "sessionId": training["_id"], "membershipId": membership["_id"], "status": "registered", "bookedAt": now(), "lifecycleVersion": 1, "attendanceVersion": 0})
            if existing:
                booking["_id"] = existing["_id"]; booking["createdAt"] = existing["createdAt"]; booking["lifecycleVersion"] = existing.get("lifecycleVersion", 1)+1
                db.bookings.replace_one({"_id": existing["_id"]}, booking, session=session)
            else: db.bookings.insert_one(booking, session=session)
            record_event("booking_created", booking_snapshot(booking, training, session), session); return booking
        item = transaction(work); invalidate(); return response({"item": item}, 201)

    @router.delete("/bookings/{item_id}")
    def cancel_booking(item_id: str, user=Depends(current_user)):
        db = get_db()
        def work(session):
            guard_member(user["_id"], session)
            booking = db.bookings.find_one({"_id": oid(item_id), "userId": user["_id"]}, session=session)
            if not booking: fail(404, "Registracija nerasta.")
            if booking["status"] == "cancelled": return booking
            if booking["status"] != "registered": fail(409, "Šios registracijos atšaukti negalima.")
            training = db.training_sessions.find_one_and_update({"_id": booking["sessionId"], "status": "scheduled", "startsAt": {"$gt": now()}, "bookedCount": {"$gt": 0}}, {"$inc": {"bookedCount": -1}}, session=session, return_document=ReturnDocument.AFTER)
            if not training: fail(409, "Registraciją atšaukti galima tik iki treniruotės pradžios.")
            booking.update({"status": "cancelled", "cancelledAt": now(), "cancellationReason": "member", "updatedAt": now(), "lifecycleVersion": booking.get("lifecycleVersion", 1)+1})
            db.bookings.replace_one({"_id": booking["_id"]}, booking, session=session)
            record_event("booking_cancelled", {**booking_snapshot(booking, training, session), "reason": "member"}, session); return booking
        item = transaction(work); invalidate(); return response({"item": item})

    @router.patch("/bookings/{item_id}/attendance")
    def attendance(item_id: str, body: AttendanceInput, user=Depends(require_coach)):
        db = get_db()
        def work(session):
            booking = db.bookings.find_one_and_update({"_id": oid(item_id)}, {"$inc": {"reviewVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
            if not booking: fail(404, "Registracija nerasta.")
            training = db.training_sessions.find_one({"_id": booking["sessionId"]}, session=session)
            if not training: fail(404, "Treniruotė nerasta.")
            own(user, training)
            if training["endsAt"] > now(): fail(409, "Lankomumą galima pažymėti tik po treniruotės.")
            if training["status"] == "cancelled" or booking["status"] == "cancelled": fail(409, "Atšauktos registracijos lankomumas nežymimas.")
            if booking["status"] == body.status: return booking
            if body.status == "no_show" and db.reviews.find_one({"bookingId": booking["_id"]}, session=session): fail(409, "Dalyvis jau paliko atsiliepimą; lankomumo pakeisti negalima.")
            booking.update({"status": body.status, "attendanceMarkedAt": now(), "attendanceMarkedBy": user["_id"], "attendanceVersion": booking.get("attendanceVersion", 0)+1, "updatedAt": now()})
            db.bookings.replace_one({"_id": booking["_id"]}, booking, session=session)
            record_event("attendance_marked", {**booking_snapshot(booking, training, session), "status": body.status}, session); return booking
        return response({"item": transaction(work)})

    @router.post("/reviews", status_code=201)
    def review(body: ReviewInput, user=Depends(current_user)):
        db = get_db()
        def work(session):
            booking = db.bookings.find_one_and_update({"_id": oid(body.bookingId), "userId": user["_id"], "status": "attended"}, {"$inc": {"reviewVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
            if not booking: fail(403, "Atsiliepimą gali palikti tik treniruotėje dalyvavęs narys.")
            training = db.training_sessions.find_one({"_id": booking["sessionId"]}, session=session)
            if not training or training["endsAt"] > now(): fail(409, "Treniruotė dar nepasibaigė.")
            item = timestamps({"bookingId": booking["_id"], "rating": body.rating, "comment": body.comment})
            db.reviews.insert_one(item, session=session)
            record_event("review_created", {**booking_snapshot(booking, training, session), "reviewId": str(item["_id"]), "rating": body.rating}, session); return item
        return response({"item": transaction(work)}, 201)
    return router
