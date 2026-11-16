"""Separate Python frontend. Every durable operation goes through the backend API."""
import asyncio
import hmac
import os
import secrets
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).parent
API_URL = os.getenv("API_URL", "http://backend:3000").rstrip("/")
TZ = ZoneInfo("Europe/Vilnius")
app = FastAPI(title="PULSE frontend", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "templates")
LABELS = {"registered": "Užsiregistruota", "cancelled": "Atšaukta", "attended": "Dalyvauta", "no_show": "Neatvykta", "active": "Aktyvi", "expired": "Pasibaigusi", "scheduled": "Planuojama", "completed": "Pasibaigusi"}
PAGES = {"overview": ("Apžvalga", "grid"), "schedule": ("Treniruočių tvarkaraštis", "calendar"), "bookings": ("Mano registracijos", "check"), "membership": ("Narystė", "card"), "journal": ("Treniruočių žurnalas", "book"), "messages": ("Pokalbiai", "message"), "manage": ("Valdyti treniruotes", "users"), "catalog": ("Klubo administravimas", "edit"), "reports": ("Ataskaitos", "chart")}
CATALOGS = {
    "plans": {"title": "Narystės planai", "fields": [("name", "Pavadinimas", "text"), ("price", "Kaina (€)", "number"), ("durationDays", "Trukmė (dienomis)", "number")]},
    "rooms": {"title": "Salės", "fields": [("name", "Pavadinimas", "text"), ("capacity", "Talpa", "number"), ("location", "Vieta", "text")]},
    "exercises": {"title": "Pratimai", "fields": [("name", "Pavadinimas", "text"), ("muscleGroup", "Raumenų grupė", "text"), ("category", "Kategorija", "text"), ("description", "Aprašymas", "text")]},
    "trainers": {"title": "Treneriai", "fields": [("firstName", "Vardas", "text"), ("lastName", "Pavardė", "text"), ("specialization", "Specializacija", "text")]},
}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


async def api(request, path, method="GET", body=None):
    try:
        async with httpx.AsyncClient(base_url=API_URL, timeout=20) as client:
            response = await client.request(method, "/api" + path, json=body, cookies={k: v for k, v in request.cookies.items() if k != "pulse_csrf"})
    except httpx.RequestError:
        raise ApiError("Nepavyksta pasiekti serverio. Patikrinkite, ar paleistos visos Docker paslaugos.", 503)
    try:
        result = response.json() if response.content else {}
    except ValueError:
        raise ApiError("Serveris grąžino netinkamą atsakymą.", 502)
    if response.is_error:
        error = result.get("error", result.get("message", "Užklausos įvykdyti nepavyko."))
        if isinstance(error, dict):
            error = error.get("message", "Užklausos įvykdyti nepavyko.")
        if not isinstance(error, str):
            error = "Užklausos duomenys netinkami. Patikrinkite laukus."
        raise ApiError(error, response.status_code)
    return result, response


def parsed(value):
    if isinstance(value, datetime):
        return value.astimezone(TZ) if value.tzinfo else value.replace(tzinfo=TZ)
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(TZ)


def fmt(value, pattern="%Y-%m-%d"):
    return parsed(value).strftime(pattern) if value else "—"


def identifier(item):
    return item.get("_id", "") if isinstance(item, dict) else item or ""


def fullname(user):
    return f"{user.get('firstName', '')} {user.get('lastName', '')}".strip() if isinstance(user, dict) else "Treneris"


def enrich_session(s):
    s = dict(s)
    s["free"] = max(0, s.get("capacity", 0) - s.get("bookedCount", 0))
    s["occupancy"] = min(100, s.get("bookedCount", 0) / max(1, s.get("capacity", 1)) * 100)
    s["future"] = bool(s.get("startsAt") and parsed(s["startsAt"]) > datetime.now(TZ))
    s["minutes"] = round((parsed(s["endsAt"]) - parsed(s["startsAt"])).total_seconds() / 60) if s.get("startsAt") and s.get("endsAt") else 0
    return s


def iso_local(value):
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise ApiError("Nurodykite tinkamą datą ir laiką.")


