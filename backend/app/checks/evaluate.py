"""评估已审核的检查项。数值只走规则引擎；定性做两遍，第二遍看不到第一遍结论。"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field, ValidationError

from app.checks.items import CheckItemView, DraftProposal, _stage_config, reject_proposal
from app.checks.retrieve import retrieve_blocks
from app.citation.check import BasisClauseView
from app.citation.quantities import extract_quantities
from app.hashing import sha256_text, stable_hash
from app.llm.provider import generate_json
from app.rules.evaluator import RuleEngineRefusal, judge

RULE_VERSION = "check-1"
_CODE = re.compile(
    r"(?:GB|HJ|GBZ)\s*/?\s*T?\s*\d+(?:\.\d+)?\s*[-—]\s*\d{4}",
    re.IGNORECASE,
)


class ItemFinding(BaseModel):
    item_code: str = ""
    kind: str
    question: str
    verdict: str
    evidence_span: str = ""
    char_start: int = 0
    char_end: int = 0
    quote: str = ""
    basis_doc_id: str | None = None
    basis_path: str | None = None
    section_path: str = ""
    dual_review: str = ""
    needs_review: bool = False
    review_note: str = ""
    reason: str = ""
    missing: list = Field(default_factory=list)
    trace: list = Field(default_factory=list)
    as_of: str


class RejectedItem(BaseModel):
    reason: str
    item_code: str = ""
    payload: dict = Field(default_factory=dict)


class CheckRun(BaseModel):
    findings: list[ItemFinding]
    rejected: list[RejectedItem]
    result_hash: str


def evaluate_checks(
    parsed,
    *,
    items: list[CheckItemView],
    clauses: list[BasisClauseView],
    as_of: date,
    transport,
    cache,
    config: dict | None = None,
) -> CheckRun:
    findings: list[ItemFinding] = []
    rejected: list[RejectedItem] = []
    llm_used: list[dict] = []
    allowed = _basis_codes(clauses, as_of)
    findings.extend(_outside_findings(parsed, allowed, as_of))
    findings.extend(_unparsed_findings(parsed.blocks, as_of))

    def call_model(schema, prompt: str, stage: str) -> dict:
        cfg = _stage_config(config, stage)
        if hasattr(cache, "model"):
            cache.model = str(cfg.get("model") or "")
            cache.prompt_version = str(cfg.get("prompt_version") or "")
        result = generate_json(schema, prompt, transport=transport, cache=cache, config=cfg)
        llm_used.append({"cache_key": result["cache_key"], "output": result["output"]})
        return result["output"]

    for item in items:
        if item.status != "active":
            continue
        clause = _matching_clause(item, clauses, as_of)
        if clause is None:
            continue
        proposal = _as_proposal(item)
        if proposal is None:
            _reject(rejected, item.item_code, "检查项字段无法读取", {})
            findings.append(_undecided(item, clause, as_of, "检查项字段无法读取"))
            continue
        reason = reject_proposal(proposal, clause.text)
        if reason:
            _reject(rejected, item.item_code, reason, {"quote": item.quote})
            findings.append(_undecided(item, clause, as_of, reason))
            continue
        if item.kind == "numeric":
            findings.append(_numeric(parsed, item, clause, allowed, as_of))
            continue
        chunks = retrieve_blocks(parsed.blocks, item.report_topics, limit=24)
        readable, captions, outside = _split_chunks(chunks, parsed.blocks, allowed)
        readable = readable[:6]
        if readable:
            if item.kind == "presence":
                findings.append(_presence(parsed, item, clause, readable, as_of, call_model, rejected))
            elif item.kind == "qualitative":
                findings.append(_qualitative(parsed, item, clause, readable, as_of, call_model, rejected))
            else:
                findings.append(_undecided(item, clause, as_of, "检查项类型不认识"))
            continue
        if outside:
            block = outside[0]
            findings.append(
                _located(
                    item,
                    clause,
                    as_of,
                    kind="out_of_scope",
                    question="这项报告内容引用了依据库以外的标准",
                    verdict="超出依据范围",
                    block=block,
                )
            )
            continue
        if captions or _section_has_image(parsed.blocks, chunks):
            findings.append(_undecided(item, clause, as_of, "依赖未解析的图片或公式"))
            continue
        findings.append(_undecided(item, clause, as_of, "没有定位到报告片段"))

    findings = sorted(findings, key=_finding_key)
    rejected = sorted(rejected, key=lambda row: (row.item_code, row.reason))
    llm_used = sorted(llm_used, key=lambda row: row["cache_key"])
    digest = stable_hash(
        {
            "rule_version": RULE_VERSION,
            "report_sha256": sha256_text(parsed.full_text),
            "basis_sha256": stable_hash({"clauses": sorted(clause.text_sha256 for clause in clauses)}),
            "as_of": as_of.isoformat(),
            "findings": [row.model_dump() for row in findings],
            "rejected": [row.model_dump() for row in rejected],
            "llm_output": llm_used,
        }
    )
    return CheckRun(findings=findings, rejected=rejected, result_hash=digest)


def _numeric(parsed, item: CheckItemView, clause: BasisClauseView, allowed: set[str], as_of: date) -> ItemFinding:
    readable, _captions, _outside = _split_chunks(
        retrieve_blocks(parsed.blocks, item.report_topics, limit=24),
        parsed.blocks,
        allowed,
    )
    readable = readable[:6]
    quantities = _quantities(readable)
    variables: dict = {}
    evidence = None
    if len(item.required_params) == 1 and len(quantities) == 1:
        block, quantity = quantities[0]
        variables[item.required_params[0]] = {
            "value": quantity.value,
            "unit": quantity.unit,
            "fact_id": "report",
        }
        evidence = (block, quantity)
    elif len(quantities) > 1:
        return _undecided(
            item,
            clause,
            as_of,
            "报告片段里有多个数值，无法对应到同一个参数",
            missing=list(item.required_params),
        )
    try:
        judged = judge(
            {
                "kind": "numeric",
                "status": "active",
                "requirement": item.requirement,
                "required_params": list(item.required_params),
                "applies_when": item.applies_when or None,
            },
            variables,
        )
    except RuleEngineRefusal:
        return _undecided(item, clause, as_of, "规则引擎拒绝了这项")
    span, start, end, section = "", 0, 0, ""
    if evidence is not None and judged["verdict"] in {"符合", "不符合"}:
        block, quantity = evidence
        pos = (block.text or "").find(quantity.raw)
        if pos >= 0:
            span = quantity.raw
            start = block.char_start + pos
            end = start + len(span)
            section = getattr(block, "section_path", "") or ""
            if parsed.full_text[start:end] != span:
                span, start, end = "", 0, 0
    if judged["verdict"] in {"符合", "不符合"} and not span:
        return _undecided(item, clause, as_of, "数值在报告里对不上原文位置", missing=judged.get("missing") or [])
    return ItemFinding(
        item_code=item.item_code,
        kind="numeric",
        question="报告中的数值是否满足已核验条款",
        verdict=judged["verdict"],
        evidence_span=span,
        char_start=start,
        char_end=end,
        quote=item.quote if judged["verdict"] in {"符合", "不符合"} else "",
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        section_path=section,
        missing=list(judged.get("missing") or []),
        trace=list(judged.get("trace") or []),
        as_of=as_of.isoformat(),
        reason="" if judged["verdict"] != "无法判定" else "缺少报告中的必需数值",
    )


def _presence(parsed, item, clause, chunks, as_of, call_model, rejected) -> ItemFinding:
    prompt = _prompt(
        clause.text,
        chunks,
        "判断报告片段里有没有写这项要求的措施或制度。"
        "verdict 只能是 有、无、无法判定。evidence_span 必须是报告片段中的连续原文。",
    )
    output = call_model(_PassOut, prompt, "presence")
    verdict, span, located = _consume_pass(
        output,
        chunks,
        parsed.full_text,
        item.item_code,
        rejected,
        {"有", "无", "无法判定"},
    )
    return _model_finding(item, clause, as_of, "presence", "报告是否写有该项措施或制度", verdict, span, located, chunks)


def _qualitative(parsed, item, clause, chunks, as_of, call_model, rejected) -> ItemFinding:
    prompt_a = _prompt(
        clause.text,
        chunks,
        "请按条款原文的顺序，逐条核对报告片段是否写明这些定性要件。"
        "verdict 只能是 符合、不符合、无法判定。evidence_span 必须是报告片段中的连续原文。",
    )
    prompt_b = _prompt(
        clause.text,
        chunks,
        "请换一个顺序：先看报告片段写了什么，再对照条款原文的要件。"
        "这是独立的一次判断，不要引用或假设任何其他轮次的结论。"
        "verdict 只能是 符合、不符合、无法判定。evidence_span 必须是报告片段中的连续原文。",
    )
    allowed = {"符合", "不符合", "无法判定"}
    first = _consume_pass(
        call_model(_PassOut, prompt_a, "qual-a"),
        chunks,
        parsed.full_text,
        item.item_code,
        rejected,
        allowed,
    )
    second = _consume_pass(
        call_model(_PassOut, prompt_b, "qual-b"),
        chunks,
        parsed.full_text,
        item.item_code,
        rejected,
        allowed,
    )
    agreed = first[0] == second[0]
    verdict, span, located = first if agreed else ("无法判定", "", None)
    finding = _model_finding(
        item,
        clause,
        as_of,
        "qualitative",
        "定性要件是否写明",
        verdict,
        span,
        located,
        chunks,
        reason="" if agreed else "两遍结论不一致",
    )
    finding.dual_review = "同源双审"
    finding.needs_review = True
    finding.review_note = "需人工复核"
    finding.trace = [{"pass": "a", "verdict": first[0]}, {"pass": "b", "verdict": second[0]}]
    return finding


class _PassOut(BaseModel):
    verdict: str
    evidence_span: str = ""
    reason: str = ""


def _prompt(clause_text: str, chunks, instruction: str) -> str:
    body = "\n---\n".join(chunk.text or "" for chunk in chunks)
    return (
        f"{instruction}\n"
        "不要比较数值，不要换算单位，不要引用片段之外的标准。\n"
        "条款原文：\n"
        f"{clause_text}\n"
        "报告片段如下：\n"
        f"{body}"
    )


def _consume_pass(output: dict, chunks, full_text: str, item_code: str, rejected: list[RejectedItem], allowed: set[str]):
    verdict = output.get("verdict")
    span = output.get("evidence_span") or ""
    if verdict not in allowed:
        _reject(rejected, item_code, "模型输出未通过校验", output)
        return "无法判定", "", None
    if verdict == "无法判定":
        return verdict, "", None
    if verdict in {"有", "符合", "不符合"} or span:
        located = _locate(span, chunks, full_text)
        if located is None:
            _reject(rejected, item_code, "evidence_span 与报告原文对不上", {"evidence_span": span, "verdict": verdict})
            return "无法判定", "", None
        return verdict, span, located
    return verdict, "", None


def _model_finding(item, clause, as_of, kind, question, verdict, span, located, chunks, reason: str = "") -> ItemFinding:
    start, end = located if located else (0, 0)
    section = ""
    if span:
        for chunk in chunks:
            if span in (chunk.text or ""):
                section = getattr(chunk, "section_path", "") or ""
                break
    return ItemFinding(
        item_code=item.item_code,
        kind=kind,
        question=question,
        verdict=verdict,
        evidence_span=span,
        char_start=start,
        char_end=end,
        quote=item.quote if span else "",
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        section_path=section,
        reason=reason,
        as_of=as_of.isoformat(),
    )


def _locate(span: str, chunks, full_text: str):
    if not span:
        return None
    for chunk in chunks:
        text = chunk.text or ""
        pos = text.find(span)
        if pos < 0:
            continue
        start = chunk.char_start + pos
        end = start + len(span)
        if 0 <= start < end <= len(full_text) and full_text[start:end] == span:
            return start, end
    return None


def _outside_findings(parsed, allowed: set[str], as_of: date) -> list[ItemFinding]:
    found = []
    for block in parsed.blocks:
        text = getattr(block, "text", "") or ""
        unknown = _unknown_codes(text, allowed)
        if not unknown:
            continue
        if parsed.full_text[block.char_start : block.char_end] != text:
            continue
        found.append(
            ItemFinding(
                kind="out_of_scope",
                question="报告引用了依据库以外的标准：" + "、".join(sorted(unknown)),
                verdict="超出依据范围",
                evidence_span=text,
                char_start=block.char_start,
                char_end=block.char_end,
                section_path=getattr(block, "section_path", "") or "",
                as_of=as_of.isoformat(),
            )
        )
    return found


def _unparsed_findings(blocks, as_of: date) -> list[ItemFinding]:
    sections: dict[str, int] = {}
    for block in blocks:
        if getattr(block, "type", "") == "image" or getattr(block, "not_parsed", False):
            section = getattr(block, "section_path", "") or ""
            sections[section] = sections.get(section, 0) + 1
    findings = []
    for section in sorted(sections):
        label = section or "未分节"
        findings.append(
            ItemFinding(
                kind="unparsed",
                question=f"{label}含未解析的图片或公式，依赖这些内容的结论无法判定",
                verdict="无法判定",
                section_path=section,
                as_of=as_of.isoformat(),
            )
        )
    return findings


def _split_chunks(chunks, blocks, allowed: set[str]):
    readable, captions, outside = [], [], []
    for chunk in chunks:
        if _unknown_codes(chunk.text or "", allowed):
            outside.append(chunk)
            continue
        if _is_caption(chunk, blocks):
            captions.append(chunk)
            continue
        readable.append(chunk)
    return readable, captions, outside


def _is_caption(block, blocks) -> bool:
    text = (getattr(block, "text", "") or "").strip()
    if not text or len(text) > 80:
        return False
    if "图" not in text and "公式" not in text:
        return False
    if getattr(block, "has_image", False):
        return True
    following = _following(block, blocks)
    return following is not None and (
        getattr(following, "type", "") == "image" or getattr(following, "not_parsed", False)
    )


def _following(block, blocks):
    for index, current in enumerate(blocks):
        if current is block and index + 1 < len(blocks):
            return blocks[index + 1]
    return None


def _section_has_image(blocks, chunks) -> bool:
    sections = {getattr(chunk, "section_path", "") or "" for chunk in chunks}
    for block in blocks:
        if (getattr(block, "section_path", "") or "") not in sections:
            continue
        if getattr(block, "type", "") == "image" or getattr(block, "not_parsed", False):
            return True
    return False


def _quantities(blocks):
    found = []
    for block in blocks:
        for quantity in extract_quantities(block.text or ""):
            found.append((block, quantity))
    return found


def _matching_clause(item: CheckItemView, clauses: list[BasisClauseView], as_of: date) -> BasisClauseView | None:
    for clause in clauses:
        if clause.doc_id != item.doc_id or clause.path != item.clause_path:
            continue
        if clause.text_sha256 != item.clause_sha256:
            continue
        if clause.verified and clause.status == "active" and clause.effective_date <= as_of:
            return clause
    return None


def _basis_codes(clauses: list[BasisClauseView], as_of: date) -> set[str]:
    codes: set[str] = set()
    for clause in clauses:
        if clause.verified and clause.status == "active" and clause.effective_date <= as_of:
            codes |= _codes(f"{clause.doc_id} {clause.title}")
    return codes


def _unknown_codes(text: str, allowed: set[str]) -> set[str]:
    return {code for code in _codes(text) if code not in allowed}


def _codes(text: str) -> set[str]:
    return {re.sub(r"[^A-Z0-9]", "", match.group(0).upper()) for match in _CODE.finditer(text or "")}


def _as_proposal(item: CheckItemView) -> DraftProposal | None:
    try:
        return DraftProposal(
            item_code=item.item_code,
            kind=item.kind,
            report_topics=item.report_topics,
            required_params=item.required_params,
            requirement=item.requirement,
            quote=item.quote,
        )
    except ValidationError:
        return None


def _reject(rejected: list[RejectedItem], item_code: str, reason: str, payload: dict) -> None:
    rejected.append(RejectedItem(reason=reason, item_code=item_code, payload=payload))


def _undecided(
    item: CheckItemView,
    clause: BasisClauseView,
    as_of: date,
    reason: str,
    missing: list | None = None,
) -> ItemFinding:
    return ItemFinding(
        item_code=item.item_code,
        kind=item.kind,
        question="这项无法判定",
        verdict="无法判定",
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        reason=reason,
        missing=list(missing or []),
        as_of=as_of.isoformat(),
    )


def _located(item, clause, as_of, *, kind, question, verdict, block) -> ItemFinding:
    return ItemFinding(
        item_code=item.item_code,
        kind=kind,
        question=question,
        verdict=verdict,
        evidence_span=block.text or "",
        char_start=block.char_start,
        char_end=block.char_end,
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        section_path=getattr(block, "section_path", "") or "",
        as_of=as_of.isoformat(),
    )


def _finding_key(row: ItemFinding):
    return (row.kind, row.item_code, row.section_path, row.char_start, row.verdict, row.evidence_span)
