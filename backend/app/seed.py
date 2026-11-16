"""Idempotent demonstration data. Run: python -m app.seed."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from pymongo import ReturnDocument
from .db import get_db, init_database, mongo_client, transaction
from .common import now, timestamps
from .security import hash_password
from .events import record_event
from .training import booking_snapshot

def backfill_demo_events(member_id):
    """Repair historical demo fixtures without duplicating business events."""
    db = get_db()
    def work(session):
        # Serialize parallel seed runs using the demo account's existing guard.
        db.users.update_one({"_id": member_id}, {"$inc": {"bookingVersion": 1}}, session=session)
        def missing(event_type, aggregate_key, aggregate_id, payload, occurred_at):
            if db.events.find_one({"type": event_type, f"payload.{aggregate_key}": str(aggregate_id)}, session=session):
                return
            event = record_event(event_type, payload, session)
            db.events.update_one({"_id": event["_id"]}, {"$set": {"occurredAt": occurred_at}}, session=session)
        for membership in db.memberships.find({"userId": member_id}, session=session):
            missing("membership_purchased", "membershipId", membership["_id"], {
                "membershipId": str(membership["_id"]), "userId": str(member_id),
                "planId": str(membership["planId"]), "planName": membership["planNameSnapshot"],
                "pricePaid": membership["pricePaid"],
            }, membership.get("purchasedAt", membership["createdAt"]))
        for booking in db.bookings.find({"userId": member_id}, session=session):
            training = db.training_sessions.find_one({"_id": booking["sessionId"]}, session=session)
            if not training:
                continue
            payload = booking_snapshot(booking, training, session)
            missing("booking_created", "bookingId", booking["_id"], {
                **payload, "lifecycleVersion": 1,
            }, booking["bookedAt"])
            if booking["status"] in ("attended", "no_show"):
                missing("attendance_marked", "bookingId", booking["_id"], {
                    **payload, "status": booking["status"],
                }, booking.get("attendanceMarkedAt", training["endsAt"]))
    transaction(work)

def seed():
    init_database(); db = get_db(); password_hash = hash_password("Sportas123!")
    def ensure(collection, criteria, data):
        return db[collection].find_one_and_update(criteria, {"$setOnInsert": timestamps(data)}, upsert=True, return_document=ReturnDocument.AFTER)
    def account(email, first, last, role, specialization=""):
        return ensure("users", {"email": email}, {"email": email, "firstName": first, "lastName": last, "birthYear": 1995 if role == "trainer" else 2000, "role": role, "passwordHash": password_hash, "isActive": True, "specialization": specialization, "bookingVersion": 0, "scheduleVersion": 0})
    member = account("member@pulse.lt", "Mantas", "Kazlauskas", "member")
    coach = account("trainer@pulse.lt", "Lukas", "Petrauskas", "trainer", "Funkcinės treniruotės ir jėgos ugdymas")
    coach2 = account("ieva@pulse.lt", "Ieva", "Jankauskaitė", "trainer", "Joga, mobilumas ir pilatesas")
    account("admin@pulse.lt", "Austėja", "Administratorė", "admin")
    strength = ensure("rooms", {"name": "Jėgos studija"}, {"name": "Jėgos studija", "capacity": 16, "location": "1 aukštas", "isActive": True, "scheduleVersion": 0})
    yoga = ensure("rooms", {"name": "Ramybės studija"}, {"name": "Ramybės studija", "capacity": 20, "location": "2 aukštas", "isActive": True, "scheduleVersion": 0})
    plan = ensure("membership_plans", {"name": "Mėnesio ritmas"}, {"name": "Mėnesio ritmas", "durationDays": 30, "price": 39, "description": "30 dienų neribotų apsilankymų ir grupinių treniruočių.", "isActive": True, "purchaseVersion": 0})
    ensure("membership_plans", {"name": "Trys mėnesiai"}, {"name": "Trys mėnesiai", "durationDays": 90, "price": 99, "description": "90 dienų tavo tikslams. Visi apsilankymai neriboti.", "isActive": True, "purchaseVersion": 0})
    ensure("membership_plans", {"name": "Metų progresas"}, {"name": "Metų progresas", "durationDays": 365, "price": 349, "description": "Visi metai judesio ir neribotų apsilankymų.", "isActive": True, "purchaseVersion": 0})
    squat = ensure("exercises", {"name": "Pritūpimai su štanga"}, {"name": "Pritūpimai su štanga", "muscleGroup": "Kojos", "category": "Jėga", "description": "Kontroliuojamas pritūpimas išlaikant neutralią nugaros padėtį.", "isActive": True})
    for name, group, category in [("Bėgimas takeliu", "Visas kūnas", "Ištvermė"), ("Štangos spaudimas", "Krūtinė", "Jėga"), ("Lenta", "Korpusas", "Stabilumas")]:
        ensure("exercises", {"name": name}, {"name": name, "muscleGroup": group, "category": category, "description": "", "isActive": True})
    if not db.memberships.find_one({"userId": member["_id"]}):
        db.memberships.insert_one(timestamps({"userId": member["_id"], "planId": plan["_id"], "startsAt": now()-timedelta(days=7), "expiresAt": now()+timedelta(days=23), "purchasedAt": now()-timedelta(days=7), "pricePaid": plan["price"], "planNameSnapshot": plan["name"], "status": "active"}))
    membership = db.memberships.find_one({"userId": member["_id"]}, sort=[("expiresAt", -1)])
    if not db.training_sessions.find_one({}):
        local = now().astimezone(ZoneInfo("Europe/Vilnius"))
        def time_at(offset, hour):
            return (local + timedelta(days=offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        templates = [("Funkcinė jėga", "Funkcinė", coach, strength, 18, "Viso kūno treniruotė stipresnei kasdienybei."), ("Rytinė joga", "Joga", coach2, yoga, 9, "Kvėpavimas, judesys ir lengva dienos pradžia."), ("HIIT energija", "HIIT", coach, strength, 19, "Intensyvūs intervalai, ištvermė ir gera nuotaika."), ("Pilateso pagrindai", "Pilatesas", coach2, yoga, 17, "Dėmesys laikysenai, stabilumui ir judesio kokybei.")]
        for day in range(1, 8):
            title, kind, trainer, room, hour, description = templates[(day-1) % len(templates)]
            starts = time_at(day, hour)
            item = timestamps({"title": title, "type": kind, "trainerId": trainer["_id"], "roomId": room["_id"], "startsAt": starts, "endsAt": starts+timedelta(hours=1), "capacity": room["capacity"], "description": description, "status": "scheduled", "bookedCount": 1 if day <= 2 else 0, "scheduleVersion": 0})
            db.training_sessions.insert_one(item)
            if day <= 2:
                db.bookings.insert_one(timestamps({"userId": member["_id"], "sessionId": item["_id"], "membershipId": membership["_id"], "status": "registered", "bookedAt": now(), "lifecycleVersion": 1, "attendanceVersion": 0}))
        starts = time_at(-1, 18)
        past = timestamps({"title": "Funkcinė jėga", "type": "Funkcinė", "trainerId": coach["_id"], "roomId": strength["_id"], "startsAt": starts, "endsAt": starts+timedelta(hours=1), "capacity": strength["capacity"], "description": "Viso kūno jėga ir judesio technika.", "status": "completed", "bookedCount": 1, "scheduleVersion": 0})
        db.training_sessions.insert_one(past)
        db.bookings.insert_one(timestamps({"userId": member["_id"], "sessionId": past["_id"], "membershipId": membership["_id"], "status": "attended", "bookedAt": now()-timedelta(days=2), "attendanceMarkedAt": now(), "attendanceMarkedBy": coach["_id"], "attendanceVersion": 1, "lifecycleVersion": 1}))
    if not db.workout_logs.find_one({"userId": member["_id"]}):
        for day in (14, 7, 0):
            db.workout_logs.insert_one(timestamps({"userId": member["_id"], "exerciseId": squat["_id"], "performedAt": now()-timedelta(days=day), "sets": 3, "repetitions": 10, "weight": 54-day, "notes": "Judesys užtikrintesnis." if day == 0 else "Dėmesys technikai."}))
    chat = ensure("conversations", {"memberId": member["_id"], "trainerId": coach["_id"]}, {"memberId": member["_id"], "trainerId": coach["_id"]})
    if not db.messages.find_one({"conversationId": chat["_id"]}):
        db.messages.insert_one(timestamps({"conversationId": chat["_id"], "senderId": coach["_id"], "content": "Labas, Mantai! Jei turėsi klausimų apie treniruotes ar pratimus, parašyk čia.", "sentAt": now(), "readAt": None}))
    backfill_demo_events(member["_id"])
    print("Demo duomenys paruošti. member@pulse.lt / trainer@pulse.lt / admin@pulse.lt — Sportas123!")

if __name__ == "__main__":
    try: seed()
    finally: mongo_client.close()
