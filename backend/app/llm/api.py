"""模型接口设置。"""

import json
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from app.llm.deepseek import deepseek_transport
from app.llm.provider import MemoryCache, generate_json
from app.llm.runtime_env import runtime_env
from app.llm.settings import LlmSettingsError, LlmSettingsIn, public_settings, save_settings
from app.schematic.gemini_client import GeminiCallError, ping_gemini

router = APIRouter(prefix="/api/llm", tags=["llm"])


class PingOut(BaseModel):
    ok: bool


@router.get("/settings")
def read_settings():
    return public_settings()


@router.put("/settings")
def write_settings(body: LlmSettingsIn):
    from app.public_trial import enabled

    if enabled():
        raise HTTPException(status_code=403, detail="在线试用的模型已经配好，不能在这里修改。")
    try:
        return save_settings(body)
    except (LlmSettingsError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ping")
async def ping(request: Request):
    from app.public_trial import enabled

    raw = await request.body()
    if enabled():
        cfg = public_settings()
    elif raw.strip():
        try:
            body = LlmSettingsIn.model_validate_json(raw)
            cfg = save_settings(body)
        except (LlmSettingsError, ValidationError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        cfg = public_settings()
    if not cfg["outbound_allowed"]:
        raise HTTPException(status_code=400, detail="尚未允许把内容发往接口。")
    if cfg["provider"] == "gemini":
        return _ping_gemini(cfg)
    if not cfg["api_key_configured"]:
        raise HTTPException(status_code=400, detail="未配置 DEEPSEEK_API_KEY。请在 API 密钥框填写后再测试。")
    seen: list[BaseException] = []

    def transport(payload: dict) -> str:
        try:
            return deepseek_transport(timeout=45)(payload)
        except Exception as exc:
            seen.append(exc)
            raise

    probe = dict(cfg)
    probe["retries"] = 0
    probe["cache"] = False
    result = generate_json(
        PingOut,
        "不要引用任何报告。只输出一个 JSON 对象：{\"ok\": true}",
        transport=transport,
        cache=MemoryCache(),
        config=probe,
    )
    output = result["output"]
    if output.get("ok") is not True:
        detail = _ping_detail(seen[-1]) if seen else (output.get("reason") or "接口没有返回可用的 JSON")
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "cache_hit": result["cache_hit"], "model": cfg["model"]}


def _ping_gemini(cfg: dict) -> dict:
    if not cfg["api_key_configured"]:
        raise HTTPException(status_code=400, detail="未配置 GEMINI_API_KEY。请在 API 密钥框填写后再测试。")
    key = runtime_env("GEMINI_API_KEY")
    try:
        text = ping_gemini(cfg["model"], cfg["base_url"], key)
        output = PingOut.model_validate_json(_json_object(text)).model_dump()
    except GeminiCallError as exc:
        raise HTTPException(status_code=400, detail=_redact(str(exc), key)) from exc
    except (ValidationError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=_redact("接口没有返回可用的 JSON", key)) from exc
    if output.get("ok") is not True:
        raise HTTPException(status_code=400, detail="接口没有返回可用的 JSON")
    return {"ok": True, "cache_hit": False, "model": cfg["model"]}


def _json_object(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [line for line in cleaned.splitlines() if not line.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return cleaned


def _redact(text: str, key: str) -> str:
    if key:
        text = text.replace(key, "***")
    return text[:300]


def _ping_detail(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        snippet = " ".join(exc.response.text.split())[:240]
        text = f"接口返回 {exc.response.status_code}"
        if snippet:
            text += f"：{snippet}"
    elif isinstance(exc, httpx.TimeoutException):
        text = "接口超时，没有在限定时间内返回。"
    elif isinstance(exc, httpx.RequestError):
        text = "连不上接口地址，请确认代理或接口从这台电脑能访问。"
    else:
        text = str(exc).strip() or "接口没有返回可用的 JSON"
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        text = text.replace(key, "***")
    return text[:300]
