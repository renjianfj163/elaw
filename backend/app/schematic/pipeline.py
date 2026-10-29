"""规划、裁剪，再交给现有的 review_schematic。

原理图上传一次，规划和审查共用。规划失败时只保留电源、引脚和典型应用相关章节，
并标明计划未能生成。数值比较仍由规则引擎完成。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.hashing import sha256_bytes, sha256_text, stable_hash
from app.llm.provider import MemoryCache
from app.schematic.crop import bind_basis, crop_datasheet
from app.schematic.planner import CircuitAnalysisPlan, plan_schematic
from app.schematic.review import (
    MAX_BASIS_CHARS,
    SchematicDraftItem,
    SchematicInputError,
    SchematicReviewResult,
    check_schematic_file,
    close_transport,
    load_gemini_config,
    review_schematic,
)
from app.schematic.rules import load_rule_packs, render_rules, select_tags

PLAN_FAILED = "计划未能生成"
NO_RULES = "没有对上规则标签"


class PlannedReview(BaseModel):
    items: list[SchematicDraftItem] = Field(default_factory=list)
    rejected: list = Field(default_factory=list)
    result_hash: str
    reason: str = ""
    plan_note: str = ""
    identified_modules: list[str] = Field(default_factory=list)
    active_components: list[str] = Field(default_factory=list)
    rule_tags: list[str] = Field(default_factory=list)


def review_planned(
    schematic_pdf_path: Path | str,
    product_spec_text: str,
    datasheet_text: str,
    *,
    transport=None,
    cache: MemoryCache | None = None,
    config: dict | None = None,
    rules_path: Path | None = None,
) -> PlannedReview:
    path = Path(schematic_pdf_path)
    check_schematic_file(path)
    product = product_spec_text.strip()
    datasheet = datasheet_text.strip()
    if not product:
        raise SchematicInputError("请提供产品定义文本")
    if len(product_spec_text) > MAX_BASIS_CHARS:
        raise SchematicInputError("产品定义文本过长。请只保留与本次电路相关的章节。")
    if not datasheet:
        raise SchematicInputError("请提供 Datasheet 文本")

    pdf_sha = sha256_bytes(path.read_bytes())
    cfg = dict(config or load_gemini_config())
    store = cache if cache is not None else MemoryCache()
    packs = load_rule_packs(rules_path)
    owns_transport = transport is None
    sender = transport if transport is not None else _default_transport(path)
    try:
        plan = plan_schematic(
            sender,
            store,
            cfg,
            pdf_sha=pdf_sha,
            product_spec_text=product,
            packs=packs,
        )
        note, components, tags = _plan_view(plan)
        if plan is not None:
            tags = select_tags(tags, packs)
        rules, used_tags = render_rules(tags, packs)
        if plan is not None and not used_tags:
            note = NO_RULES
        cropped = crop_datasheet(datasheet, components, conservative=plan is None)
        if not cropped.strip():
            raise SchematicInputError("没有找到引脚、电源或典型应用相关章节。请在摘录里保留这些内容后再审查。")
        basis = bind_basis(cropped, rules if plan is not None else "")
        reviewed = review_schematic(
            path,
            product,
            basis,
            transport=sender,
            cache=store,
            config=cfg,
        )
        return _assemble(reviewed, plan, note, used_tags if plan is not None else [], pdf_sha, datasheet)
    finally:
        if owns_transport:
            close_transport(sender)


def _plan_view(plan: CircuitAnalysisPlan | None) -> tuple[str, list[str], list[str]]:
    if plan is None:
        return PLAN_FAILED, [], []
    return "", list(plan.active_components), list(plan.required_rule_tags)


def _assemble(
    reviewed: SchematicReviewResult,
    plan: CircuitAnalysisPlan | None,
    note: str,
    tags: list[str],
    pdf_sha: str,
    datasheet: str,
) -> PlannedReview:
    modules = list(plan.identified_modules) if plan is not None else []
    components = list(plan.active_components) if plan is not None else []
    digest = stable_hash(
        {
            "review": reviewed.result_hash,
            "plan": plan.model_dump() if plan is not None else None,
            "plan_note": note,
            "rule_tags": tags,
            "pdf_sha256": pdf_sha,
            "datasheet_sha256": sha256_text(datasheet),
        }
    )
    return PlannedReview(
        items=reviewed.items,
        rejected=[item.model_dump() for item in reviewed.rejected],
        result_hash=digest,
        reason=reviewed.reason,
        plan_note=note,
        identified_modules=modules,
        active_components=components,
        rule_tags=tags,
    )


def _default_transport(path: Path):
    from app.schematic.gemini_client import gemini_transport

    return gemini_transport(path)
