import os

import httpx
from fastapi.testclient import TestClient

from app.llm.provider import build_request, load_llm_config
from app.main import app


def test_llm_settings_keep_flash_thinking_high_and_leave_the_key_out_of_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: deepseek\n"
        "  base_url: https://api.deepseek.com\n"
        "  model: deepseek-v4-flash\n"
        "  thinking: enabled\n"
        "  reasoning_effort: high\n"
        "  max_tokens: 8192\n"
        "  response_format: json_object\n"
        "  retries: 2\n"
        "  cache: true\n"
        "  prompt_version: \"2026-10-21\"\n"
        "  outbound_allowed: false\n",
        encoding="utf-8",
    )
    client = TestClient(app)
    current = client.get("/api/llm/settings")
    assert current.status_code == 200
    assert current.json()["model"] == "deepseek-v4-flash"
    assert current.json()["thinking"] == "enabled"
    assert current.json()["reasoning_effort"] == "high"
    assert current.json()["sends_temperature"] is False
    assert current.json()["api_key_configured"] is False
    assert "api_key" not in current.json()

    saved = client.put(
        "/api/llm/settings",
        json={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens": 8192,
            "response_format": "json_object",
            "retries": 2,
            "cache": True,
            "prompt_version": "2026-10-21",
            "outbound_allowed": False,
            "api_key": "secret-value",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["api_key_configured"] is True
    stored = (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    assert "secret-value" not in stored
    body = build_request(load_llm_config(), "请输出 JSON")
    assert "temperature" not in body
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    blocked = client.post("/api/llm/ping")
    assert blocked.status_code == 400
    os.environ.pop("DEEPSEEK_API_KEY", None)


def test_ping_uses_the_key_typed_in_the_form(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        seen["body"] = json
        seen["timeout"] = timeout

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "{\"ok\": true}"}}]}

        return Response()

    monkeypatch.setattr("app.llm.deepseek.httpx.post", fake_post)
    client = TestClient(app)
    response = client.post(
        "/api/llm/ping",
        json={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens": 8192,
            "response_format": "json_object",
            "retries": 2,
            "cache": True,
            "prompt_version": "2026-10-21",
            "outbound_allowed": True,
            "api_key": "secret-value",
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["auth"] == "Bearer secret-value"
    assert seen["body"]["model"] == "deepseek-v4-flash"
    assert "temperature" not in seen["body"]
    stored = (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    assert "secret-value" not in stored
    os.environ.pop("DEEPSEEK_API_KEY", None)


def test_ping_reports_the_interface_status_when_the_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def fake_post(url, headers=None, json=None, timeout=None):
        request = httpx.Request("POST", url)
        return httpx.Response(401, request=request, text='{"error":{"message":"Authentication Fails"}}')

    monkeypatch.setattr("app.llm.deepseek.httpx.post", fake_post)
    client = TestClient(app)
    response = client.post(
        "/api/llm/ping",
        json={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens": 8192,
            "response_format": "json_object",
            "retries": 2,
            "cache": True,
            "prompt_version": "2026-10-21",
            "outbound_allowed": True,
            "api_key": "secret-value",
        },
    )
    assert response.status_code == 400
    assert "401" in response.json()["detail"]
    assert "Authentication Fails" in response.json()["detail"]
    assert "secret-value" not in response.json()["detail"]
    os.environ.pop("DEEPSEEK_API_KEY", None)
