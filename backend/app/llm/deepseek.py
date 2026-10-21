import os

import httpx

from app.llm.provider import load_llm_config


def deepseek_transport():
    cfg = load_llm_config()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base = str(cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")

    def send(body: dict) -> str:
        if not api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY")
        response = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=180,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        return message.get("content") or ""

    return send
