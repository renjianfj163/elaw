"""规划、裁剪，再交给现有的 review_schematic。

原理图可以一次上传多份，规划和审查共用这些 PDF。产品定义和 Datasheet 都可以不提供。
有 Datasheet 时，规划失败只保留电源、引脚和典型应用相关章节。
没有 Datasheet 时，规划失败退回通用规则，并标明计划未能生成。
数值比较仍由规则引擎完成。
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.hashing import sha256_text, stable_hash
from app.llm.provider import MemoryCache
from app.schematic.crop import crop_datasheet, split_basis, split_marked_documents
from app.schematic.evidence import claims_datasheet
from app.schematic.quotes import quote_matches
from app.schematic.planner import CircuitAnalysisPlan, plan_schematic
from app.schematic.review import (
    MAX_BASIS_CHARS,
    SchematicDraftItem,
    SchematicInputError,
    SchematicReviewResult,
    close_transport,
    combine_reviews,
    schematic_digest,
    schematic_paths,
    load_gemini_config,
    review_schematic,
)
from app.schematic.rules import load_rule_packs, render_rules, select_tags

PLAN_FAILED = "计划未能生成"
NO_RULES = "没有对上规则标签"
RULES_ONLY_NOTE = "本次审查仅基于通用电路规则库，未挂载产品定义与 Datasheet，器件特异性参数及引脚定义状态标记为「无法判定」。"
PRODUCT_ONLY_NOTE = "本次未提供 Datasheet。手册中的引脚定义和额定值，图上没有标出时标为「无法判定」。"
DATASHEET_ONLY_NOTE = "本次未提供产品定义。审查依据是已提供的 Datasheet 和通用规则。"
DATASHEET_UNUSED = "手册文本已送入，但没有一条结论引用了手册原文。已列出的条目只对照了通用审查规则。"


class PlannedReview(BaseModel):
    items: list[SchematicDraftItem] = Field(default_factory=list)
    rejected: list = Field(default_factory=list)
    result_hash: str
    reason: str = ""
    plan_note: str = ""
    identified_modules: list[str] = Field(default_factory=list)
    active_components: list[str] = Field(default_factory=list)
    rule_tags: list[str] = Field(default_factory=list)
    basis_note: str = ""
    coverage_note: str = ""


def review_planned(
    schematic_pdf_path: Path | str | list[Path],
    product_spec_text: str,
    datasheet_text: str,
    *,
    transport=None,
    cache: MemoryCache | None = None,
    config: dict | None = None,
    rules_path: Path | None = None,
) -> PlannedReview:
    paths = schematic_paths(schematic_pdf_path)
    product = product_spec_text.strip()
    datasheet = datasheet_text.strip()
    if len(product_spec_text) > MAX_BASIS_CHARS:
        raise SchematicInputError("产品定义文本过长。请只保留与本次电路相关的章节。")

    pdf_sha = schematic_digest(paths)
    cfg = dict(config or load_gemini_config())
    store = cache if cache is not None else MemoryCache()
    packs = load_rule_packs(rules_path)
    owns_transport = transport is None
    sender = transport if transport is not None else _default_transport(paths)
    try:
        plan = plan_schematic(
            sender,
            store,
            cfg,
            pdf_sha=pdf_sha,
            product_spec_text=product,
            packs=packs,
        )
        note, components, requested = _plan_view(plan)
        if plan is None and not datasheet:
            requested = select_tags([], packs)
            note = PLAN_FAILED
        elif plan is not None:
            requested = select_tags(requested, packs)
        rules, used_tags = render_rules(requested, packs)
        if plan is not None and not used_tags:
            note = NO_RULES
        if datasheet:
            cropped = crop_datasheet(datasheet, components, conservative=plan is None)
            if not cropped.strip():
                raise SchematicInputError("没有找到引脚、电源或典型应用相关章节。请在摘录里保留这些内容后再审查。")
        else:
            cropped = ""
        include_rules = bool(used_tags) and (plan is not None or not datasheet)
        manual, rule_text = split_basis(cropped, rules if include_rules else "")
        if not manual.strip() and not rule_text.strip() and not product:
            raise SchematicInputError("没有可引用的依据")
        reviewed = review_schematic(
            paths,
            product,
            manual,
            rules_text=rule_text,
            transport=sender,
            cache=store,
            config=cfg,
            coverage=_coverage(product, datasheet),
            roster=components if datasheet else None,
        )
        coverage_note = ""
        if datasheet and components:
            reviewed, coverage_note = _fill_datasheet_gaps(
                reviewed,
                paths,
                datasheet,
                components,
                sender,
                store,
                cfg,
                pdf_sha,
            )
        mounted = used_tags if include_rules else []
        return _assemble(
            reviewed,
            plan,
            note,
            mounted,
            pdf_sha,
            datasheet,
            _basis_note(product, datasheet, reviewed.items, manual),
            coverage_note,
        )
    finally:
        if owns_transport:
            close_transport(sender)


def _plan_view(plan: CircuitAnalysisPlan | None) -> tuple[str, list[str], list[str]]:
    if plan is None:
        return PLAN_FAILED, [], []
    return "", list(plan.active_components), list(plan.required_rule_tags)


def _coverage(product: str, datasheet: str) -> str:
    if product and datasheet:
        return "full"
    if product:
        return "product"
    if datasheet:
        return "datasheet"
    return "rules"


def _basis_note(product: str, datasheet: str, items: list | None = None, manual: str = "") -> str:
    quoted = bool(manual.strip()) and any(quote_matches(item.quote, manual) for item in items or [])
    if datasheet.strip() and manual.strip() and not quoted:
        return DATASHEET_UNUSED
    if product and datasheet:
        return ""
    if not product and not datasheet:
        return RULES_ONLY_NOTE
    if not datasheet:
        return PRODUCT_ONLY_NOTE
    return DATASHEET_ONLY_NOTE


def _assemble(
    reviewed: SchematicReviewResult,
    plan: CircuitAnalysisPlan | None,
    note: str,
    tags: list[str],
    pdf_sha: str,
    datasheet: str,
    basis_note: str,
    coverage_note: str = "",
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
            "basis_note": basis_note,
            "coverage_note": coverage_note,
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
        basis_note=basis_note,
        coverage_note=coverage_note,
    )


_COMPONENT = re.compile(
    r"^(?P<ref>[A-Za-z]{1,6}\d{1,5}[A-Za-z]?)\s*(?:[（(](?P<part>[^)）]+)[)）])?\s*$"
)
_PART_TOKEN = re.compile(r"[A-Za-z0-9]{4,}")
_PART_ONLY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{2,}")
_GAP_BATCH = 3
_GAP_EXCERPT = 32_000
_MAX_GAP_CALLS = 8


def _fill_datasheet_gaps(
    reviewed: SchematicReviewResult,
    paths: list[Path],
    datasheet: str,
    components: list[str],
    sender,
    store: MemoryCache,
    cfg: dict,
    pdf_sha: str,
) -> tuple[SchematicReviewResult, str]:
    covered = _covered_refdes(reviewed.items, reviewed.rejected)
    missing: list[str] = []
    pending: list[dict] = []
    for label in components:
        ref, part = _parse_component(label)
        if not ref:
            missing.append(label)
            continue
        if ref in covered:
            continue
        needles = _part_needles(part)
        if not needles or not _datasheet_has(datasheet, needles):
            missing.append(label)
            continue
        pending.append({"ref": ref, "label": label, "needles": needles})
    asked: list[dict] = []
    unexcerpted: list[str] = []
    calls = 0
    for start in range(0, len(pending), _GAP_BATCH):
        if calls >= _MAX_GAP_CALLS:
            break
        group = pending[start : start + _GAP_BATCH]
        blocks = []
        refs = []
        for item in group:
            excerpt = _excerpt_for_needles(datasheet, item["needles"], _GAP_EXCERPT)
            if not excerpt:
                unexcerpted.append(item["label"])
                continue
            blocks.append(excerpt)
            refs.append(item["ref"])
            asked.append(item)
        if not refs:
            continue
        manual = "\n\n".join(blocks)
        if len(manual) > MAX_BASIS_CHARS:
            manual = manual[:MAX_BASIS_CHARS]
        extra = review_schematic(
            paths,
            "",
            manual,
            rules_text="",
            transport=sender,
            cache=store,
            config=cfg,
            coverage="datasheet",
            only_refdes=refs,
        )
        kept_items, kept_rejected = _usable_gap(extra, manual)
        if kept_items or kept_rejected:
            reviewed = combine_reviews(
                reviewed,
                SchematicReviewResult(
                    items=kept_items,
                    rejected=kept_rejected,
                    result_hash=extra.result_hash,
                    reason=extra.reason,
                ),
                pdf_sha=pdf_sha,
                product="",
                datasheet=datasheet,
                cfg=cfg,
            )
        calls += 1
    covered = _covered_refdes(reviewed.items, reviewed.rejected)
    still = [item["label"] for item in asked if item["ref"] not in covered]
    handled = {item["label"] for item in asked} | set(unexcerpted)
    skipped = [item["label"] for item in pending if item["label"] not in handled]
    notes = []
    if missing:
        notes.append("这些器件的型号没有出现在已提供的手册文本里，所以没有手册检查项：" + "、".join(missing))
    if unexcerpted:
        notes.append("这些器件的型号在手册里，但没有抽出引脚、电源或典型应用章节：" + "、".join(unexcerpted))
    if skipped:
        notes.append("这些器件的型号在手册里，但这一轮补审名额已满，还没有逐个复查：" + "、".join(skipped))
    if still:
        notes.append("这些器件的型号在手册里，但补审后仍没有形成可引用的手册约束：" + "、".join(still))
    elif asked:
        notes.append("第一遍没有落到手册约束上的器件，已按型号分段补审。")
    return reviewed, " ".join(notes)


def _parse_component(label: str) -> tuple[str, str]:
    match = _COMPONENT.match(label.strip())
    if not match:
        return "", ""
    return match.group("ref"), (match.group("part") or "").strip()


def _part_needles(part: str) -> list[str]:
    found: list[str] = []
    for token in _PART_TOKEN.findall(part):
        if any(ch.isalpha() for ch in token) and any(ch.isdigit() for ch in token) and token not in found:
            found.append(token)
    return found


def _datasheet_has(text: str, needles: list[str]) -> bool:
    folded = text.lower()
    return any(needle.lower() in folded for needle in needles)


def _constraint_quote(quote: str, refdes: str) -> bool:
    text = quote.strip()
    if not text or text == refdes.strip():
        return False
    return _PART_ONLY.fullmatch(text) is None


def _covered_refdes(items, rejected) -> set[str]:
    covered: set[str] = set()
    for item in items:
        source = getattr(item, "requirement_source", "")
        quote = getattr(item, "quote", "")
        ref = getattr(item, "target_refdes", "")
        if claims_datasheet(source) and _constraint_quote(quote, ref):
            covered.add(ref.strip())
    for row in rejected:
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        source = str(payload.get("requirement_source") or "")
        quote = str(payload.get("quote") or "")
        ref = str(payload.get("target_refdes") or "")
        if claims_datasheet(source) and _constraint_quote(quote, ref):
            covered.add(ref.strip())
    return covered


def _excerpt_for_needles(datasheet: str, needles: list[str], limit: int) -> str:
    folded = [needle.lower() for needle in needles]
    docs = split_marked_documents(datasheet)
    chosen = [(title, body) for title, body in docs if any(needle in f"{title}\n{body}".lower() for needle in folded)]
    if not chosen:
        return ""
    share = max(limit // len(chosen), 1)
    parts: list[str] = []
    for title, body in chosen:
        piece = crop_datasheet(body, needles, limit=share)
        if not piece or not any(needle in piece.lower() for needle in folded):
            piece = _window_around(body, folded, share)
        piece = piece.strip()
        if not piece:
            continue
        parts.append(f"【{title}】\n{piece}" if title else piece)
    return "\n\n".join(parts).strip()[:limit]


def _window_around(text: str, needles: list[str], limit: int) -> str:
    folded = text.lower()
    indexes = [folded.find(needle) for needle in needles if needle in folded]
    if not indexes:
        return ""
    anchor = min(indexes)
    start = max(0, anchor - limit // 2)
    return text[start : start + limit].strip()


def _usable_gap(extra: SchematicReviewResult, manual: str) -> tuple[list, list]:
    items = []
    for item in extra.items:
        if item.admission == "needs_evidence" or not claims_datasheet(item.requirement_source):
            continue
        if not _constraint_quote(item.quote, item.target_refdes):
            continue
        items.append(item)
    rejected = []
    for row in extra.rejected:
        payload = row.payload if isinstance(row.payload, dict) else {}
        quote = str(payload.get("quote") or "")
        ref = str(payload.get("target_refdes") or "")
        if not quote or not quote_matches(quote, manual) or not _constraint_quote(quote, ref):
            continue
        rejected.append(row)
    return items, rejected


def _default_transport(paths: list[Path]):
    from app.schematic.gemini_client import gemini_transport

    return gemini_transport(paths)
