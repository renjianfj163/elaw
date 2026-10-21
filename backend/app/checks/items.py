"""检查项草稿。条款号和限值必须来自已核验原文，不能由模型另写一套。"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.citation.check import BasisClauseView
from app.hashing import new_id, sha256_text, utc_now
from app.llm.provider import generate_json
from app.models import BasisCheckItem, BasisClause, BasisDocument, ReviewLog
from app.rules.evaluator import OPS
from app.verify.citation import quote_matches

_LITERAL_KEYS = {"value", "unit", "cmp", "fact_id"}


class CheckError(Exception):
    pass


class DraftProposal(BaseModel):
    item_code: str = Field(min_length=1, max_length=80)
    kind: Literal["numeric", "presence", "qualitative"]
    report_topics: list[str] = Field(min_length=1)
    required_params: list[str] = Field(default_factory=list)
    requirement: dict = Field(default_factory=dict)
    quote: str = Field(min_length=1)


class CheckItemView(BaseModel):
    id: str
    doc_id: str
    clause_path: str
    clause_sha256: str
    item_code: str
    kind: Literal["numeric", "presence", "qualitative"]
    report_topics: list[str]
    required_params: list[str] = Field(default_factory=list)
    requirement: dict = Field(default_factory=dict)
    applies_when: dict = Field(default_factory=dict)
    quote: str
    status: str


class DraftResult(BaseModel):
    item: CheckItemView | None = None
    rejected_reason: str | None = None


def reject_proposal(proposal: DraftProposal, clause_text: str) -> str | None:
    if not any(topic.strip() for topic in proposal.report_topics):
        return "缺少报告定位词"
    if not quote_matches(proposal.quote, clause_text):
        return "quote 不是条款原文的子串"
    numbers = list(_numbers(proposal.requirement))
    if proposal.kind == "numeric":
        if not proposal.requirement:
            return "数值检查项缺少规则"
        if not any(name.strip() for name in proposal.required_params):
            return "数值检查项缺少参数名"
        if not numbers:
            return "数值检查项必须使用条款原文中的数字"
        if not _structure_ok(proposal.requirement):
            return "规则含有引擎不认识的结构"
    for number in numbers:
        if not _number_in_clause(number, clause_text):
            return "requirement 中的数字不在条款原文里"
    return None


def propose_draft(clause: BasisClauseView, *, transport, cache, config: dict | None = None) -> DraftResult:
    if not clause.verified or clause.status != "active":
        return DraftResult(rejected_reason="条款未核验或已停用")
    cfg = _stage_config(config, "draft")
    last_reason = {"text": ""}

    def checking(body):
        try:
            content = transport(body)
            proposal = DraftProposal.model_validate(json.loads(content))
        except (ValidationError, json.JSONDecodeError, TypeError) as exc:
            last_reason["text"] = "模型输出未通过校验"
            raise ValueError("模型输出未通过校验") from exc
        reason = reject_proposal(proposal, clause.text)
        if reason:
            last_reason["text"] = reason
            raise ValueError(reason)
        return content

    result = generate_json(DraftProposal, _draft_prompt(clause), transport=checking, cache=cache, config=cfg)
    output = result["output"]
    if output.get("verdict") == "无法判定":
        return DraftResult(rejected_reason=last_reason["text"] or output.get("reason") or "模型输出未通过校验")
    proposal = DraftProposal.model_validate(output)
    reason = reject_proposal(proposal, clause.text)
    if reason:
        return DraftResult(rejected_reason=reason)
    return DraftResult(
        item=CheckItemView(
            id=new_id(),
            doc_id=clause.doc_id,
            clause_path=clause.path,
            clause_sha256=clause.text_sha256 or sha256_text(clause.text),
            item_code=proposal.item_code,
            kind=proposal.kind,
            report_topics=[topic.strip() for topic in proposal.report_topics if topic.strip()],
            required_params=[name.strip() for name in proposal.required_params if name.strip()],
            requirement=proposal.requirement,
            quote=proposal.quote,
            status="draft",
        )
    )


def save_draft(db: Session, item: CheckItemView, *, by: str, note: str = "") -> BasisCheckItem:
    if not by.strip():
        raise CheckError("需要留下审核人")
    if item.status != "draft":
        raise CheckError("新检查项只能是草稿")
    clause = verified_clause(db, doc_id=item.doc_id, path=item.clause_path, text_sha256=item.clause_sha256)
    if clause is None:
        raise CheckError("只能依据已核验条款起草")
    proposal = DraftProposal(
        item_code=item.item_code,
        kind=item.kind,
        report_topics=item.report_topics,
        required_params=item.required_params,
        requirement=item.requirement,
        quote=item.quote,
    )
    reason = reject_proposal(proposal, clause.text)
    if reason:
        raise CheckError(reason)
    row = BasisCheckItem(
        id=item.id,
        doc_id=item.doc_id,
        clause_path=item.clause_path,
        clause_sha256=item.clause_sha256,
        item_code=item.item_code,
        kind=item.kind,
        report_topics=list(item.report_topics),
        required_params=list(item.required_params),
        requirement=dict(item.requirement),
        applies_when=dict(item.applies_when),
        quote=item.quote,
        status="draft",
        created_at=utc_now(),
    )
    db.add(row)
    db.add(
        ReviewLog(
            id=new_id(),
            target_type="basis_check_item",
            target_id=row.id,
            action="draft",
            by=by.strip(),
            at=utc_now(),
            note=note,
        )
    )
    db.flush()
    return row


def activate_item(db: Session, item_id: str, *, by: str, note: str = "") -> BasisCheckItem:
    if not by.strip():
        raise CheckError("需要留下审核人")
    row = db.get(BasisCheckItem, item_id)
    if row is None or row.status != "draft":
        raise CheckError("只能审核草稿")
    clause = verified_clause(db, doc_id=row.doc_id, path=row.clause_path, text_sha256=row.clause_sha256)
    if clause is None:
        raise CheckError("条款未核验，不能启用")
    row.status = "active"
    row.reviewed_by = by.strip()
    row.reviewed_at = utc_now()
    db.add(
        ReviewLog(
            id=new_id(),
            target_type="basis_check_item",
            target_id=row.id,
            action="activate",
            by=by.strip(),
            at=utc_now(),
            note=note,
        )
    )
    db.flush()
    return row


def list_items(db: Session, status: str | None = None) -> list[BasisCheckItem]:
    stmt = select(BasisCheckItem).order_by(BasisCheckItem.created_at, BasisCheckItem.item_code)
    if status:
        stmt = stmt.where(BasisCheckItem.status == status)
    return list(db.scalars(stmt))


def verified_clause(db: Session, *, doc_id: str, path: str, text_sha256: str) -> BasisClause | None:
    stmt = (
        select(BasisClause)
        .join(BasisDocument, BasisClause.document_id == BasisDocument.id)
        .where(
            BasisDocument.doc_id == doc_id,
            BasisDocument.status == "active",
            BasisClause.path == path,
            BasisClause.text_sha256 == text_sha256,
            BasisClause.verified.is_(True),
            BasisClause.verified_at.is_not(None),
        )
    )
    return db.scalar(stmt)


def view_from_row(row: BasisCheckItem) -> CheckItemView:
    return CheckItemView(
        id=row.id,
        doc_id=row.doc_id,
        clause_path=row.clause_path,
        clause_sha256=row.clause_sha256,
        item_code=row.item_code,
        kind=row.kind,
        report_topics=list(row.report_topics or []),
        required_params=list(row.required_params or []),
        requirement=dict(row.requirement or {}),
        applies_when=dict(row.applies_when or {}),
        quote=row.quote,
        status=row.status,
    )


def _draft_prompt(clause: BasisClauseView) -> str:
    return (
        "只根据下面这一条已核验条款起草一个检查项。"
        "不要补充条款里没有的数字，不要引用其他标准，不要编造条款号。\n"
        f"条款路径：{clause.path}\n"
        "条款原文：\n"
        f"{clause.text}\n"
        "quote 必须是条款原文的连续子串。"
        "kind 只能是 numeric、presence、qualitative。"
        "numeric 的 requirement 只能使用规则引擎运算符，其中的数字必须原样出现在条款原文里。"
    )


def _stage_config(config: dict | None, stage: str) -> dict:
    from app.llm.provider import load_llm_config

    cfg = dict(config or load_llm_config())
    cfg["prompt_version"] = f"{cfg.get('prompt_version', 'check')}#{stage}"
    return cfg


def _numbers(node) -> list[float]:
    found: list[float] = []
    if isinstance(node, bool) or node is None:
        return found
    if isinstance(node, (int, float)):
        return [float(node)]
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "var":
                continue
            found.extend(_numbers(value))
        return found
    if isinstance(node, list):
        for item in node:
            found.extend(_numbers(item))
    return found


def _number_in_clause(number: float, text: str) -> bool:
    candidates = [str(number)]
    if number == int(number):
        candidates.insert(0, str(int(number)))
    folded = text.replace(",", "")
    for candidate in candidates:
        if re.search(rf"(?<!\d){re.escape(candidate)}(?!\d)", folded):
            return True
    return False


def _structure_ok(node) -> bool:
    if node is None or isinstance(node, str):
        return True
    if isinstance(node, bool):
        return False
    if isinstance(node, (int, float)):
        return True
    if isinstance(node, list):
        return bool(node) and all(_structure_ok(item) for item in node)
    if isinstance(node, dict):
        if not node:
            return False
        if set(node) <= _LITERAL_KEYS and "value" in node:
            return _structure_ok(node.get("value"))
        if set(node) == {"var"} and isinstance(node["var"], str) and node["var"].strip():
            return True
        if len(node) == 1:
            op, arg = next(iter(node.items()))
            if op in OPS and op != "var":
                return _structure_ok(arg)
    return False
