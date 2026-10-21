"""模型设置。密钥只进当前进程的环境变量，不写入配置文件。"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from app.llm.provider import _config_path, load_llm_config

_EFFORT = {"low", "medium", "high"}


class LlmSettingsIn(BaseModel):
    provider: Literal["deepseek"] = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["low", "medium", "high"] = "high"
    max_tokens: int = Field(default=8192, ge=1, le=32768)
    response_format: Literal["json_object"] = "json_object"
    retries: int = Field(default=2, ge=0, le=5)
    cache: bool = True
    prompt_version: str = "2026-10-21"
    outbound_allowed: bool = True
    api_key: str = ""


class LlmSettingsError(Exception):
    pass


def public_settings() -> dict:
    cfg = _normalized(load_llm_config())
    cfg["api_key_configured"] = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    cfg["sends_temperature"] = cfg["thinking"] != "enabled"
    return cfg


def save_settings(body: LlmSettingsIn) -> dict:
    cfg = _normalized(body.model_dump())
    key = body.api_key.strip()
    if key:
        os.environ["DEEPSEEK_API_KEY"] = key
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
    provider = str(raw.get("provider") or "deepseek")
    if provider != "deepseek":
        raise LlmSettingsError("目前只预留 DeepSeek")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise LlmSettingsError("请填写模型名")
    base_url = str(raw.get("base_url") or "https://api.deepseek.com").strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise LlmSettingsError("接口地址需要以 https:// 开头")
    prompt_version = str(raw.get("prompt_version") or "").strip()
    if not prompt_version:
        raise LlmSettingsError("请填写 prompt 版本")
    return {
        "provider": "deepseek",
        "base_url": base_url,
        "model": model,
        "thinking": thinking,
        "reasoning_effort": effort,
        "max_tokens": int(raw.get("max_tokens") or 8192),
        "response_format": "json_object",
        "retries": int(raw.get("retries") if raw.get("retries") is not None else 2),
        "cache": bool(raw.get("cache", True)),
        "prompt_version": prompt_version,
        "outbound_allowed": bool(raw.get("outbound_allowed", True)),
    }


def _render(cfg: dict) -> str:
    cache = "true" if cfg["cache"] else "false"
    outbound = "true" if cfg["outbound_allowed"] else "false"
    return (
        "# 换模型只改这个文件。密钥放在环境变量 DEEPSEEK_API_KEY，不要写进这里。\n"
        "# 思考模式为 enabled 时，请求不发送 temperature。\n"
        "llm:\n"
        "  provider: deepseek\n"
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
