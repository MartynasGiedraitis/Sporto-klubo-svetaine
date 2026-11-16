import hashlib
import hmac
import secrets
from datetime import timedelta
import jwt
from fastapi import Depends, Request
from fastapi.security import APIKeyCookie
from . import config
from .common import now, oid, fail
from .db import get_db

COOKIE = "pulse_session"
cookie_scheme = APIKeyCookie(name=COOKIE, auto_error=False)

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"

def verify_password(password, stored):
    try:
        algorithm, salt, expected = stored.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False

def login_cookie(response, user):
    token = jwt.encode({"sub": str(user["_id"]), "exp": now() + timedelta(hours=8)}, config.JWT_SECRET, algorithm="HS256")
    response.set_cookie(COOKIE, token, max_age=8*3600, httponly=True, secure=config.PRODUCTION, samesite="strict", path="/")

def optional_user(request: Request, token=Depends(cookie_scheme)):
    if not token:
        return None
    try:
        claims = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
        user = get_db().users.find_one({"_id": oid(claims["sub"]), "isActive": True}, {"passwordHash": 0})
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    return user

def current_user(user=Depends(optional_user)):
    if not user:
        fail(401, "Prisijunkite prie sistemos.")
    return user

def require_admin(user=Depends(current_user)):
    if user["role"] != "admin":
        fail(403, "Neturite administratoriaus teisių.")
    return user

def require_coach(user=Depends(current_user)):
    if user["role"] not in ("trainer", "admin"):
        fail(403, "Neturite trenerio teisių.")
    return user

def require_member(user=Depends(current_user)):
    if user["role"] != "member":
        fail(403, "Šis veiksmas skirtas nariams.")
    return user
