import os
import time

from fastapi.testclient import TestClient

from app.local_guard import issue_launch_token, reset_for_tests
from app.main import app


def _client():
    return TestClient(app, base_url="http://127.0.0.1:8000")


def test_guard_off_still_answers_health():
    response = _client().get("/api/health")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in {key.lower() for key in response.headers}


def test_local_guard_requires_launch_cookie_and_rejects_other_sites(monkeypatch):
    monkeypatch.setenv("ELAW_GUARD", "1")
    monkeypatch.setenv("ELAW_PORT", "8000")
    reset_for_tests()
    client = _client()

    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/auth/launch", params={"token": "nope"}).status_code == 401

    token = issue_launch_token()
    opened = client.get("/auth/launch", params={"token": token}, follow_redirects=False)
    assert opened.status_code == 303
    cookie = opened.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert client.get("/auth/launch", params={"token": token}, follow_redirects=False).status_code == 401

    assert client.get("/api/health").status_code == 200
    assert client.post("/api/heartbeat").status_code == 403
    assert client.post("/api/heartbeat", headers={"Origin": "https://example.test"}).status_code == 403
    assert client.post("/api/heartbeat", headers={"Origin": "http://127.0.0.1:3000"}).status_code == 403
    allowed = client.post("/api/heartbeat", headers={"Origin": "http://127.0.0.1:8000"})
    assert allowed.status_code == 200
    assert allowed.json()["ok"] is True
    reset_for_tests()


def test_shutdown_stays_inside_the_guard(monkeypatch):
    monkeypatch.setenv("ELAW_GUARD", "1")
    monkeypatch.setenv("ELAW_PORT", "8000")
    monkeypatch.setenv("ELAW_SHUTDOWN_DELAY", "0")
    reset_for_tests()
    called = []
    monkeypatch.setattr("app.local_guard._terminate", lambda: called.append("exit"))
    client = _client()
    token = issue_launch_token()
    client.get("/auth/launch", params={"token": token}, follow_redirects=False)
    response = client.post("/api/shutdown", headers={"Origin": "http://127.0.0.1:8000"})
    assert response.json()["ok"] is True
    deadline = time.time() + 1
    while time.time() < deadline and not called:
        time.sleep(0.02)
    assert called == ["exit"]

    monkeypatch.setenv("ELAW_GUARD", "0")
    assert _client().post("/api/shutdown").json()["ok"] is False
    os.environ["ELAW_GUARD"] = "0"
