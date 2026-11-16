"""Isolated frontend checks; backend persistence is tested separately."""
import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location("pulse_frontend", Path(__file__).parents[1] / "app.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


@pytest.fixture
def client():
    with TestClient(ui.app) as session:
        yield session


def test_anonymous_auth_pages_and_secure_forms(client, monkeypatch):
    async def unauthenticated(*args, **kwargs):
        raise ui.ApiError("Neprisijungta", 401)
    monkeypatch.setattr(ui, "api", unauthenticated)
    for url in ("/", "/?register=1"):
        response = client.get(url)
        assert response.status_code == 200
        assert 'name="csrf"' in response.text
        assert "<script" not in response.text
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("page", list(ui.PAGES))
def test_all_admin_screens_render_empty_states(page, client, monkeypatch):
    async def empty_backend(request, path, method="GET", body=None):
        if path == "/auth/me":
            return {"user": {"_id": "u1", "firstName": "Austėja", "lastName": "Testas", "role": "admin"}}, None
        return {"items": []}, None
    monkeypatch.setattr(ui, "api", empty_backend)
    response = client.get("/", params={"page": page})
    assert response.status_code == 200
    assert 'role="alert"' not in response.text
    assert "<script" not in response.text


@pytest.mark.parametrize("headers,token", [({}, ""), ({"origin": "https://foreign.example"}, "valid"), ({"sec-fetch-site": "cross-site"}, "valid")])
def test_csrf_rejected_before_backend_call(headers, token, client, monkeypatch):
    async def forbidden_call(*args, **kwargs):
        pytest.fail("CSRF rejection must happen before contacting the backend")
    monkeypatch.setattr(ui, "api", forbidden_call)
    client.cookies.set("pulse_csrf", "valid")
    response = client.post("/actions/book", headers=headers, data={"csrf": token, "page": "schedule", "sessionId": "s1"}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_login_preserves_backend_session_cookie(client, monkeypatch):
    calls = []
    async def backend_login(request, path, method="GET", body=None):
        calls.append((path, method, body))
        return {"user": {}}, httpx.Response(200, headers={"set-cookie": "pulse_session=example; HttpOnly; Path=/; SameSite=strict"})
    monkeypatch.setattr(ui, "api", backend_login)
    client.cookies.set("pulse_csrf", "valid")
    response = client.post("/login", data={"csrf": "valid", "email": "member@pulse.lt", "password": "Sportas123!"}, follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("/auth/login", "POST", {"email": "member@pulse.lt", "password": "Sportas123!"})]
    assert client.cookies.get("pulse_session") == "example"


def test_workout_form_converts_values_and_lithuanian_time(client, monkeypatch):
    calls = []
    async def backend_save(request, path, method="GET", body=None):
        calls.append((path, method, body))
        return {"item": {}}, None
    monkeypatch.setattr(ui, "api", backend_save)
    client.cookies.set("pulse_csrf", "valid")
    response = client.post("/actions/save-log", data={"csrf": "valid", "page": "journal", "exerciseId": "e1", "performedAt": "2026-11-16T18:00", "sets": "3", "repetitions": "10", "weight": "54.5", "durationSeconds": "", "notes": "Progresas"}, follow_redirects=False)
    assert response.status_code == 303
    path, method, body = calls[0]
    assert (path, method) == ("/workout-logs", "POST")
    assert body["performedAt"] == "2026-11-16T16:00:00+00:00"
    assert body["sets"] == 3 and body["weight"] == 54.5
    assert body["durationSeconds"] is None
