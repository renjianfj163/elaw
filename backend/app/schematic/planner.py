"""规划器：只看原理图和产品定义，输出模块、器件和规则标签。

不判断符合或不符合。标签只能来自闭合集合，对不上的标签会被丢掉。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.llm.provider import MemoryCache, cache_key
from app.schematic.review import PROMPT_VERSION

RuleTag = Literal[
    "power_supply",
    "high_speed_digital",
    "component_derating",
    "dft_hygiene",
    "rf_bias",
]
RULE_TAGS: tuple[RuleTag, ...] = (
    "power_supply",
    "high_speed_digital",
    "component_derating",
    "dft_hygiene",
    "rf_bias",
)
RULE_TAG_SET = set(RULE_TAGS)

PLANNER_SYSTEM = """你负责浏览原理图并给出检索计划。不要做审查，不要判断符合或不符合。
规则标签只能从用户给出的列表里选择，不能自造。
看不清的器件和模块不要编造。
只输出一个 JSON 对象，不要输出解释。"""


class CircuitAnalysisPlan(BaseModel):
    identified_modules: list[str] = Field(default_factory=list, description="图纸上的电路模块")
    active_components: list[str] = Field(default_factory=list, description="位号及型号，如 U2(TPS7A4700)")
    required_rule_tags: list[RuleTag] = Field(default_factory=list, description="需要召回的规则标签")


def parse_plan(content: str) -> CircuitAnalysisPlan:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("模型输出不是 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    try:
        return CircuitAnalysisPlan(
            identified_modules=_strings(data.get("identified_modules"), "identified_modules"),
            active_components=_strings(data.get("active_components"), "active_components"),
            required_rule_tags=_tags(data.get("required_rule_tags")),
        )
    except ValidationError as exc:
        raise ValueError("模型输出未通过校验") from exc


def planner_prompt(product_spec_text: str, packs: list[dict]) -> str:
    lines = [f"- {pack['tag']}：{pack['hint']}" for pack in packs]
    catalog = "\n".join(lines) if lines else "（当前没有可召回的规则标签）"
    product = product_spec_text.strip()
    if product:
        opening = (
            "请浏览本次上传的全部原理图，结合产品定义，只输出检索计划。\n"
            f"【产品定义】\n{product}\n\n"
        )
    else:
        opening = (
            "请浏览本次上传的全部原理图，只输出检索计划。本次没有产品定义。\n"
            "可以根据图纸上的料号和网络名判断器件类型，并且只用来选择下面的规则标签。"
            "不要把这个判断写成审查结论。\n\n"
        )
    return (
        opening
        + "【可选规则标签】只能从下面选择，不能自造：\n"
        + f"{catalog}\n\n"
        + "任务：\n"
        + "1. 识别图纸上的主要电路模块。\n"
        + "2. 列出图纸上实际画出的关键器件，写成位号（型号）。\n"
        + "3. 选择需要召回的规则标签。电源、信号接口、器件降额和可测试性是通用项，图纸上有对应电路就应选上。射频只在图纸上有射频链路时选择。\n"
        + "不要做具体审查，不要判断符合或不符合。看不清的器件不要编造。"
    )


def plan_schematic(
    transport,
    cache: MemoryCache,
    cfg: dict,
    *,
    pdf_sha: str,
    product_spec_text: str,
    packs: list[dict],
) -> CircuitAnalysisPlan | None:
    model = str(cfg.get("planner_model") or "gemini-3.5-flash")
    prompt_version = f"{cfg.get('prompt_version') or PROMPT_VERSION}#plan"
    payload = {
        "pdf_sha256": pdf_sha,
        "product": product_spec_text,
        "tags": [pack["tag"] for pack in packs],
    }
    key = cache_key(model, prompt_version, payload)
    if cfg.get("cache", True):
        hit = cache.get(key)
        if hit is not None:
            if hit.get("verdict") == "无法判定":
                return None
            return CircuitAnalysisPlan.model_validate(hit)
    body = {
        "model": model,
        "system": PLANNER_SYSTEM,
        "user": planner_prompt(product_spec_text, packs),
        "max_output_tokens": int(cfg.get("max_output_tokens") or 65536),
        "schema": "plan",
    }
    attempts = int(cfg.get("retries", 2)) + 1
    for _ in range(attempts):
        try:
            plan = parse_plan(transport(body))
        except Exception:
            continue
        dumped = plan.model_dump()
        if cfg.get("cache", True):
            cache.put(key, dumped)
        return plan
    if cfg.get("cache", True):
        cache.put(key, {"verdict": "无法判定", "reason": "计划未能生成"})
    return None


def _strings(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} 不是数组")
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in found:
            found.append(text)
        if len(found) >= 400:
            break
    return found


def _tags(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("规则标签不是数组")
    found: list[str] = []
    for item in value:
        if isinstance(item, str) and item in RULE_TAG_SET and item not in found:
            found.append(item)
    return found
