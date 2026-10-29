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

from app.checks.items import _number_in_clause, _numbers, _structure_ok
from app.hashing import sha256_bytes, sha256_text, stable_hash
from app.llm.provider import MemoryCache, cache_key
from app.rules.evaluator import evaluate
from app.verify.citation import quote_matches

PROMPT_VERSION = "schematic-2"
MAX_BASIS_CHARS = 48_000
MAX_PDF_BYTES = 40 * 1024 * 1024
_VERDICTS = {"true": "符合", "false": "不符合", "unknown": "无法判定"}

SYSTEM_INSTRUCTION = """你是一位资深的硬件电路设计审核专家。你的任务是根据提供的产品定义规范和元器件 Datasheet 文本，审查输入的电路原理图 PDF。

必须遵守：
- 只根据图纸上真实可见的连线、符号和标号下结论。不要因为 Datasheet 推荐了某个电路，就认为图纸上也画了。
- findings 必须写明图纸上对应的位号或网络名，并且包含 target_refdes 本身。
- 忽略图纸上未出现的器件，不要为它们生成审查项。
- 依据里写了、图纸上没有画出的模块或引脚，conclusion 使用「不适用」，不要把它们当成遗漏。
- 文末审查规则要逐条形成检查项。图纸上没有对应电路时，conclusion 使用「不适用」。
- 审查规则里的电流、容值、电阻、耐压和功率是数值比较。check_type 用 numeric，不要自行换算或估算。
- 线条遮挡、符号重叠或无法看清连接时，conclusion 使用「无法判定」。
- quote 必须是产品定义或 Datasheet 依据中的连续原文，不要改写，不要补字。
- check_type 为 numeric 时，不要自行判断符合或不符合，conclusion 固定写「无法判定」。把从图纸读到的数放进 observed（value 与 unit）。把依据中的比较写成 compare_op、limit_value、limit_unit。区间比较时 compare_op 用 between，并填写 limit_high。param_name 用简短英文参数名。这些数字必须原样出现在依据原文里。
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
    requirement: dict = Field(default_factory=dict)
    required_params: list[str] = Field(default_factory=list)
    compare_op: Literal["==", "!=", "<", "<=", ">", ">=", "between"] | None = None
    limit_value: float | None = None
    limit_unit: str = ""
    limit_high: float | None = None
    param_name: str = ""


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
        "model": str(raw.get("model") or "gemini-2.5-pro"),
        "planner_model": str(raw.get("planner_model") or "gemini-2.5-flash"),
        "prompt_version": str(raw.get("prompt_version") or PROMPT_VERSION),
        "retries": int(raw.get("retries") if raw.get("retries") is not None else 2),
        "cache": bool(raw.get("cache", True)),
        "max_output_tokens": int(raw.get("max_output_tokens") or 8192),
    }


def review_schematic(
    schematic_pdf_path: Path | str,
    product_spec_text: str,
    datasheet_text: str,
    *,
    transport=None,
    cache: MemoryCache | None = None,
    config: dict | None = None,
) -> SchematicReviewResult:
    path = Path(schematic_pdf_path)
    _check_inputs(path, product_spec_text, datasheet_text)
    pdf_bytes = path.read_bytes()
    pdf_sha = sha256_bytes(pdf_bytes)
    cfg = dict(config or load_gemini_config())
    store = cache if cache is not None else MemoryCache()
    owns_transport = transport is None
    sender = transport if transport is not None else _default_transport(path)
    try:
        return _review_with_sender(path, product_spec_text, datasheet_text, sender, store, cfg, pdf_sha)
    finally:
        if owns_transport:
            close_transport(sender)


def _review_with_sender(path, product_spec_text, datasheet_text, sender, store, cfg, pdf_sha):
    basis = f"{product_spec_text}\n{datasheet_text}"
    primary, primary_raw, primary_reason = _generate_batch(
        sender,
        store,
        cfg,
        pdf_sha=pdf_sha,
        stage="primary",
        system=SYSTEM_INSTRUCTION,
        user=_user_prompt(product_spec_text, datasheet_text),
        targets=None,
    )
    if primary is None:
        return _finish([], [], pdf_sha, product_spec_text, datasheet_text, cfg, [primary_raw], primary_reason)

    drafts: list[SchematicDraftItem] = []
    rejected: list[RejectedSchematicItem] = []
    qualitative: list[SchematicProposal] = []
    for proposal in primary.items:
        item, reject = _accept(proposal, basis)
        if reject is not None:
            rejected.append(reject)
            continue
        if proposal.check_type == "qualitative":
            qualitative.append(proposal)
            continue
        assert item is not None
        drafts.append(item)

    second_raw = None
    if qualitative:
        targets = [_target_view(item) for item in qualitative]
        second, second_raw, second_reason = _generate_batch(
            sender,
            store,
            cfg,
            pdf_sha=pdf_sha,
            stage="qualitative-second",
            system=SYSTEM_INSTRUCTION + "\n这是第二遍复核。不要参考、复述或猜测任何先前结论。",
            user=_second_prompt(product_spec_text, datasheet_text, targets),
            targets=targets,
        )
        by_key: dict[tuple, list[str]] = {}
        if second is not None:
            for proposal in second.items:
                by_key.setdefault(_identity(proposal), []).append(proposal.conclusion)
        for proposal in qualitative:
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


def _default_transport(path: Path):
    from app.schematic.gemini_client import gemini_transport

    return gemini_transport(path)


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


def _check_inputs(path: Path, product_spec_text: str, datasheet_text: str) -> None:
    check_schematic_file(path)
    if not product_spec_text.strip():
        raise SchematicInputError("请提供产品定义文本")
    if not datasheet_text.strip():
        raise SchematicInputError("请提供 Datasheet 文本")
    if len(product_spec_text) > MAX_BASIS_CHARS or len(datasheet_text) > MAX_BASIS_CHARS:
        raise SchematicInputError(
            "依据文本过长。请只保留与图上器件相关的章节，例如引脚配置、典型应用电路、绝对最大额定值，不要送入整本 Datasheet。"
        )


def _user_prompt(product_spec_text: str, datasheet_text: str) -> str:
    return (
        f"【产品定义依据】\n{product_spec_text.strip()}\n\n"
        f"【Datasheet 依据】\n{datasheet_text.strip()}\n\n"
        "【审查要求】\n"
        "识别 PDF 图纸上出现的元器件位号和网络标签。"
        "对照依据，并逐条核对文末的审查规则。"
        "依据或规则里写了、图纸上没有的电路，结论使用「不适用」，不要当成遗漏。"
        "审查规则里的电流、容值、电阻、耐压和功率用 numeric，conclusion 写「无法判定」。"
        "忽略图纸上未出现的器件。"
        "无法确定的连接标为无法判定。"
        '只输出 {"items": [...]}。'
    )


def _second_prompt(product_spec_text: str, datasheet_text: str, targets: list[dict]) -> str:
    listed = json.dumps(targets, ensure_ascii=False)
    return (
        f"【产品定义依据】\n{product_spec_text.strip()}\n\n"
        f"【Datasheet 依据】\n{datasheet_text.strip()}\n\n"
        "【第二遍复核】\n"
        "下面这些定性项需要重新判断。这里没有上一遍的发现或结论，请只根据图纸和依据填写。\n"
        "依据里写了但图纸上没有的模块，结论使用「不适用」。\n"
        f"{listed}\n"
        '只输出 {"items": [...]}。每一项的 target_refdes、quote、check_type 保持与上面一致。'
    )


def _target_view(item: SchematicProposal) -> dict:
    return {
        "target_refdes": item.target_refdes,
        "check_type": item.check_type,
        "requirement_source": item.requirement_source,
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
    key = cache_key(str(cfg.get("model") or "gemini-2.5-pro"), prompt_version, payload)
    if cfg.get("cache", True):
        hit = cache.get(key)
        if hit is not None:
            if hit.get("verdict") == "无法判定":
                return None, hit, hit.get("reason") or "模型输出未通过校验"
            return ProposalBatch.model_validate(hit), hit, ""
    body = {
        "model": cfg.get("model") or "gemini-2.5-pro",
        "system": system,
        "user": user,
        "max_output_tokens": int(cfg.get("max_output_tokens") or 8192),
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
    proposal: SchematicProposal, basis: str
) -> tuple[SchematicDraftItem | None, RejectedSchematicItem | None]:
    payload = proposal.model_dump()
    if not quote_matches(proposal.quote, basis):
        return None, RejectedSchematicItem(reason="quote 不是依据原文的子串", target_refdes=proposal.target_refdes, payload=payload)
    if not _mentions(proposal.target_refdes, proposal.findings):
        return None, RejectedSchematicItem(
            reason="findings 没有写明图纸上的位号",
            target_refdes=proposal.target_refdes,
            payload=payload,
        )
    if proposal.check_type != "numeric":
        return _draft_from(proposal, conclusion=proposal.conclusion, method="model"), None
    proposal = _compile_numeric(proposal)
    for number in _numbers(proposal.requirement):
        if not _number_in_clause(number, basis):
            return None, RejectedSchematicItem(
                reason="requirement 中的数字不在依据原文里",
                target_refdes=proposal.target_refdes,
                payload=payload,
            )
    if not proposal.requirement or not _structure_ok(proposal.requirement):
        return (
            _draft_from(
                proposal,
                conclusion="无法判定",
                method="rule-engine",
                reason="数值检查项缺少可执行的规则",
            ),
            None,
        )
    params = [name.strip() for name in proposal.required_params if name.strip()]
    if not params or proposal.observed is None:
        return (
            _draft_from(
                proposal,
                conclusion="无法判定",
                method="rule-engine",
                reason="数值检查项缺少图上读数或参数名",
            ),
            None,
        )
    variables = {
        params[0]: {"value": proposal.observed.value, "unit": proposal.observed.unit},
    }
    judged = evaluate(proposal.requirement, variables)
    return (
        _draft_from(
            proposal,
            conclusion=_VERDICTS[judged["result"]],
            method="rule-engine",
            reason="" if judged["result"] != "unknown" else "规则引擎无法完成数值比较",
            trace=judged["trace"],
        ),
        None,
    )


def _draft_from(
    proposal: SchematicProposal,
    *,
    conclusion: str,
    method: str,
    dual_review: str = "",
    review_note: str = "",
    reason: str = "",
    trace: list | None = None,
) -> SchematicDraftItem:
    return SchematicDraftItem(
        target_refdes=proposal.target_refdes.strip(),
        check_type=proposal.check_type,
        requirement_source=proposal.requirement_source.strip(),
        findings=proposal.findings.strip(),
        conclusion=conclusion,  # type: ignore[arg-type]
        status="draft",
        quote=proposal.quote,
        method=method,
        dual_review=dual_review,
        needs_review=True,
        review_note=review_note or "草稿，需人工复核后才能启用",
        reason=reason,
        trace=list(trace or []),
    )


def _compile_numeric(proposal: SchematicProposal) -> SchematicProposal:
    if proposal.requirement or proposal.compare_op is None or proposal.limit_value is None:
        return proposal
    param = proposal.param_name.strip() or "reading"
    low = {"value": proposal.limit_value, "unit": proposal.limit_unit}
    if proposal.compare_op == "between":
        if proposal.limit_high is None:
            return proposal
        high = {"value": proposal.limit_high, "unit": proposal.limit_unit}
        requirement = {"between": [{"var": param}, low, high]}
    else:
        requirement = {proposal.compare_op: [{"var": param}, low]}
    return proposal.model_copy(update={"requirement": requirement, "required_params": [param]})


def _mentions(refdes: str, findings: str) -> bool:
    token = refdes.strip()
    if not token:
        return False
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, findings) is not None


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
