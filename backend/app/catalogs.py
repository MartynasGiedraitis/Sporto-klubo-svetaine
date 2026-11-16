from datetime import timedelta
from fastapi import APIRouter, Depends, Body
from pymongo import ReturnDocument
from .db import get_db, transaction
from .security import current_user, optional_user, require_admin, hash_password
from .schemas import Plan, RoomInput, ExerciseInput, TrainerInput, TrainerPatch, MembershipPurchase
from .common import response, now, oid, fail, timestamps
from .events import record_event

def create_catalog_router(redis_client):
    router = APIRouter(tags=["Narystės ir katalogai"])

    def invalidate():
        try:
            redis_client.incr(f"schedule:{get_db().name}:version")
        except Exception:
            pass

    @router.get("/plans")
    def plans(all: bool = False, user=Depends(optional_user)):
        criteria = {} if all and user and user["role"] == "admin" else {"isActive": True}
        return response({"items": list(get_db().membership_plans.find(criteria).sort("price", 1))})

    @router.post("/plans", status_code=201)
    def create_plan(body: Plan, user=Depends(require_admin)):
        item = timestamps(body.model_dump()); item["purchaseVersion"] = 0
        get_db().membership_plans.insert_one(item)
        return response({"item": item}, 201)

    @router.patch("/plans/{item_id}")
    def edit_plan(item_id: str, body: dict = Body(...), user=Depends(require_admin)):
        old = get_db().membership_plans.find_one({"_id": oid(item_id)})
        if not old: fail(404, "Narystės planas nerastas.")
        data = Plan.model_validate({**old, **body}).model_dump(); data["updatedAt"] = now()
        item = get_db().membership_plans.find_one_and_update({"_id": old["_id"]}, {"$set": data}, return_document=ReturnDocument.AFTER)
        return response({"item": item})

    @router.get("/memberships/me")
    def memberships(user=Depends(current_user)):
        items = list(get_db().memberships.find({"userId": user["_id"]}).sort("expiresAt", -1))
        for item in items:
            item["planId"] = get_db().membership_plans.find_one({"_id": item["planId"]})
            if item["status"] == "active" and item["expiresAt"] <= now(): item["status"] = "expired"
        return response({"items": items})

    @router.post("/memberships", status_code=201)
    def purchase(body: MembershipPurchase, user=Depends(current_user)):
        db = get_db()
        def work(session):
            plan = db.membership_plans.find_one_and_update({"_id": oid(body.planId), "isActive": True}, {"$inc": {"purchaseVersion": 1}}, session=session, return_document=ReturnDocument.AFTER)
            if not plan: fail(404, "Aktyvus narystės planas nerastas.")
            stamp = now()
            item = timestamps({"userId": user["_id"], "planId": plan["_id"], "startsAt": stamp, "expiresAt": stamp + timedelta(days=plan["durationDays"]), "purchasedAt": stamp, "pricePaid": plan["price"], "planNameSnapshot": plan["name"], "status": "active"})
            db.memberships.insert_one(item, session=session)
            record_event("membership_purchased", {"membershipId": str(item["_id"]), "userId": str(user["_id"]), "planId": str(plan["_id"]), "planName": plan["name"], "pricePaid": plan["price"]}, session)
            return item
        return response({"item": transaction(work)}, 201)

    @router.get("/trainers")
    def trainers(all: bool = False, user=Depends(optional_user)):
        is_admin = user and user["role"] == "admin"
        fields = {"firstName": 1, "lastName": 1, "specialization": 1, "role": 1, "isActive": 1}
        if is_admin: fields.update({"email": 1, "birthYear": 1})
        return response({"items": list(get_db().users.find({"role": "trainer", **({} if all and is_admin else {"isActive": True})}, fields).sort("firstName", 1))})

    @router.post("/trainers", status_code=201)
    def create_trainer(body: TrainerInput, user=Depends(require_admin)):
        data = body.model_dump(); password = data.pop("password"); data["email"] = str(data["email"]).lower()
        item = timestamps({**data, "passwordHash": hash_password(password), "role": "trainer", "bookingVersion": 0, "scheduleVersion": 0})
        get_db().users.insert_one(item)
        return response({"item": item}, 201)

    @router.patch("/trainers/{item_id}")
    def edit_trainer(item_id: str, body: TrainerPatch, user=Depends(require_admin)):
        data = body.model_dump(exclude_none=True); data["updatedAt"] = now()
        if "email" in data: data["email"] = str(data["email"]).lower()
        item = get_db().users.find_one_and_update({"_id": oid(item_id), "role": "trainer"}, {"$set": data}, return_document=ReturnDocument.AFTER)
        if not item: fail(404, "Treneris nerastas.")
        invalidate(); return response({"item": item})

    def add_catalog(path, collection, schema):
        @router.get(f"/{path}", name=f"list_{path}")
        def listing(all: bool = False, user=Depends(optional_user)):
            criteria = {} if all and user and user["role"] == "admin" else {"isActive": True}
            return response({"items": list(get_db()[collection].find(criteria).sort("name", 1))})

        @router.post(f"/{path}", status_code=201, name=f"create_{path}")
        def create(body: dict = Body(...), user=Depends(require_admin)):
            item = timestamps(schema.model_validate(body).model_dump())
            if collection == "rooms": item["scheduleVersion"] = 0
            get_db()[collection].insert_one(item); invalidate()
            return response({"item": item}, 201)

        @router.patch(f"/{path}/{{item_id}}", name=f"edit_{path}")
        def edit(item_id: str, body: dict = Body(...), user=Depends(require_admin)):
            db = get_db()
            def work(session):
                old = db[collection].find_one({"_id": oid(item_id)}, session=session)
                if not old: fail(404, "Įrašas nerastas.")
                data = schema.model_validate({**old, **body}).model_dump(); data["updatedAt"] = now()
                update = {"$set": data}
                if collection == "rooms": update["$inc"] = {"scheduleVersion": 1}
                item = db[collection].find_one_and_update({"_id": old["_id"]}, update, session=session, return_document=ReturnDocument.AFTER)
                if collection == "rooms" and db.training_sessions.find_one({"roomId": old["_id"], "status": "scheduled", "endsAt": {"$gt": now()}, "capacity": {"$gt": data["capacity"]}}, session=session): fail(409, "Salės talpa mažesnė už suplanuotos treniruotės limitą.")
                return item
            item = transaction(work); invalidate(); return response({"item": item})
    add_catalog("rooms", "rooms", RoomInput)
    add_catalog("exercises", "exercises", ExerciseInput)
    return router
