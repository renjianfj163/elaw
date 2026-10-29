"""Gemini 与 DeepSeek 的默认地址。代理根地址不要带 /v1beta。"""

from __future__ import annotations

from app.llm.runtime_env import runtime_env

DEFAULT_GEMINI_PROXY = "http://108.160.140.173:8443"
DEEPSEEK_BASE = "https://api.deepseek.com"
MODELS = {"gemini": "gemini-2.5-pro", "deepseek": "deepseek-v4-flash"}
LABELS = {
    "gemini": "Gemini · gemini-2.5-pro",
    "deepseek": "DeepSeek · deepseek-v4-flash",
}


def normalize_gemini_base(url: str) -> str:
    base = str(url or "").strip().rstrip("/")
    if base.endswith("/v1beta"):
        base = base[: -len("/v1beta")]
    return base


def gemini_proxy_base(config: dict | None = None) -> str:
    cfg = config
    if cfg is None:
        try:
            from app.llm.provider import load_llm_config

            cfg = load_llm_config()
        except Exception:
            cfg = {}
    if str(cfg.get("provider") or "") == "gemini":
        saved = str(cfg.get("base_url") or "").strip()
        if saved:
            return normalize_gemini_base(saved)
    env = runtime_env("GEMINI_BASE_URL")
    if env:
        return normalize_gemini_base(env)
    return DEFAULT_GEMINI_PROXY


def presets_for(cfg: dict) -> dict:
    if str(cfg.get("provider") or "") == "gemini" and str(cfg.get("base_url") or "").strip():
        proxy = normalize_gemini_base(str(cfg["base_url"]))
    else:
        proxy = gemini_proxy_base({"provider": ""})
    return {
        "gemini": {"model": MODELS["gemini"], "base_url": proxy, "label": LABELS["gemini"]},
        "deepseek": {"model": MODELS["deepseek"], "base_url": DEEPSEEK_BASE, "label": LABELS["deepseek"]},
    }
