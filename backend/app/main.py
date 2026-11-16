import logging
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError
from redis import Redis
from . import config
from .db import get_db, init_database
from .common import response, now, timestamps
from .security import current_user, require_admin, hash_password, verify_password, login_cookie, COOKIE
from .schemas import Registration, Login
from .catalogs import create_catalog_router
from .training import create_training_router
from .personal import create_personal_router
from .analytics import create_analytics_router
from .worker import start_event_worker

logger = logging.getLogger("pulse")

def create_app(redis_client=None, run_worker=True):
    cache = redis_client or Redis.from_url(config.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)

    @asynccontextmanager
    async def lifespan(application):
        config.validate_configuration()
        init_database()
        cache.ping()
        stop = start_event_worker() if run_worker else None
        yield
        if stop: stop()
        if redis_client is None: cache.close()

    application = FastAPI(title="Pulse sporto klubo API", version="1.0.0", description="Neribotos narystės, treniruočių registracijos, asmeninis žurnalas, privatūs pokalbiai ir ClickHouse analitika.", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url="/api/redoc", lifespan=lifespan)
    application.state.redis = cache
    application.add_middleware(CORSMiddleware, allow_origins=[config.FRONTEND_ORIGIN], allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type"])

    @application.middleware("http")
    async def request_security(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != config.FRONTEND_ORIGIN: return JSONResponse({"error": "Užklausos kilmė neleidžiama."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site": return JSONResponse({"error": "Kryžminės svetainės užklausa neleidžiama."}, status_code=403)
            if request.method in ("POST", "PATCH", "PUT") and request.headers.get("content-type", "").split(";")[0] != "application/json": return JSONResponse({"error": "Siųskite application/json duomenis."}, status_code=415)
            if len(await request.body()) > 65536: return JSONResponse({"error": "Užklausa per didelė."}, status_code=413)
        if request.url.path.startswith("/api"):
            ip = request.client.host if request.client else "unknown"
            key = f"rate:{ip}:{int(time.time()) // 60}"
            try:
                count = cache.incr(key)
                if count == 1: cache.expire(key, 65)
                if count > 600: return JSONResponse({"error": "Per daug užklausų. Pabandykite vėliau."}, status_code=429)
                if request.url.path in ("/api/auth/login", "/api/auth/register"):
                    auth_key = f"auth-rate:{ip}:{int(time.time()) // 900}"
                    auth_count = cache.incr(auth_key)
                    if auth_count == 1: cache.expire(auth_key, 905)
                    if auth_count > 60: return JSONResponse({"error": "Per daug prisijungimo bandymų."}, status_code=429)
            except Exception:
                pass
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["X-Frame-Options"] = "DENY"
        result.headers["Referrer-Policy"] = "same-origin"
        return result

    @application.exception_handler(HTTPException)
    async def http_error(_request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    @application.exception_handler(RequestValidationError)
    @application.exception_handler(ValidationError)
    async def validation_error(_request, error):
        return JSONResponse({"error": "Patikrinkite įvestus duomenis.", "details": [{"field": ".".join(map(str, issue["loc"])), "message": issue["msg"]} for issue in error.errors()]}, status_code=400)

    @application.exception_handler(DuplicateKeyError)
    async def duplicate_error(_request, _error):
        return JSONResponse({"error": "Toks įrašas jau egzistuoja."}, status_code=409)

    @application.exception_handler(Exception)
    async def unexpected_error(_request, error):
        logger.error("API request failed", exc_info=error)
        return JSONResponse({"error": "Vidinė serverio klaida."}, status_code=500)

    @application.get("/api/health", tags=["Sistema"])
    def health():
        return {"status": "ok"}

    @application.post("/api/auth/register", tags=["Autentifikavimas"], status_code=201)
    def register(body: Registration):
        data = body.model_dump(); password = data.pop("password"); data["email"] = str(data["email"]).lower()
        user = timestamps({**data, "passwordHash": hash_password(password), "role": "member", "isActive": True, "specialization": "", "bookingVersion": 0, "scheduleVersion": 0})
        get_db().users.insert_one(user)
        result = response({"user": user}, 201); login_cookie(result, user); return result

    @application.post("/api/auth/login", tags=["Autentifikavimas"])
    def login(body: Login):
        user = get_db().users.find_one({"email": str(body.email).lower(), "isActive": True})
        if not user or not verify_password(body.password, user["passwordHash"]): raise HTTPException(401, "Neteisingas el. paštas arba slaptažodis.")
        result = response({"user": user}); login_cookie(result, user); return result

    @application.get("/api/auth/me", tags=["Autentifikavimas"])
    def me(user=Depends(current_user)):
        return response({"user": user})

    @application.post("/api/auth/logout", tags=["Autentifikavimas"])
    def logout():
        result = response({"ok": True}); result.delete_cookie(COOKIE, path="/", secure=config.PRODUCTION, httponly=True, samesite="strict"); return result

    application.include_router(create_catalog_router(cache), prefix="/api")
    application.include_router(create_training_router(cache), prefix="/api")
    application.include_router(create_personal_router(), prefix="/api")
    application.include_router(create_analytics_router(cache), prefix="/api/analytics", dependencies=[Depends(require_admin)])
    return application

app = create_app()