def redirect(page="overview", message="", error="", **kwargs):
    return RedirectResponse("/?" + urlencode({"page": page, **kwargs, **({"message": message} if message else {}), **({"error": error} if error else {})}), status_code=303)


def csrf_response(request, template, context, status=200):
    token = request.cookies.get("pulse_csrf") or secrets.token_urlsafe(32)
    context.update(request=request, csrf=token, LABELS=LABELS, PAGES=PAGES, CATALOGS=CATALOGS, now=datetime.now(TZ), year=date.today().year)
    response = templates.TemplateResponse(request=request, name=template, context=context, status_code=status)
    response.set_cookie("pulse_csrf", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=86400)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    return response


async def checked_form(request):
    form = await request.form()
    cookie = request.cookies.get("pulse_csrf", "")
    if not cookie or not hmac.compare_digest(cookie, str(form.get("csrf", ""))):
        raise ApiError("Forma nebegalioja. Atnaujinkite puslapį ir pabandykite dar kartą.", 403)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise ApiError("Ši užklausa neleidžiama.", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError("Ši užklausa neleidžiama.", 403)
    return form


templates.env.filters.update(fmt=fmt, localinput=lambda v: fmt(v, "%Y-%m-%dT%H:%M") if v else "", money=lambda v: f"{float(v or 0):.2f} €".replace(".", ","))
templates.env.globals.update(identifier=identifier, fullname=fullname, parsed=parsed)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    query = request.query_params
    user = None
    error = query.get("error", "")
    try:
        result, _ = await api(request, "/auth/me")
        user = result.get("user")
    except ApiError as e:
        if e.status != 401:
            error = str(e)
    if not user:
        return csrf_response(request, "auth.html", {"register": query.get("register") == "1", "error": error})
    page = query.get("page", "overview")
    if page not in PAGES or page in ("catalog", "reports") and user["role"] != "admin" or page == "manage" and user["role"] not in ("admin", "trainer"):
        page = "overview"
    ctx = {"user": user, "page": page, "query": query, "error": error, "message": query.get("message", ""), "sessions": [], "plans": [], "memberships": [], "bookings": [], "trainers": [], "rooms": [], "exercises": [], "logs": [], "conversations": [], "messages": [], "attendance": [], "selected_conversation": None, "report": None, "edit": {}, "upcoming": [], "upcoming_bookings": [], "history_bookings": [], "active_memberships": [], "current_membership": None, "attended_count": 0, "bookings_by_session": {}, "managed_sessions": [], "filtered_sessions": [], "types": [], "catalog": query.get("catalog", "plans") if query.get("catalog", "plans") in CATALOGS else "plans"}
    try:
        suffix = "?all=true" if user["role"] == "admin" else ""
        paths = {"sessions": "/sessions", "plans": "/plans" + suffix, "memberships": "/memberships/me", "bookings": "/bookings/me", "trainers": "/trainers" + suffix, "rooms": "/rooms" + suffix, "exercises": "/exercises" + suffix}
        results = await asyncio.gather(*(api(request, path) for path in paths.values()))
        for key, (result, _) in zip(paths, results):
            ctx[key] = result.get("items", [])
        ctx["sessions"] = [enrich_session(s) for s in ctx["sessions"]]
        ctx["upcoming"] = sorted([s for s in ctx["sessions"] if s["future"] and s.get("status") == "scheduled"], key=lambda s: s["startsAt"])
        ctx["active_memberships"] = [m for m in ctx["memberships"] if m.get("status") == "active" and parsed(m["expiresAt"]) > datetime.now(TZ)]
        ctx["current_membership"] = next((m for m in ctx["active_memberships"] if parsed(m["startsAt"]) <= datetime.now(TZ)), None)
        ctx["upcoming_bookings"] = [b for b in ctx["bookings"] if b.get("status") == "registered" and isinstance(b.get("sessionId"), dict) and parsed(b["sessionId"].get("startsAt")) > datetime.now(TZ)]
        ctx["history_bookings"] = [b for b in ctx["bookings"] if b not in ctx["upcoming_bookings"]]
        ctx["attended_count"] = sum(b.get("status") == "attended" for b in ctx["bookings"])
        ctx["bookings_by_session"] = {identifier(b.get("sessionId")): b for b in ctx["bookings"] if b.get("status") == "registered"}
        if page == "schedule":
            ctx["types"] = sorted({s.get("type", "") for s in ctx["sessions"] if s.get("type")})
            ctx["filtered_sessions"] = [s for s in ctx["upcoming"] if (not query.get("day") or fmt(s["startsAt"]) == query["day"]) and (not query.get("trainer") or identifier(s.get("trainerId")) == query["trainer"]) and (not query.get("type") or s.get("type") == query["type"])]
        if page == "journal":
            result, _ = await api(request, "/workout-logs")
            ctx["logs"] = result.get("items", [])
            ctx["edit"] = next((l for l in ctx["logs"] if l["_id"] == query.get("edit")), {})
        if page == "messages":
            result, _ = await api(request, "/conversations")
            ctx["conversations"] = result.get("items", [])
            selected = next((c for c in ctx["conversations"] if c["_id"] == query.get("conversation")), None)
            ctx["selected_conversation"] = selected
            if selected:
                path = f"/conversations/{selected['_id']}/messages"
                if query.get("before"):
                    path += "?" + urlencode({"before": query["before"]})
                result, _ = await api(request, path)
                ctx["messages"] = result.get("items", [])
                ctx["next_cursor"] = result.get("nextCursor")
        if page == "manage":
            ctx["managed_sessions"] = sorted([s for s in ctx["sessions"] if user["role"] == "admin" or identifier(s.get("trainerId")) == user["_id"]], key=lambda s: s["startsAt"], reverse=True)
            ctx["edit"] = next((s for s in ctx["managed_sessions"] if s["_id"] == query.get("edit")), {})
            if query.get("attendance"):
                result, _ = await api(request, f"/sessions/{query['attendance']}/bookings")
                ctx["attendance"] = result.get("items", [])
                ctx["attendance_session"] = next((s for s in ctx["managed_sessions"] if s["_id"] == query["attendance"]), {})
        if page == "catalog":
            ctx["catalog"] = query.get("catalog", "plans") if query.get("catalog", "plans") in CATALOGS else "plans"
            ctx["edit"] = next((i for i in ctx[ctx["catalog"]] if i["_id"] == query.get("edit")), {})
        if page == "reports" and query.get("from") and query.get("to"):
            result, _ = await api(request, "/analytics/summary?" + urlencode({"from": query["from"], "to": query["to"]}))
            ctx["report"] = result
    except ApiError as e:
        ctx["error"] = str(e)
    return csrf_response(request, "app.html", ctx)


@app.post("/login")
@app.post("/register")
async def login(request: Request):
    register = request.url.path == "/register"
    try:
        form = await checked_form(request)
        body = {"email": str(form.get("email", "")), "password": str(form.get("password", ""))}
        if register:
            body.update(firstName=str(form.get("firstName", "")), lastName=str(form.get("lastName", "")), birthYear=int(form.get("birthYear", "0")))
        _, upstream = await api(request, "/auth/register" if register else "/auth/login", "POST", body)
        response = redirect()
        for cookie in upstream.headers.get_list("set-cookie"):
            response.headers.append("set-cookie", cookie)
        return response
    except (ApiError, ValueError) as e:
        return RedirectResponse("/?" + urlencode({"register": "1" if register else "0", "error": str(e) if isinstance(e, ApiError) else "Patikrinkite gimimo metus."}), status_code=303)


@app.post("/logout")
async def logout(request: Request):
    try:
        await checked_form(request)
        _, upstream = await api(request, "/auth/logout", "POST", {})
        response = redirect()
        for cookie in upstream.headers.get_list("set-cookie"):
            response.headers.append("set-cookie", cookie)
        return response
    except ApiError as e:
        return redirect(error=str(e))


@app.post("/actions/{action}")
async def actions(request: Request, action: str):
    page = "overview"
    extra = {}
    try:
        f = await checked_form(request)
        page = str(f.get("page", "overview"))
        if page not in PAGES:
            page = "overview"
        if action == "book":
            await api(request, "/bookings", "POST", {"sessionId": f["sessionId"]})
            message = "Registracija patvirtinta. Lauksime tavęs!"
        elif action == "cancel-booking":
            await api(request, f"/bookings/{f['id']}", "DELETE")
            message = "Registracija atšaukta."
        elif action == "purchase":
            await api(request, "/memberships", "POST", {"planId": f["planId"]})
            message = "Narystė įsigyta. Laikas judėti!"
        elif action == "review":
            await api(request, "/reviews", "POST", {"bookingId": f["bookingId"], "rating": int(f["rating"]), "comment": f.get("comment", "")})
            extra["tab"] = "history"
            message = "Ačiū už atsiliepimą!"
        elif action == "save-log":
            body = {"exerciseId": f["exerciseId"], "performedAt": iso_local(f["performedAt"]), "sets": int(f["sets"]), "notes": f.get("notes", "")}
            for key in ("repetitions", "weight", "durationSeconds"):
                if f.get(key):
                    body[key] = float(f[key]) if key == "weight" else int(f[key])
                else:
                    body[key] = None
            item_id = str(f.get("id", ""))
            await api(request, "/workout-logs" + ("/" + item_id if item_id else ""), "PATCH" if item_id else "POST", body)
            message = "Treniruotės rezultatas išsaugotas."
        elif action == "delete-log":
            await api(request, f"/workout-logs/{f['id']}", "DELETE")
            message = "Įrašas ištrintas."
        elif action == "conversation":
            result, _ = await api(request, "/conversations", "POST", {"trainerId": f["trainerId"]})
            extra["conversation"] = result["item"]["_id"]
            message = "Pokalbis paruoštas."
        elif action == "message":
            await api(request, f"/conversations/{f['conversationId']}/messages", "POST", {"content": f["content"]})
            extra["conversation"] = f["conversationId"]
            message = "Žinutė išsiųsta."
        elif action == "save-session":
            body = {"title": f["title"], "type": f["type"], "description": f.get("description", ""), "roomId": f["roomId"], "capacity": int(f["capacity"]), "startsAt": iso_local(f["startsAt"]), "endsAt": iso_local(f["endsAt"])}
            if f.get("trainerId"):
                body["trainerId"] = f["trainerId"]
            item_id = str(f.get("id", ""))
            await api(request, "/sessions" + ("/" + item_id if item_id else ""), "PATCH" if item_id else "POST", body)
            message = "Treniruotė išsaugota."
        elif action == "cancel-session":
            await api(request, f"/sessions/{f['id']}", "DELETE")
            message = "Treniruotė ir jos registracijos atšauktos."
        elif action == "attendance":
            await api(request, f"/bookings/{f['id']}/attendance", "PATCH", {"status": f["status"]})
            extra["attendance"] = f["sessionId"]
            message = "Lankomumas pažymėtas."
        elif action == "catalog":
            entity = str(f["entity"])
            if entity not in CATALOGS:
                raise ApiError("Nežinomas katalogas.")
            extra["catalog"] = entity
            body = {key: str(f.get(key, "")) for key, _, _ in CATALOGS[entity]["fields"]}
            for key in ("price", "durationDays", "capacity"):
                if key in body:
                    body[key] = float(body[key]) if key == "price" else int(body[key])
            body["isActive"] = f.get("isActive") == "true"
            item_id = str(f.get("id", ""))
            if entity == "trainers":
                body.update(email=f["email"], birthYear=int(f["birthYear"]))
                if not item_id:
                    body["password"] = f["password"]
            await api(request, "/" + entity + ("/" + item_id if item_id else ""), "PATCH" if item_id else "POST", body)
            message = "Pakeitimai išsaugoti."
        else:
            raise ApiError("Nežinomas veiksmas.")
        return redirect(page, message=message, **extra)
    except (ApiError, ValueError, KeyError) as e:
        return redirect(page, error=str(e) if isinstance(e, ApiError) else "Patikrinkite visus formos laukus.", **extra)
