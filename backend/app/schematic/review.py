"""用 Gemini 阅读原理图 PDF，对照产品定义和已裁剪的 Datasheet，起草检查项。

整本 Datasheet 由规划流程先裁剪，再进入这里。直接调用时仍只接收已截取的章节。
数值是否满足依据由规则引擎计算。模型给出的数值结论不采用。
定性项用同一模型复核第二遍，两遍不一致则为无法判定，并标明同源双审。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.schematic.expr import _number_in_clause, _numbers, _structure_ok
from app.hashing import sha256_bytes, sha256_text, stable_hash
from app.llm.provider import MemoryCache, cache_key
from app.rules.evaluator import evaluate
from app.schematic.evidence import (
    Admission,
    CalcInput,
    DerivedCalculation,
    ReviewEvidence,
    claims_datasheet,
    evidence_list,
    quote_homes,
    source_label,
    unverified_calculation,
    verify_derived,
)
from app.schematic.quotes import quote_matches

PROMPT_VERSION = "schematic-3"
MAX_BASIS_CHARS = 600_000
MAX_PDF_BYTES = 40 * 1024 * 1024
MAX_SCHEMATIC_FILES = 120
MAX_TOTAL_PDF_BYTES = 500 * 1024 * 1024
MAX_OUTPUT_TOKENS = 65_536
_VERDICTS = {"true": "符合", "false": "不符合", "unknown": "无法判定"}

SYSTEM_INSTRUCTION = """你是一位资深的硬件电路设计审核专家。你的任务是根据提供的产品定义规范和元器件 Datasheet 文本，审查输入的电路原理图 PDF。

