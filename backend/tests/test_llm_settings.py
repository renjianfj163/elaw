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


def test_gemini_settings_keep_the_proxy_and_leave_the_key_out_of_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.setattr("app.llm.runtime_env.read_user_env", lambda name: "")
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443\n"
        "  model: gemini-2.5-pro\n"
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
    client = TestClient(app)
    saved = client.put(
        "/api/llm/settings",
        json={
            "provider": "gemini",
            "base_url": "http://108.160.140.173:8443/v1beta",
            "model": "custom-name",
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
    assert saved.status_code == 200
    body = saved.json()
    assert body["provider"] == "gemini"
    assert body["model"] == "gemini-2.5-pro"
    assert body["base_url"] == "http://108.160.140.173:8443"
    assert body["sends_temperature"] is False
    assert body["api_key_configured"] is True
    assert body["api_key_env"] == "GEMINI_API_KEY"
    assert body["presets"]["deepseek"]["model"] == "deepseek-v4-flash"
    assert body["presets"]["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert "api_key" not in body
    stored = (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    assert "secret-value" not in stored
    assert "provider: gemini" in stored
    assert "  base_url: http://108.160.140.173:8443\n" in stored
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GEMINI_BASE_URL", None)


def test_gemini_ping_uses_the_proxy_and_not_temperature(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("app.llm.runtime_env.read_user_env", lambda name: "")
    seen = {}

    def fake_ping(model, base_url, api_key):
        seen["model"] = model
        seen["base_url"] = base_url
        seen["api_key"] = api_key
        return '{"ok": true}'

    monkeypatch.setattr("app.llm.api.ping_gemini", fake_ping)
    client = TestClient(app)
    response = client.post(
        "/api/llm/ping",
        json={
            "provider": "gemini",
            "base_url": "http://108.160.140.173:8443/v1beta",
            "model": "gemini-2.5-pro",
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
    assert response.json()["model"] == "gemini-2.5-pro"
    assert seen["base_url"] == "http://108.160.140.173:8443"
    assert seen["api_key"] == "secret-value"
    assert "secret-value" not in (tmp_path / "llm.yaml").read_text(encoding="utf-8")
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GEMINI_BASE_URL", None)


def test_proxy_falls_back_to_the_previous_user_gateway(monkeypatch):
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    monkeypatch.setattr(
        "app.llm.runtime_env.read_user_env",
        lambda name: "http://108.160.140.173:8443/v1beta" if name == "GEMINI_BASE_URL" else "",
    )
    from app.llm.endpoint import gemini_proxy_base

    assert gemini_proxy_base({"provider": "deepseek"}) == "http://108.160.140.173:8443"
    os.environ.pop("GEMINI_BASE_URL", None)
