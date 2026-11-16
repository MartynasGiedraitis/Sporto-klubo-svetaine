from fastapi import APIRouter, Depends, Body
from pymongo import ReturnDocument
from .db import get_db
from .security import current_user, require_member
from .schemas import LogInput, ConversationInput, MessageInput
from .common import response, oid, now, fail, timestamps, populate_user

def create_personal_router():
    router = APIRouter(tags=["Žurnalas ir privatūs pokalbiai"])

    def checked_log(data):
        if not data.get("durationSeconds") and not (data.get("sets") and data.get("repetitions")): fail(400, "Nurodykite serijas ir pakartojimus arba trukmę.")
        data["exerciseId"] = oid(data["exerciseId"])
        if not get_db().exercises.find_one({"_id": data["exerciseId"], "isActive": True}): fail(400, "Pratimas nerastas arba išjungtas.")
        return data

    def populated_log(item):
        result = dict(item); result["exerciseId"] = get_db().exercises.find_one({"_id": item["exerciseId"]}); return result

    @router.get("/workout-logs")
    def logs(user=Depends(current_user)):
        return response({"items": [populated_log(item) for item in get_db().workout_logs.find({"userId": user["_id"]}).sort("performedAt", -1).limit(500)]})

    @router.post("/workout-logs", status_code=201)
    def create_log(body: LogInput, user=Depends(current_user)):
        item = timestamps({**checked_log(body.model_dump(exclude_none=True)), "userId": user["_id"]})
        get_db().workout_logs.insert_one(item); return response({"item": populated_log(item)}, 201)

    @router.patch("/workout-logs/{item_id}")
    def edit_log(item_id: str, body: dict = Body(...), user=Depends(current_user)):
        db = get_db(); old = db.workout_logs.find_one({"_id": oid(item_id), "userId": user["_id"]})
        if not old: fail(404, "Žurnalo įrašas nerastas.")
        merged = {**old, **body, "exerciseId": body.get("exerciseId", str(old["exerciseId"]))}
        validated = LogInput.model_validate(merged).model_dump()
        data = checked_log({key: value for key, value in validated.items() if value is not None}); data["updatedAt"] = now()
        update = {"$set": data}
        unset = {key: "" for key in ("sets", "repetitions", "weight", "durationSeconds") if key in body and validated[key] is None}
        if unset: update["$unset"] = unset
        item = db.workout_logs.find_one_and_update({"_id": old["_id"]}, update, return_document=ReturnDocument.AFTER)
        return response({"item": populated_log(item)})

    @router.delete("/workout-logs/{item_id}")
    def delete_log(item_id: str, user=Depends(current_user)):
        result = get_db().workout_logs.delete_one({"_id": oid(item_id), "userId": user["_id"]})
        if not result.deleted_count: fail(404, "Žurnalo įrašas nerastas.")
        return {"ok": True}

    def find_chat(item_id, user):
        chat = get_db().conversations.find_one({"_id": oid(item_id), "$or": [{"memberId": user["_id"]}, {"trainerId": user["_id"]}]})
        if not chat: fail(404, "Pokalbis nerastas.")
        return chat

    def populated_chat(chat):
        return {**chat, "memberId": populate_user(chat["memberId"]), "trainerId": populate_user(chat["trainerId"])}

    @router.get("/conversations")
    def chats(user=Depends(current_user)):
        items = get_db().conversations.find({"$or": [{"memberId": user["_id"]}, {"trainerId": user["_id"]}]}).sort("updatedAt", -1)
        return response({"items": [populated_chat(chat) for chat in items]})

    @router.post("/conversations", status_code=201)
    def create_chat(body: ConversationInput, user=Depends(require_member)):
        trainer_id = oid(body.trainerId); db = get_db()
        if not db.users.find_one({"_id": trainer_id, "role": "trainer", "isActive": True}): fail(400, "Treneris nerastas.")
        item = db.conversations.find_one_and_update({"memberId": user["_id"], "trainerId": trainer_id}, {"$setOnInsert": timestamps({"memberId": user["_id"], "trainerId": trainer_id})}, upsert=True, return_document=ReturnDocument.AFTER)
        return response({"item": populated_chat(item)}, 201)

    @router.get("/conversations/{item_id}/messages")
    def messages(item_id: str, before: str | None = None, user=Depends(current_user)):
        chat = find_chat(item_id, user); db = get_db(); criteria = {"conversationId": chat["_id"]}
        if before: criteria["_id"] = {"$lt": oid(before)}
        items = list(db.messages.find(criteria).sort("_id", -1).limit(100))
        cursor = str(items[-1]["_id"]) if len(items) == 100 else None
        db.messages.update_many({"conversationId": chat["_id"], "senderId": {"$ne": user["_id"]}, "readAt": None}, {"$set": {"readAt": now()}})
        for item in items: item["senderId"] = populate_user(item["senderId"])
        return response({"items": list(reversed(items)), "nextCursor": cursor})

    @router.post("/conversations/{item_id}/messages", status_code=201)
    def send_message(item_id: str, body: MessageInput, user=Depends(current_user)):
        chat = find_chat(item_id, user); db = get_db()
        item = timestamps({"conversationId": chat["_id"], "senderId": user["_id"], "content": body.content, "sentAt": now(), "readAt": None})
        db.messages.insert_one(item)
        db.conversations.update_one({"_id": chat["_id"]}, {"$set": {"updatedAt": now()}})
        item["senderId"] = populate_user(user["_id"]); return response({"item": item}, 201)
    return router