必须遵守：
- 只根据图纸上真实可见的连线、符号和标号下结论。不要因为 Datasheet 推荐了某个电路，就认为图纸上也画了。
- findings 必须写明图纸上对应的位号或网络名，并且包含 target_refdes 本身。
- 忽略图纸上未出现的器件，不要为它们生成审查项。
- 依据里写了、图纸上没有画出的模块或引脚，conclusion 使用「不适用」，不要把它们当成遗漏。
- 若提供了【Datasheet 依据】，其中能对上图纸位号的绝对最大额定值、推荐工作条件、引脚功能和典型应用要求，也要各自形成检查项。这些项的 quote 用该段原文，requirement_source 写 Datasheet。不要用审查规则的句子代替手册原文。
- 文末审查规则要逐条形成检查项。这些项的 quote 用规则原文，requirement_source 写审查规则。图纸上没有对应电路时，conclusion 使用「不适用」。
- 审查规则里的电流、容值、电阻、耐压和功率是数值比较。check_type 用 numeric，conclusion 写「无法判定」。原文中的数字写入 limit_value。若限值要由原文里的几个数字算出，填写 calc_op 和 calc_inputs，不要把计算结果写进 limit_value。手册没有写出的额定值不要补。
- 线条遮挡、符号重叠或无法看清连接时，conclusion 使用「无法判定」。
- quote 必须是本次依据中某一段的连续原文，不要改写，不要补字。requirement_source 必须和这段出处一致。
- check_type 为 numeric 时，不要自行判断符合或不符合，conclusion 固定写「无法判定」。把从图纸读到的数放进 observed（value 与 unit）。把依据中的比较写成 compare_op、limit_value、limit_unit。区间比较时 compare_op 用 between，并填写 limit_high。param_name 用简短英文参数名。这些数字必须原样出现在依据原文里。不要输出 requirement 对象。
- 其他 check_type 的 conclusion 只能是：符合、不符合、无法判定、不适用。
- status 固定为 draft。
- 只输出一个 JSON 对象，不要输出解释。对象只有 items 数组。"""


class SchematicInputError(Exception):
    pass


class ObservedReading(BaseModel):
    value: float
    unit: str = ""


class SchematicProposal(BaseModel):
    target_refdes: str = Field(min_length=1)
    check_type: Literal["numeric", "presence", "connectivity", "qualitative"]
    requirement_source: str = Field(min_length=1)
    findings: str = Field(min_length=1)
    conclusion: Literal["符合", "不符合", "无法判定", "不适用"]
    status: str = "draft"
    quote: str = Field(min_length=1)
    observed: ObservedReading | None = None
    compare_op: Literal["==", "!=", "<", "<=", ">", ">=", "between"] | None = None
    limit_value: float | None = None
    limit_unit: str = ""
    limit_high: float | None = None
    param_name: str = ""
    part_number: str = ""
    calc_op: Literal["divide", "multiply", "percent_divide"] | None = None
    calc_inputs: list[CalcInput] = Field(default_factory=list)


class ProposalBatch(BaseModel):
    items: list[SchematicProposal] = Field(default_factory=list)


class SchematicDraftItem(BaseModel):
    target_refdes: str
    check_type: Literal["numeric", "presence", "connectivity", "qualitative"]
    requirement_source: str
    findings: str
    conclusion: Literal["符合", "不符合", "无法判定", "不适用"]
    status: Literal["draft"] = "draft"
    quote: str
    method: str
    dual_review: str = ""
    needs_review: bool = True
    review_note: str = ""
    reason: str = ""
    trace: list = Field(default_factory=list)
    admission: Admission = "accepted"
    evidence: list[ReviewEvidence] = Field(default_factory=list)
    calculation: DerivedCalculation | None = None


class RejectedSchematicItem(BaseModel):
    reason: str
    target_refdes: str = ""
    payload: dict = Field(default_factory=dict)


class SchematicReviewResult(BaseModel):
    items: list[SchematicDraftItem] = Field(default_factory=list)
    rejected: list[RejectedSchematicItem] = Field(default_factory=list)
    result_hash: str
    reason: str = ""


def load_gemini_config() -> dict:
    from app.llm.provider import _config_path

    path = _config_path("gemini.yaml")
    raw = {}
    if path.is_file():
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = loaded.get("gemini") or {}
    return {
        "model": str(raw.get("model") or "gemini-3.5-flash"),
        "planner_model": str(raw.get("planner_model") or "gemini-3.5-flash"),
        "prompt_version": str(raw.get("prompt_version") or PROMPT_VERSION),
        "retries": int(raw.get("retries") if raw.get("retries") is not None else 2),
        "cache": bool(raw.get("cache", True)),
        "max_output_tokens": int(raw.get("max_output_tokens") or MAX_OUTPUT_TOKENS),
    }


def review_schematic(
    schematic_pdf_path: Path | str | list[Path],
    product_spec_text: str,
    datasheet_text: str,
    *,
    rules_text: str = "",
    transport=None,
    cache: MemoryCache | None = None,
    config: dict | None = None,
    coverage: str = "full",
    roster: list[str] | None = None,
    only_refdes: list[str] | None = None,
) -> SchematicReviewResult:
    paths = schematic_paths(schematic_pdf_path)
    _check_inputs(paths, product_spec_text, datasheet_text, rules_text)
    pdf_sha = schematic_digest(paths)
    cfg = dict(config or load_gemini_config())
    store = cache if cache is not None else MemoryCache()
    owns_transport = transport is None
    sender = transport if transport is not None else _default_transport(paths)
    try:
        return _review_with_sender(
            paths,
            product_spec_text,
            datasheet_text,
            sender,
            store,
            cfg,
            pdf_sha,
            coverage,
            rules_text,
            roster,
            only_refdes,
        )
    finally:
        if owns_transport:
            close_transport(sender)


def _review_with_sender(
    path,
    product_spec_text,
    datasheet_text,
    sender,
    store,
    cfg,
    pdf_sha,
    coverage,
    rules_text="",
    roster: list[str] | None = None,
    only_refdes: list[str] | None = None,
):
    basis = "\n".join(part for part in (product_spec_text, datasheet_text, rules_text) if part.strip())
    stage = "datasheet-gap" if only_refdes else "primary"
    primary, primary_raw, primary_reason = _generate_batch(
        sender,
        store,
        cfg,
        pdf_sha=pdf_sha,
        stage=stage,
        system=_system(coverage),
        user=_user_prompt(
            product_spec_text,
            datasheet_text,
            rules_text,
            coverage=coverage,
            roster=roster,
            only_refdes=only_refdes,
        ),
        targets=None,
    )
    if primary is None:
        return _finish([], [], pdf_sha, product_spec_text, datasheet_text, cfg, [primary_raw], primary_reason)

    drafts: list[SchematicDraftItem] = []
    rejected: list[RejectedSchematicItem] = []
    qualitative: list[tuple[SchematicProposal, SchematicDraftItem]] = []
    for proposal in primary.items:
        item, reject = _accept(proposal, basis, product_spec_text, datasheet_text, rules_text)
        if reject is not None:
            rejected.append(reject)
        if item is None:
            continue
        if item.admission == "needs_evidence":
            drafts.append(item)
            continue
        if proposal.check_type == "qualitative":
            qualitative.append((proposal, item))
            continue
        drafts.append(item)

    second_raw = None
    if qualitative:
        targets = [_target_view(proposal, kept.requirement_source) for proposal, kept in qualitative]
        second, second_raw, second_reason = _generate_batch(
            sender,
            store,
            cfg,
            pdf_sha=pdf_sha,
            stage="qualitative-second",
            system=_system(coverage) + "\n这是第二遍复核。不要参考、复述或猜测任何先前结论。",
            user=_second_prompt(
                product_spec_text,
                datasheet_text,
                targets,
                rules_text,
                coverage=coverage,
                only_refdes=only_refdes,
            ),
            targets=targets,
        )
        by_key: dict[tuple, list[str]] = {}
        if second is not None:
            for proposal in second.items:
                by_key.setdefault(_identity(proposal), []).append(proposal.conclusion)
        for proposal, kept in qualitative:
            first = proposal.conclusion
            queue = by_key.get(_identity(proposal), [])
            second_conclusion = queue.pop(0) if queue else ""
            if second is None or not second_conclusion or second_conclusion != first:
                conclusion = "无法判定"
                reason = second_reason or "两遍结论不一致"
            else:
                conclusion = first
                reason = ""
            drafts.append(
                _draft_from(
                    proposal,
                    conclusion=conclusion,
                    method="model",
                    dual_review="同源双审",
                    review_note="同源双审，建议人工复核",
                    reason=reason,
                    requirement_source=kept.requirement_source,
                    evidence=kept.evidence,
                )
            )

    passes = [primary_raw]
    if second_raw is not None:
        passes.append(second_raw)
    return _finish(drafts, rejected, pdf_sha, product_spec_text, datasheet_text, cfg, passes, "")


def close_transport(sender) -> None:
    closer = getattr(sender, "close", None)
    if callable(closer):
        closer()


def _default_transport(paths: list[Path]):
    from app.schematic.gemini_client import gemini_transport

    return gemini_transport(paths)


def check_schematic_file(path: Path) -> None:
    if path.suffix.lower() != ".pdf":
        raise SchematicInputError("原理图需要是 PDF")
    if not path.is_file():
        raise SchematicInputError("找不到原理图 PDF")
    size = path.stat().st_size
    if size <= 0:
        raise SchematicInputError("原理图 PDF 是空的")
    if size > MAX_PDF_BYTES:
        raise SchematicInputError("原理图 PDF 超过 40MB")


def schematic_paths(value: Path | str | list[Path] | tuple[Path, ...]) -> list[Path]:
    if isinstance(value, (str, Path)):
        paths = [Path(value)]
    else:
        paths = [Path(item) for item in value]
    if not paths:
        raise SchematicInputError("请上传原理图 PDF")
    if len(paths) > MAX_SCHEMATIC_FILES:
        raise SchematicInputError(f"原理图一次最多 {MAX_SCHEMATIC_FILES} 份")
    total = 0
    for path in paths:
        check_schematic_file(path)
        total += path.stat().st_size
    if total > MAX_TOTAL_PDF_BYTES:
        raise SchematicInputError("原理图合计超过 500MB")
    return paths


def schematic_digest(paths: list[Path]) -> str:
    parts = [sha256_bytes(path.read_bytes()) for path in paths]
    if len(parts) == 1:
        return parts[0]
    return sha256_text("\n".join(parts))


def _check_inputs(paths: list[Path], product_spec_text: str, datasheet_text: str, rules_text: str = "") -> None:
    schematic_paths(paths)
    if not product_spec_text.strip() and not datasheet_text.strip() and not rules_text.strip():
        raise SchematicInputError("没有可引用的依据")
    if (
        len(product_spec_text) > MAX_BASIS_CHARS
        or len(datasheet_text) > MAX_BASIS_CHARS
        or len(rules_text) > MAX_BASIS_CHARS
    ):
        raise SchematicInputError(
            "依据文本过长。请只保留与图上器件相关的章节，例如引脚配置、典型应用电路、绝对最大额定值，不要送入整本 Datasheet。"
        )


def _system(coverage: str) -> str:
    extra = _COVERAGE_NOTE.get(coverage)
    if not extra:
        return SYSTEM_INSTRUCTION
    return f"{SYSTEM_INSTRUCTION}\n{extra}"


def _user_prompt(
    product_spec_text: str,
    datasheet_text: str,
    rules_text: str = "",
    *,
    coverage: str = "full",
    roster: list[str] | None = None,
    only_refdes: list[str] | None = None,
) -> str:
    if only_refdes:
        return _scoped_prompt(
            product_spec_text,
            datasheet_text,
            coverage,
            rules_text=rules_text,
            only_refdes=only_refdes,
        )
    if coverage == "full":
        blocks = _basis_blocks(product_spec_text, datasheet_text, rules_text)
        task = (
            "【审查要求】\n"
            "识别本次上传的全部原理图 PDF 上出现的元器件位号和网络标签。"
            "对照依据，并逐条核对审查规则。"
            "依据或规则里写了、图纸上没有的电路，结论使用「不适用」，不要当成遗漏。"
            "审查规则里的电流、容值、电阻、耐压和功率用 numeric，conclusion 写「无法判定」。"
            f"{_datasheet_task(datasheet_text)}"
            f"{_roster_task(roster)}"
            "忽略图纸上未出现的器件。"
            "无法确定的连接标为无法判定。"
            '只输出 {"items": [...]}。'
        )
        return "\n\n".join([*blocks, task])
    return _scoped_prompt(
        product_spec_text,
        datasheet_text,
        coverage,
        rules_text=rules_text,
        roster=roster,
    )


def _second_prompt(
    product_spec_text: str,
    datasheet_text: str,
    targets: list[dict],
    rules_text: str = "",
    *,
    coverage: str = "full",
    only_refdes: list[str] | None = None,
) -> str:
    listed = json.dumps(targets, ensure_ascii=False)
    if only_refdes or coverage != "full":
        return _scoped_prompt(
            product_spec_text,
            datasheet_text,
            coverage,
            rules_text=rules_text,
            second=listed,
            only_refdes=only_refdes,
        )
    blocks = _basis_blocks(product_spec_text, datasheet_text, rules_text)
    task = (
        "【第二遍复核】\n"
        "下面这些定性项需要重新判断。这里没有上一遍的发现或结论，请只根据图纸和依据填写。\n"
        "依据里写了但图纸上没有的模块，结论使用「不适用」。\n"
        f"{_datasheet_task(datasheet_text)}"
        f"{listed}\n"
        '只输出 {"items": [...]}。每一项的 target_refdes、quote、check_type 保持与上面一致。'
    )
    return "\n\n".join([*blocks, task])


def _basis_blocks(product_spec_text: str, datasheet_text: str, rules_text: str) -> list[str]:
    blocks: list[str] = []
    if product_spec_text.strip():
        blocks.append(f"【产品定义依据】\n{product_spec_text.strip()}")
    if datasheet_text.strip():
        blocks.append(f"【Datasheet 依据】\n{datasheet_text.strip()}")
    if rules_text.strip():
        blocks.append(f"【审查规则】\n{rules_text.strip()}")
    return blocks


def _datasheet_task(datasheet_text: str) -> str:
    if not datasheet_text.strip():
        return ""
    return (
        "手册里能对上图纸位号的绝对最大额定值、推荐工作条件、引脚功能和典型应用要求，必须各自形成检查项。"
        "这些项的 quote 只能是【Datasheet 依据】里的连续原文，requirement_source 写 Datasheet。"
        "不要用审查规则的句子代替手册原文。"
        "只核对型号字符串是否出现，不算完成，不要用型号本身当 quote。"
        "手册没有写出的额定值不要编造。需要由原文中的数字算出限值时，填写 calc_op 与 calc_inputs，不要把计算结果当作原文。"
    )


def _roster_task(roster: list[str] | None) -> str:
    rows = [row.strip() for row in roster or [] if row and row.strip()]
    if not rows:
        return ""
    listed = "\n".join(f"- {row}" for row in rows)
    return (
        "【规划已识别的关键器件】\n"
        f"{listed}\n"
        "上面每一个型号，只要手册里出现了，就要各自形成 Datasheet 检查项。"
        "不要只挑一两个电源电压就结束。"
    )


def _only_task(refdes: list[str] | None) -> str:
    rows = [row.strip() for row in refdes or [] if row and row.strip()]
    if not rows:
        return ""
    return (
        "【手册补审】\n"
        f"这一遍只为这些位号生成 Datasheet 检查项：{'、'.join(rows)}。"
        "不要输出审查规则项，不要用型号字符串本身当 quote。"
        "每个位号至少一条，内容限于绝对最大额定值、推荐工作条件、引脚功能或典型应用，quote 必须是手册连续原文。"
    )


def _scoped_prompt(
    product_spec_text: str,
    datasheet_text: str,
    coverage: str,
    *,
    rules_text: str = "",
    second: str = "",
    roster: list[str] | None = None,
    only_refdes: list[str] | None = None,
) -> str:
    blocks = _basis_blocks(product_spec_text, datasheet_text, rules_text)
    guide = _COVERAGE_NOTE.get(coverage) or _COVERAGE_NOTE["rules"]
    if only_refdes and not second:
        task = (
            f"{_only_task(only_refdes)}"
            f"{_datasheet_task(datasheet_text)}"
            "忽略没有点名的位号。"
            "无法确定的连接标为无法判定。"
            '只输出 {"items": [...]}。'
        )
        return "\n\n".join([*blocks, task])
    if second:
        task = (
            "【第二遍复核】\n"
            "下面这些定性项需要重新判断。这里没有上一遍的发现或结论，请只根据图纸和依据填写。\n"
            f"{'' if only_refdes else guide}\n"
            f"{_only_task(only_refdes)}"
            f"{_datasheet_task(datasheet_text)}"
            f"{second}\n"
            '只输出 {"items": [...]}。每一项的 target_refdes、quote、check_type 保持与上面一致。'
        )
    else:
        task = (
            "【审查要求】\n"
            f"{guide}"
            "识别本次上传的全部原理图 PDF 上出现的元器件位号和网络标签。"
            "对照依据，并逐条核对审查规则。"
            "依据或规则里写了、图纸上没有的电路，结论使用「不适用」，不要当成遗漏。"
            "审查规则里的电流、容值、电阻、耐压和功率用 numeric，conclusion 写「无法判定」。"
            f"{_datasheet_task(datasheet_text)}"
            f"{_roster_task(roster)}"
            "忽略图纸上未出现的器件。"
            "无法确定的连接标为无法判定。"
            '只输出 {"items": [...]}。'
        )
    return "\n\n".join([*blocks, task])


_COVERAGE_NOTE = {
    "rules": "本次没有产品定义和 Datasheet。quote 只能来自审查规则原文，requirement_source 写审查规则。需要手册或额定值、而图纸上没有标出的项，conclusion 使用「无法判定」。不要把器件常识写成依据。",
    "product": "本次未提供 Datasheet。quote 只能来自产品定义原文和审查规则原文。需要手册或额定值、而图纸上没有标出的项，conclusion 使用「无法判定」。不要把器件常识写成依据。",
    "datasheet": "本次未提供产品定义。手册检查项的 quote 只能来自【Datasheet 依据】，requirement_source 写 Datasheet。审查规则检查项的 quote 只能来自【审查规则】，requirement_source 写审查规则。不要把器件常识写成产品要求。",
}


def _target_view(item: SchematicProposal, requirement_source: str) -> dict:
    return {
        "target_refdes": item.target_refdes,
        "check_type": item.check_type,
        "requirement_source": requirement_source,
        "quote": item.quote,
    }


def _identity(item: SchematicProposal) -> tuple:
    return (item.target_refdes, item.check_type, item.quote)


def _generate_batch(
    transport,
    cache: MemoryCache,
    cfg: dict,
    *,
    pdf_sha: str,
    stage: str,
    system: str,
    user: str,
    targets: list[dict] | None,
) -> tuple[ProposalBatch | None, dict, str]:
    prompt_version = f"{cfg.get('prompt_version') or PROMPT_VERSION}#{stage}"
    payload = {"pdf_sha256": pdf_sha, "user": user, "targets": targets or []}
    key = cache_key(str(cfg.get("model") or "gemini-3.5-flash"), prompt_version, payload)
    if cfg.get("cache", True):
        hit = cache.get(key)
        if hit is not None:
            if hit.get("verdict") == "无法判定":
                return None, hit, hit.get("reason") or "模型输出未通过校验"
            return ProposalBatch.model_validate(hit), hit, ""
    body = {
        "model": cfg.get("model") or "gemini-3.5-flash",
        "system": system,
        "user": user,
        "max_output_tokens": int(cfg.get("max_output_tokens") or MAX_OUTPUT_TOKENS),
    }
    attempts = int(cfg.get("retries", 2)) + 1
    last = "模型输出未通过校验"
    for _ in range(attempts):
        try:
            content = transport(body)
            batch = _parse_batch(content)
        except Exception:
            continue
        dumped = batch.model_dump()
        if cfg.get("cache", True):
            cache.put(key, dumped)
        return batch, dumped, ""
    failed = {"verdict": "无法判定", "reason": last}
    if cfg.get("cache", True):
        cache.put(key, failed)
    return None, failed, last


def _parse_batch(content: str) -> ProposalBatch:
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
    if isinstance(data, list):
        data = {"items": data}
    try:
        return ProposalBatch.model_validate(data)
    except ValidationError as exc:
        raise ValueError("模型输出未通过校验") from exc


def _accept(
    proposal: SchematicProposal,
    basis: str,
    product: str = "",
    datasheet: str = "",
    rules: str = "",
) -> tuple[SchematicDraftItem | None, RejectedSchematicItem | None]:
    homes = quote_homes(proposal.quote, product, datasheet, rules)
    if not quote_matches(proposal.quote, basis):
        return _hold(proposal, "quote 不是依据原文的子串", homes)
    if not _mentions(proposal.target_refdes, proposal.findings):
        return _hold(proposal, "findings 没有写明图纸上的位号", homes)
    if claims_datasheet(proposal.requirement_source) and "datasheet" not in homes:
        return _hold(proposal, "quote 不在 Datasheet 原文里", homes)
    if proposal.part_number.strip() and proposal.part_number.strip() not in proposal.quote:
        return _hold(proposal, "声称的型号没有出现在这条原文里", homes)
    source = source_label(proposal.requirement_source, homes)
    evidences = evidence_list(proposal.quote, homes, part_number=proposal.part_number.strip())
    if proposal.check_type != "numeric":
        return _draft_from(
            proposal,
            conclusion=proposal.conclusion,
            method="model",
            requirement_source=source,
            evidence=evidences,
        ), None
    if proposal.calc_op or proposal.calc_inputs:
        return _accept_derived(proposal, source, evidences)
    tree, param = _rule_tree(proposal)
    for number in _numbers(tree):
        if not _number_in_clause(number, proposal.quote):
            reason = "这个限值没有出现在原文里，也没有给出可重算的输入。"
            return _hold(proposal, reason, homes, calculation=unverified_calculation(proposal.limit_value, proposal.limit_unit, reason))
    if not tree or not _structure_ok(tree):
        return (
            _draft_from(
                proposal,
                conclusion="无法判定",
                method="rule-engine",
                reason="数值检查项缺少可执行的规则",
                requirement_source=source,
                evidence=evidences,
                admission="needs_review",
            ),
            None,
        )
    if not param or proposal.observed is None:
        return (
            _draft_from(
                proposal,
                conclusion="无法判定",
                method="rule-engine",
                reason="数值检查项缺少图上读数或参数名",
                requirement_source=source,
                evidence=evidences,
                admission="needs_evidence",
            ),
            None,
        )
    variables = {
        param: {"value": proposal.observed.value, "unit": proposal.observed.unit},
    }
    judged = evaluate(tree, variables)
    return (
        _draft_from(
            proposal,
            conclusion=_VERDICTS[judged["result"]],
            method="rule-engine",
            reason="" if judged["result"] != "unknown" else "规则引擎无法完成数值比较",
            trace=judged["trace"],
            requirement_source=source,
            evidence=evidences,
        ),
        None,
    )


def _accept_derived(
    proposal: SchematicProposal,
    source: str,
    evidences: list[ReviewEvidence],
) -> tuple[SchematicDraftItem | None, RejectedSchematicItem | None]:
    if proposal.observed is None or proposal.compare_op is None:
        reason = "派生计算缺少图上读数或比较方式。"
        return _hold(proposal, reason, [], calculation=unverified_calculation(proposal.limit_value, proposal.limit_unit, reason), evidences=evidences, source=source)
    calc, judged, reason = verify_derived(
        proposal.calc_op or "",
        list(proposal.calc_inputs),
        proposal.quote,
        observed_value=proposal.observed.value,
        observed_unit=proposal.observed.unit,
        compare_op=proposal.compare_op,
        limit_unit=proposal.limit_unit,
        limit_high=proposal.limit_high,
        param_name=proposal.param_name,
        proposed_limit=proposal.limit_value,
        evidence_ids=[item.evidence_id for item in evidences if item.verification_status == "verified"],
    )
    derived = ReviewEvidence(
        evidence_id=sha256_text(f"derived\n{proposal.quote}\n{proposal.calc_op or ''}")[:12],
        source_type="derived_calculation",
        parameter_name=proposal.param_name,
        parameter_value=calc.calculated_value,
        unit=calc.calculated_unit,
        verification_status="verified" if calc.validation_status == "verified" else "not_verified",
    )
    if calc.validation_status != "verified" or judged is None:
        return _hold(proposal, reason or calc.assumptions, [], calculation=calc, evidences=[*evidences, derived], source=source)
    return (
        _draft_from(
            proposal,
            conclusion=_VERDICTS[judged["result"]],
            method="rule-engine",
            reason=calc.assumptions,
            trace=judged["trace"],
            requirement_source=source,
            evidence=[*evidences, derived],
            calculation=calc,
        ),
        None,
    )


def _hold(
    proposal: SchematicProposal,
    reason: str,
    homes: list[str],
    *,
    calculation: DerivedCalculation | None = None,
    evidences: list[ReviewEvidence] | None = None,
    source: str = "",
) -> tuple[SchematicDraftItem, RejectedSchematicItem]:
    rows = evidences if evidences is not None else evidence_list(proposal.quote, homes)
    item = _draft_from(
        proposal,
        conclusion="无法判定",
        method="model",
        reason=reason,
        requirement_source=source or source_label(proposal.requirement_source, homes),
        evidence=rows,
        calculation=calculation,
        admission="needs_evidence",
    )
    rejected = RejectedSchematicItem(reason=reason, target_refdes=proposal.target_refdes, payload=proposal.model_dump())
    return item, rejected


def _draft_from(
    proposal: SchematicProposal,
    *,
    conclusion: str,
    method: str,
    dual_review: str = "",
    review_note: str = "",
    reason: str = "",
    trace: list | None = None,
    requirement_source: str = "",
    evidence: list[ReviewEvidence] | None = None,
    calculation: DerivedCalculation | None = None,
    admission: Admission = "accepted",
) -> SchematicDraftItem:
    return SchematicDraftItem(
        target_refdes=proposal.target_refdes.strip(),
        check_type=proposal.check_type,
        requirement_source=(requirement_source or proposal.requirement_source).strip(),
        findings=proposal.findings.strip(),
        conclusion=conclusion,  # type: ignore[arg-type]
        status="draft",
        quote=proposal.quote,
        method=method,
        dual_review=dual_review,
        needs_review=True,
        review_note=review_note or ("证据不足，不能当成已核实结论" if admission == "needs_evidence" else "草稿，需人工复核后才能启用"),
        reason=reason,
        trace=list(trace or []),
        admission=admission,
        evidence=list(evidence or []),
        calculation=calculation,
    )


def _rule_tree(proposal: SchematicProposal) -> tuple[dict, str]:
    """把模型给出的比较字段编成规则引擎表达式。表达式不进入模型输出结构。"""
    if proposal.compare_op is None or proposal.limit_value is None:
        return {}, ""
    param = proposal.param_name.strip() or "reading"
    low = {"value": proposal.limit_value, "unit": proposal.limit_unit}
    if proposal.compare_op == "between":
        if proposal.limit_high is None:
            return {}, ""
        high = {"value": proposal.limit_high, "unit": proposal.limit_unit}
        return {"between": [{"var": param}, low, high]}, param
    return {proposal.compare_op: [{"var": param}, low]}, param


_PARTISH = re.compile(r"[A-Za-z][A-Za-z0-9-]{5,}")
_REFONLY = re.compile(r"^[A-Za-z]{1,3}\d{1,4}[A-Za-z]?$")


def _mentions(refdes: str, findings: str) -> bool:
    token = refdes.strip()
    if token and re.search(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", findings):
        return True
    for part in _PARTISH.findall(findings or ""):
        if _REFONLY.fullmatch(part):
            continue
        letters = sum(ch.isalpha() for ch in part)
        digits = sum(ch.isdigit() for ch in part)
        if letters >= 3 and digits >= 3:
            return True
    return False


def combine_reviews(
    base: SchematicReviewResult,
    extra: SchematicReviewResult,
    *,
    pdf_sha: str,
    product: str,
    datasheet: str,
    cfg: dict,
) -> SchematicReviewResult:
    seen: set[tuple] = set()
    items: list[SchematicDraftItem] = []
    for item in [*base.items, *extra.items]:
        key = (item.target_refdes, item.check_type, item.quote)
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    rejected: list[RejectedSchematicItem] = []
    rejected_seen: set[tuple] = set()
    for row in [*base.rejected, *extra.rejected]:
        quote = ""
        if isinstance(row.payload, dict):
            quote = str(row.payload.get("quote") or "")
        key = (row.reason, row.target_refdes, quote)
        if key in rejected_seen:
            continue
        rejected_seen.add(key)
        rejected.append(row)
    return _finish(
        items,
        rejected,
        pdf_sha,
        product,
        datasheet,
        cfg,
        [base.result_hash, extra.result_hash],
        base.reason or extra.reason,
    )


def _finish(
    items: list[SchematicDraftItem],
    rejected: list[RejectedSchematicItem],
    pdf_sha: str,
    product_spec_text: str,
    datasheet_text: str,
    cfg: dict,
    passes: list,
    reason: str,
) -> SchematicReviewResult:
    digest = stable_hash(
        {
            "model": cfg.get("model"),
            "prompt_version": cfg.get("prompt_version"),
            "pdf_sha256": pdf_sha,
            "product_sha256": sha256_text(product_spec_text),
            "datasheet_sha256": sha256_text(datasheet_text),
            "passes": passes,
            "items": [item.model_dump() for item in items],
            "rejected": [item.model_dump() for item in rejected],
            "reason": reason,
        }
    )
    return SchematicReviewResult(items=items, rejected=rejected, result_hash=digest, reason=reason)
