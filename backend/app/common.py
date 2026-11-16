from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException

def now():
    return datetime.now(timezone.utc)

def oid(value):
    if isinstance(value, ObjectId):
        return value
    if not isinstance(value, str) or not ObjectId.is_valid(value):
        raise HTTPException(400, "Neteisingas identifikatorius.")
    return ObjectId(value)

def json_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items() if key not in {"passwordHash", "bookingVersion", "scheduleVersion", "reviewVersion", "purchaseVersion"}}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value

def response(value, status_code=200):
    from fastapi.responses import JSONResponse
    return JSONResponse(json_value(value), status_code=status_code)

def fail(status, message):
    raise HTTPException(status, message)

def timestamps(data):
    stamp = now()
    return {**data, "createdAt": stamp, "updatedAt": stamp}

def populate_user(user_id):
    from .db import get_db
    return get_db().users.find_one({"_id": user_id}, {"firstName": 1, "lastName": 1, "role": 1, "specialization": 1})

def populate_session(item):
    if not item:
        return item
    from .db import get_db
    result = dict(item)
    result["trainerId"] = populate_user(item["trainerId"])
    result["roomId"] = get_db().rooms.find_one({"_id": item["roomId"]}, {"name": 1, "capacity": 1, "location": 1})
    return result
