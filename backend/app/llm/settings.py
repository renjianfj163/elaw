"""模型设置。密钥只进当前进程的环境变量，不写入配置文件。"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.endpoint import DEEPSEEK_BASE, MODELS, normalize_gemini_base, presets_for
from app.llm.provider import _config_path, load_llm_config
from app.llm.runtime_env import runtime_env

_EFFORT = {"low", "medium", "high"}
_PROVIDERS = {"deepseek", "gemini"}


class LlmSettingsIn(BaseModel):
    provider: Literal["deepseek", "gemini"] = "gemini"
    base_url: str = "http://108.160.140.173:8443"
    model: str = "gemini-2.5-pro"
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["low", "medium", "high"] = "high"
    max_tokens: int = Field(default=8192, ge=1, le=32768)
    response_format: Literal["json_object"] = "json_object"
    retries: int = Field(default=2, ge=0, le=5)
    cache: bool = True
    prompt_version: str = "2026-10-21"
    outbound_allowed: bool = False
    api_key: str = ""


class LlmSettingsError(Exception):
    pass


def public_settings() -> dict:
    cfg = _normalized(load_llm_config())
    env_name = "GEMINI_API_KEY" if cfg["provider"] == "gemini" else "DEEPSEEK_API_KEY"
    cfg["api_key_configured"] = bool(runtime_env(env_name))
    cfg["api_key_env"] = env_name
    cfg["keys_configured"] = {
        "gemini": bool(runtime_env("GEMINI_API_KEY")),
        "deepseek": bool(runtime_env("DEEPSEEK_API_KEY")),
    }
    cfg["sends_temperature"] = False if cfg["provider"] == "gemini" else cfg["thinking"] != "enabled"
    cfg["presets"] = presets_for(cfg)
    return cfg


def save_settings(body: LlmSettingsIn) -> dict:
    cfg = _normalized(body.model_dump())
    key = body.api_key.strip()
    if key:
        os.environ["GEMINI_API_KEY" if cfg["provider"] == "gemini" else "DEEPSEEK_API_KEY"] = key
    if cfg["provider"] == "gemini":
        os.environ["GEMINI_BASE_URL"] = cfg["base_url"]
    path = _config_path("llm.yaml")
    path.write_text(_render(cfg), encoding="utf-8")
    return public_settings()


def _normalized(raw: dict) -> dict:
    thinking = str(raw.get("thinking") or "enabled")
    if thinking not in {"enabled", "disabled"}:
        raise LlmSettingsError("思考模式只能是 enabled 或 disabled")
    effort = str(raw.get("reasoning_effort") or "high")
    if effort not in _EFFORT:
        raise LlmSettingsError("思考强度只能是 low、medium 或 high")
    provider = str(raw.get("provider") or "gemini")
    if provider not in _PROVIDERS:
        raise LlmSettingsError("模型只能是 Gemini 或 DeepSeek")
    model = MODELS[provider]
    if provider == "gemini":
        base_url = normalize_gemini_base(str(raw.get("base_url") or ""))
        if not base_url:
            base_url = presets_for({"provider": ""})["gemini"]["base_url"]
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise LlmSettingsError("代理地址需要以 http:// 或 https:// 开头")
    else:
        base_url = str(raw.get("base_url") or DEEPSEEK_BASE).strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise LlmSettingsError("接口地址需要以 https:// 开头")
    prompt_version = str(raw.get("prompt_version") or "").strip()
    if not prompt_version:
        raise LlmSettingsError("请填写 prompt 版本")
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "thinking": thinking,
        "reasoning_effort": effort,
        "max_tokens": int(raw.get("max_tokens") or 8192),
        "response_format": "json_object",
        "retries": int(raw.get("retries") if raw.get("retries") is not None else 2),
        "cache": bool(raw.get("cache", True)),
        "prompt_version": prompt_version,
        "outbound_allowed": bool(raw.get("outbound_allowed", False)),
    }


def _render(cfg: dict) -> str:
    cache = "true" if cfg["cache"] else "false"
    outbound = "true" if cfg["outbound_allowed"] else "false"
    return (
        "# 界面里可以选 Gemini 或 DeepSeek。密钥放在环境变量，不要写进这里。\n"
        "# Gemini 国内走代理，根地址不要带 /v1beta。思考模式为 enabled 时，请求不发送 temperature。\n"
        "llm:\n"
        f"  provider: {cfg['provider']}\n"
        f"  base_url: {cfg['base_url']}\n"
        f"  model: {cfg['model']}\n"
        f"  thinking: {cfg['thinking']}\n"
        f"  reasoning_effort: {cfg['reasoning_effort']}\n"
        f"  max_tokens: {cfg['max_tokens']}\n"
        "  response_format: json_object\n"
        f"  retries: {cfg['retries']}\n"
        f"  cache: {cache}\n"
        f"  prompt_version: \"{cfg['prompt_version']}\"\n"
        f"  outbound_allowed: {outbound}\n"
    )
