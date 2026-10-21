import os

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
