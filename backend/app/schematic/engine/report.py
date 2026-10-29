"""审查发现。严重度表示影响，status 表示证据，两者不合成一个分数。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schematic.engine.calculators import Calculation

Severity = Literal["critical", "high", "medium", "low", "info"]
Status = Literal["confirmed", "probable", "needs_review", "not_assessable", "cleared"]
CONFIDENCE = {
    "confirmed": "high",
    "probable": "medium",
    "needs_review": "low",
    "not_assessable": "none",
    "cleared": "high",
}


class Evidence(BaseModel):
    drawing: list[str] = Field(default_factory=list)
    electrical: list[str] = Field(default_factory=list)
    device: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)


class ConstraintEvidence(BaseModel):
    """一条发现所依据的设计约束。页码是资料里的页，不是原理图页。"""

    constraint_id: str = ""
    document_title: str = ""
    document_revision: str = ""
    source_page: int | None = None
    source_section: str = ""
    source_quote: str = ""
    parameter: str = ""
    verification_status: str = ""
    origin: str = ""


class Finding(BaseModel):
    finding_id: str = ""
    rule_id: str
    title: str
    category: str
    severity: Severity
    status: Status
    confidence: Literal["high", "medium", "low", "none"] = "none"
    description: str
    evidence: Evidence = Field(default_factory=Evidence)
    affected_components: list[str] = Field(default_factory=list)
    affected_nets: list[str] = Field(default_factory=list)
    affected_pins: list[str] = Field(default_factory=list)
    traced_path: list[str] = Field(default_factory=list)
    expected_roles: list[str] = Field(default_factory=list)
    actual_roles: list[str] = Field(default_factory=list)
    source_fact_ids: list[str] = Field(default_factory=list)
    evidence_sources: list[str] = Field(default_factory=list)
    decision: str = ""
    source_pages: list[int] = Field(default_factory=list)
    datasheet_references: list[str] = Field(default_factory=list)
    constraint_evidence: list[ConstraintEvidence] = Field(default_factory=list)
    calculation: Calculation | None = None
    assumptions: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    verification_steps: list[str] = Field(default_factory=list)
    required_missing_inputs: list[str] = Field(default_factory=list)


class Rule(BaseModel):
    rule_id: str
    name: str
    category: str
    description: str
    default_severity: Severity
    required_inputs: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    check_method: str
    evidence_required: list[str] = Field(default_factory=list)
    suggested_fix: str = ""
    confidence_policy: str = ""
    test_cases: list[str] = Field(default_factory=list)
    alias_of: str = ""


class CheckResult(BaseModel):
    applicability: Literal["applicable", "not_applicable", "not_assessable"]
    findings: list[Finding] = Field(default_factory=list)
    note: str = ""
    decision: str = ""


class Run(BaseModel):
    rule_id: str
    applicability: Literal["applicable", "not_applicable", "not_assessable"]
    outcome: Literal["clear", "findings", "not_applicable", "not_assessable", "alias"]
    finding_ids: list[str] = Field(default_factory=list)
    note: str = ""
    decision: str = ""


class Report(BaseModel):
    verdict: Literal["findings", "not_assessable", "no_applicable_rules", "clear"]
    findings: list[Finding] = Field(default_factory=list)
    runs: list[Run] = Field(default_factory=list)
    unimplemented: list[str] = Field(default_factory=list)
    result_hash: str
    catalog_version: str = ""
    limits: list[str] = Field(default_factory=list)


def evidence_of(item) -> ConstraintEvidence:
    title = getattr(item, "document_title", "") or getattr(item, "source", "")
    page = getattr(item, "source_page", None)
    return ConstraintEvidence(
        constraint_id=getattr(item, "constraint_id", "") or "",
        document_title=title,
        document_revision=getattr(item, "revision", "") or "",
        source_page=page,
        source_section=getattr(item, "source_section", "") or "",
        source_quote=getattr(item, "source_quote", "") or "",
        parameter=getattr(item, "parameter", "") or "",
        verification_status=getattr(item, "verification_status", "") or ("verified" if getattr(item, "verified", False) else ""),
        origin=getattr(item, "origin", "") or "",
    )


def _reference_line(item: ConstraintEvidence) -> str:
    parts = [item.document_title, item.document_revision]
    if item.source_page is not None:
        parts.append(f"第{item.source_page}页")
    if item.source_section:
        parts.append(item.source_section)
    if item.parameter:
        parts.append(item.parameter)
    if item.source_quote:
        parts.append(item.source_quote)
    return "，".join(part for part in parts if part)


def make_finding(
    rule: Rule,
    *,
    title: str,
    status: Status,
    description: str,
    severity: Severity | None = None,
    components: list[str] | None = None,
    nets: list[str] | None = None,
    pages: list[int] | None = None,
    calculation: Calculation | None = None,
    assumptions: list[str] | None = None,
    verification_steps: list[str] | None = None,
    missing: list[str] | None = None,
    datasheet_references: list[str] | None = None,
    constraints: list | None = None,
    electrical: list[str] | None = None,
    device: list[str] | None = None,
    inferences: list[str] | None = None,
    drawing: list[str] | None = None,
    pins: list[str] | None = None,
    path: list[str] | None = None,
    expected: list[str] | None = None,
    actual: list[str] | None = None,
    fact_ids: list[str] | None = None,
    sources: list[str] | None = None,
    decision: str = "",
) -> Finding:
    chosen = status
    evidence_items = [evidence_of(item) for item in (constraints or [])]
    references = list(datasheet_references or [])
    for item in evidence_items:
        text = _reference_line(item)
        if text and text not in references:
            references.append(text)
    return Finding(
        rule_id=rule.rule_id,
        title=title,
        category=rule.category,
        severity=severity or rule.default_severity,
        status=chosen,
        confidence=CONFIDENCE[chosen],
        description=description,
        evidence=Evidence(
            drawing=drawing or [],
            electrical=electrical or [],
            device=device or [],
            inferences=inferences or [],
            missing_inputs=missing or [],
        ),
        affected_components=components or [],
        affected_nets=nets or [],
        affected_pins=pins or [],
        traced_path=path or [],
        expected_roles=expected or [],
        actual_roles=actual or [],
        source_fact_ids=fact_ids or [],
        evidence_sources=sources or [],
        decision=decision,
        source_pages=pages or [],
        datasheet_references=references,
        constraint_evidence=evidence_items,
        calculation=calculation,
        assumptions=list(assumptions or []),
        suggested_fix=rule.suggested_fix,
        verification_steps=verification_steps or [],
        required_missing_inputs=missing or [],
    )


_DECISION_RANK = (
    "confirmed_error",
    "conflicting_constraints",
    "needs_review",
    "insufficient_netlist",
    "missing_pin_role",
)


def rollup_decision(findings: list[Finding]) -> str:
    present = {item.decision for item in findings if item.decision}
    for name in _DECISION_RANK:
        if name in present:
            return name
    return ""


def finish(findings: list[Finding]) -> CheckResult:
    decision = rollup_decision(findings)
    if not findings:
        return CheckResult(applicability="applicable", decision=decision)
    if all(item.status == "not_assessable" for item in findings):
        return CheckResult(applicability="not_assessable", findings=findings, decision=decision)
    return CheckResult(applicability="applicable", findings=findings, decision=decision)


NOT_APPLICABLE = CheckResult(applicability="not_applicable")
