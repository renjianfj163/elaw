"""Gemini 文件上传与结构化生成。测试注入 transport，不走这里。"""

from __future__ import annotations

import time
from pathlib import Path

from app.llm.endpoint import gemini_proxy_base, normalize_gemini_base
from app.llm.provider import load_llm_config
from app.llm.runtime_env import runtime_env
from app.schematic.review import ProposalBatch

_ACTIVE = {"ACTIVE", "FileState.ACTIVE"}


class GeminiCallError(RuntimeError):
    pass


def gemini_transport(pdf_path: Path | list[Path]):
    if isinstance(pdf_path, (str, Path)):
        sources = [Path(pdf_path)]
    else:
        sources = [Path(item) for item in pdf_path]
    session = {"client": None, "uploaded": [], "closed": False}

    def close() -> None:
        if session["closed"]:
            return
        session["closed"] = True
        client = session.get("client")
        uploaded = list(session.get("uploaded") or [])
        session["uploaded"] = []
        if client is None:
            return
        for item in uploaded:
            name = getattr(item, "name", None)
            if not name:
                continue
            try:
                client.files.delete(name=name)
            except Exception:
                pass

    def send(body: dict) -> str:
        if session["closed"]:
            raise GeminiCallError("原理图上传已结束")
        if "temperature" in body:
            raise GeminiCallError("请求不能发送 temperature")
        cfg = load_llm_config()
        if str(cfg.get("provider") or "") not in {"", "gemini"}:
            raise GeminiCallError("原理图审查目前只接入了 Gemini。请在模型设置里选择 Gemini。")
        if not cfg.get("outbound_allowed"):
            raise GeminiCallError("尚未允许把原理图和依据发往接口")
        api_key = runtime_env("GEMINI_API_KEY")
        if not api_key:
            raise GeminiCallError("未配置 GEMINI_API_KEY")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GeminiCallError("未安装 google-genai。请在 backend 环境执行 pip install google-genai") from exc

        client = session["client"]
        if client is None:
            client = genai.Client(api_key=api_key, http_options=_http_options(types, cfg))
            session["client"] = client
        uploaded = session["uploaded"]
        if not uploaded:
            for source in sources:
                item = client.files.upload(file=str(source))
                session["uploaded"].append(item)
                ready = _wait_until_ready(client, item)
                session["uploaded"][-1] = ready
            uploaded = session["uploaded"]
        response = client.models.generate_content(
            model=body["model"],
            contents=[*uploaded, body["user"]],
            config=types.GenerateContentConfig(
                system_instruction=body["system"],
                response_mime_type="application/json",
                response_schema=_response_schema(str(body.get("schema") or "proposal")),
                max_output_tokens=int(body["max_output_tokens"]),
            ),
        )
        text = getattr(response, "text", None) or ""
        if not text.strip():
            raise GeminiCallError("接口没有返回 JSON")
        return text

    send.close = close
    return send


def ping_gemini(model: str, base_url: str, api_key: str) -> str:
    if not api_key.strip():
        raise GeminiCallError("未配置 GEMINI_API_KEY")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GeminiCallError("未安装 google-genai。请在 backend 环境执行 pip install google-genai") from exc
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(base_url=normalize_gemini_base(base_url), timeout=45_000),
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents='只输出 JSON 对象 {"ok": true}，不要输出其他文字。',
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=64,
            ),
        )
    except Exception as exc:
        raise GeminiCallError(_safe_error(exc, api_key)) from exc
    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise GeminiCallError("接口没有返回 JSON")
    return text


def _http_options(types, cfg: dict):
    base = gemini_proxy_base(cfg)
    if not base:
        return None
    return types.HttpOptions(base_url=base)


def _safe_error(exc: BaseException, api_key: str) -> str:
    text = str(exc).strip() or "接口没有返回可用的 JSON"
    if api_key:
        text = text.replace(api_key, "***")
    return text[:300]


def _response_schema(name: str):
    if name == "plan":
        from app.schematic.planner import CircuitAnalysisPlan

        return CircuitAnalysisPlan
    if name == "discover":
        from app.schematic.discovery import DiscoveryBatch

        return DiscoveryBatch
    return ProposalBatch


def _wait_until_ready(client, uploaded, attempts: int = 30):
    for _ in range(attempts):
        state = _state_name(uploaded)
        if state in _ACTIVE or not state:
            return uploaded
        if state == "FAILED":
            raise GeminiCallError("原理图 PDF 上传后未能处理")
        time.sleep(1)
        name = getattr(uploaded, "name", None)
        if not name:
            return uploaded
        uploaded = client.files.get(name=name)
    raise GeminiCallError("原理图 PDF 上传后长时间未能处理")


def _state_name(uploaded) -> str:
    state = getattr(uploaded, "state", None)
    if state is None:
        return ""
    name = getattr(state, "name", None)
    return str(name or state)
