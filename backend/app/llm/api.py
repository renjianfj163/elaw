"""模型接口设置。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from app.llm.deepseek import deepseek_transport
from app.llm.provider import MemoryCache, generate_json
from app.llm.settings import LlmSettingsError, LlmSettingsIn, public_settings, save_settings

router = APIRouter(prefix="/api/llm", tags=["llm"])


class PingOut(BaseModel):
    ok: bool


@router.get("/settings")
def read_settings():
    return public_settings()


@router.put("/settings")
def write_settings(body: LlmSettingsIn):
    try:
        return save_settings(body)
    except (LlmSettingsError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ping")
def ping():
    cfg = public_settings()
    if not cfg["outbound_allowed"]:
        raise HTTPException(status_code=400, detail="尚未允许把内容发往接口。")
    if not cfg["api_key_configured"]:
        raise HTTPException(status_code=400, detail="未配置 DEEPSEEK_API_KEY")
    result = generate_json(
        PingOut,
        "不要引用任何报告。只输出一个 JSON 对象：{\"ok\": true}",
        transport=deepseek_transport(),
        cache=MemoryCache(),
    )
    output = result["output"]
    if output.get("ok") is not True and output.get("verdict") == "无法判定":
        raise HTTPException(status_code=400, detail=output.get("reason") or "接口没有返回可用的 JSON")
    return {"ok": bool(output.get("ok")), "cache_hit": result["cache_hit"], "model": cfg["model"]}
