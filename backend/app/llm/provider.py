import json
import os
from pathlib import Path

import yaml
from pydantic import BaseModel

from app.db import repo_root
from app.hashing import stable_hash

UNDECIDED = {"verdict": "无法判定", "reason": "模型输出未通过校验"}


def load_llm_config() -> dict:
    return _load("llm.yaml")["llm"]


def load_local_config() -> dict:
    return _load("local.yaml")


def _load(name: str) -> dict:
    path = _config_path(name)
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _config_path(name: str) -> Path:
    env = os.environ.get("ELAW_CONFIG_DIR")
    root = Path(env) if env else repo_root() / "config"
    return root / name


def cache_key(model: str, prompt_version: str, payload: dict) -> str:
    return stable_hash({"model": model, "prompt_version": prompt_version, "input": payload})


def build_request(cfg: dict, prompt: str) -> dict:
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": "只输出一个 JSON 对象，不要输出解释。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": int(cfg.get("max_tokens") or 8192),
        "response_format": {"type": cfg.get("response_format") or "json_object"},
        "stream": False,
    }
    if str(cfg.get("thinking")) == "enabled":
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = cfg.get("reasoning_effort") or "high"
    else:
        body["temperature"] = 0
    return body


class MemoryCache:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value: dict) -> None:
        self.data[key] = value


def generate_json(schema: type[BaseModel], prompt: str, *, transport, cache: MemoryCache, config: dict | None = None):
    cfg = config or load_llm_config()
    payload = {"prompt": prompt, "schema": schema.__name__}
    key = cache_key(cfg["model"], str(cfg.get("prompt_version")), payload)
    if cfg.get("cache", True):
        hit = cache.get(key)
        if hit is not None:
            return {"output": hit, "cache_key": key, "cache_hit": True}
    body = build_request(cfg, prompt)
    attempts = int(cfg.get("retries", 2)) + 1
    for _ in range(attempts):
        try:
            content = transport(body)
            parsed = schema.model_validate(json.loads(content)).model_dump()
            if cfg.get("cache", True):
                cache.put(key, parsed)
            return {"output": parsed, "cache_key": key, "cache_hit": False}
        except Exception:
            continue
    if cfg.get("cache", True):
        cache.put(key, dict(UNDECIDED))
    return {"output": dict(UNDECIDED), "cache_key": key, "cache_hit": False}
