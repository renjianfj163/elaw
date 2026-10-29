import os

from fastapi.testclient import TestClient

from app.llm.provider import _config_path
from app.main import app
from app.public_trial import reset_for_tests


def _client():
    return TestClient(app)


def test_public_trial_health_hides_the_key_and_explains_the_upload(monkeypatch):
    monkeypatch.setenv("ELAW_PUBLIC", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "trial-secret-value")
    response = _client().get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["public_trial"] is True
    assert "送去 Gemini" in body["notice"]
    assert "保密" in body["notice"]
    assert body["limits"]["max_files"] == 8
    assert "trial-secret-value" not in response.text
    assert "api_key" not in body


def test_public_trial_rejects_settings_that_could_redirect_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_PUBLIC", "1")
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "server-key")
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443\n"
        "  model: gemini-3.5-flash\n"
        "  thinking: enabled\n"
        "  reasoning_effort: high\n"
        "  max_tokens: 8192\n"
        "  response_format: json_object\n"
        "  retries: 2\n"
        "  cache: true\n"
        "  prompt_version: \"2026-10-21\"\n"
        "  outbound_allowed: true\n",
        encoding="utf-8",
    )
    client = _client()
    current = client.get("/api/llm/settings")
    assert current.status_code == 200
    assert current.json()["public_trial"] is True
    assert "api_key" not in current.json()
    assert "server-key" not in current.text

    rejected = client.put(
        "/api/llm/settings",
        json={
            "provider": "gemini",
            "base_url": "http://evil.example",
            "model": "gemini-3.5-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens": 8192,
            "response_format": "json_object",
            "retries": 2,
            "cache": True,
            "prompt_version": "2026-10-21",
            "outbound_allowed": True,
            "api_key": "attacker-key",
        },
    )
    assert rejected.status_code == 403
    stored = (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    assert "evil.example" not in stored
    assert "attacker-key" not in stored
    assert os.environ["GEMINI_API_KEY"] == "server-key"
    assert "evil.example" not in _config_path("llm.yaml").read_text(encoding="utf-8")


def test_public_trial_ping_ignores_a_rewritten_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_PUBLIC", "1")
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "server-key")
    monkeypatch.setenv("ELAW_TRIAL_PING_LIMIT", "2")
    reset_for_tests()
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443\n"
        "  model: gemini-3.5-flash\n"
        "  thinking: enabled\n"
        "  reasoning_effort: high\n"
        "  max_tokens: 8192\n"
        "  response_format: json_object\n"
        "  retries: 2\n"
        "  cache: true\n"
        "  prompt_version: \"2026-10-21\"\n"
        "  outbound_allowed: true\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_ping(model, base_url, api_key):
        seen["model"] = model
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return '{"ok": true}'

    monkeypatch.setattr("app.llm.api.ping_gemini", fake_ping)
    response = _client().post(
        "/api/llm/ping",
        json={"provider": "gemini", "base_url": "http://evil.example", "api_key": "attacker-key"},
    )
    assert response.status_code == 200
    assert seen["base_url"] == "http://108.160.140.173:8443"
    assert seen["api_key"] == "server-key"
    assert "attacker-key" not in (tmp_path / "llm.yaml").read_text(encoding="utf-8")


def test_public_trial_rate_limit_stops_repeated_pings(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_PUBLIC", "1")
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "server-key")
    monkeypatch.setenv("ELAW_TRIAL_PING_LIMIT", "1")
    reset_for_tests()
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443\n"
        "  model: gemini-3.5-flash\n"
        "  thinking: enabled\n"
        "  reasoning_effort: high\n"
        "  max_tokens: 8192\n"
        "  response_format: json_object\n"
        "  retries: 2\n"
        "  cache: true\n"
        "  prompt_version: \"2026-10-21\"\n"
        "  outbound_allowed: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.llm.api.ping_gemini", lambda model, base_url, api_key: '{"ok": true}')
    client = _client()
    assert client.post("/api/llm/ping").status_code == 200
    limited = client.post("/api/llm/ping")
    assert limited.status_code == 429
    assert "次数" in limited.json()["detail"]


def test_public_trial_caps_schematic_batches(monkeypatch):
    monkeypatch.setenv("ELAW_PUBLIC", "1")
    reset_for_tests()
    files = [("schematic", (f"{index}.pdf", b"%PDF-1.4", "application/pdf")) for index in range(9)]
    response = _client().post("/api/schematic/review", files=files)
    assert response.status_code == 400
    assert "8" in response.json()["detail"]


def test_public_trial_opens_without_the_local_launch_cookie(monkeypatch):
    monkeypatch.setenv("ELAW_PUBLIC", "1")
    monkeypatch.setenv("ELAW_GUARD", "1")
    monkeypatch.setenv("ELAW_PORT", "8000")
    response = TestClient(app, base_url="http://203.0.113.8:8000").get("/api/health")
    assert response.status_code == 200
    assert response.json()["public_trial"] is True
    shutdown = TestClient(app, base_url="http://203.0.113.8:8000").post("/api/shutdown")
    assert shutdown.json()["ok"] is False


def test_local_mode_still_allows_saving_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ELAW_PUBLIC", raising=False)
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443\n"
        "  model: gemini-3.5-flash\n"
        "  thinking: enabled\n"
        "  reasoning_effort: high\n"
        "  max_tokens: 8192\n"
        "  response_format: json_object\n"
        "  retries: 2\n"
        "  cache: true\n"
        "  prompt_version: \"2026-10-21\"\n"
        "  outbound_allowed: true\n",
        encoding="utf-8",
    )
    saved = _client().put(
        "/api/llm/settings",
        json={
            "provider": "gemini",
            "base_url": "http://108.160.140.173:8443",
            "model": "gemini-3.5-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens": 8192,
            "response_format": "json_object",
            "retries": 2,
            "cache": True,
            "prompt_version": "2026-10-21",
            "outbound_allowed": True,
            "api_key": "typed-by-the-user",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["public_trial"] is False
    assert saved.json()["api_key_configured"] is True
    assert "api_key" not in saved.json()
    assert "typed-by-the-user" not in (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    os.environ.pop("GEMINI_API_KEY", None)
