"""审查结论引用的证据和派生计算。

原文必须能在已保存的资料里对上。计算结果由程序重算，不必出现在原文里。
对不上的数字保持为空，不把模型给出的限值写成已核实阈值。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schematic.expr import _number_in_clause
from app.hashing import sha256_text
from app.rules.evaluator import evaluate
from app.schematic.quotes import quote_matches

SourceType = Literal["datasheet", "design_requirement", "general_rule", "derived_calculation", "ai_inference"]
Admission = Literal["accepted", "needs_evidence", "needs_review", "not_assessable"]
_HOME = {"datasheet": "datasheet", "product": "design_requirement", "rules": "general_rule"}


class ReviewEvidence(BaseModel):
    evidence_id: str
    source_type: SourceType
    source_document_id: str = ""
    document_title: str = ""
    manufacturer: str = ""
    part_number: str = ""
    document_revision: str = ""
    pdf_page: int | None = None
    section: str = ""
    table_or_figure: str = ""
    quote: str = ""
    parameter_name: str = ""
    parameter_value: float | None = None
    unit: str = ""
    operating_conditions: str = ""
    verification_status: Literal["verified", "not_verified"] = "not_verified"


class CalcInput(BaseModel):
    name: str
    value: float
    unit: str = ""


class DerivedCalculation(BaseModel):
    formula: str = ""
    input_values: list[dict] = Field(default_factory=list)
    input_units: list[str] = Field(default_factory=list)
    input_evidence_ids: list[str] = Field(default_factory=list)
    assumptions: str = ""
    calculation_method: str = "rule-engine"
    calculated_value: float | None = None
    calculated_unit: str = ""
    tolerance_or_uncertainty: str = ""
    validation_status: Literal["verified", "not_verified"] = "not_verified"


def quote_homes(quote: str, product: str, datasheet: str, rules: str) -> list[str]:
    homes: list[str] = []
    if datasheet.strip() and quote_matches(quote, datasheet):
        homes.append("datasheet")
    if product.strip() and quote_matches(quote, product):
        homes.append("product")
    if rules.strip() and quote_matches(quote, rules):
        homes.append("rules")
    return homes


def claims_datasheet(source: str) -> bool:
    text = source.lower()
    return "datasheet" in text or "手册" in source or "数据表" in source


def source_label(claimed: str, homes: list[str]) -> str:
    if "datasheet" in homes:
        return claimed.strip() if claims_datasheet(claimed) else "Datasheet"
    if claims_datasheet(claimed):
        return "待核实"
    if "rules" in homes and "product" not in homes:
        return "审查规则"
    return claimed.strip() or "待核实"


def evidence_list(quote: str, homes: list[str], *, part_number: str = "") -> list[ReviewEvidence]:
    if not homes:
        return [
            ReviewEvidence(
                evidence_id=_eid("ai_inference", quote),
                source_type="ai_inference",
                quote=quote,
                part_number="",
                verification_status="not_verified",
            )
        ]
    rows: list[ReviewEvidence] = []
    for home in homes:
        kind = _HOME[home]
        rows.append(
            ReviewEvidence(
                evidence_id=_eid(kind, quote),
                source_type=kind,  # type: ignore[arg-type]
                quote=quote,
                part_number=part_number if part_number and part_number in quote else "",
                verification_status="verified",
            )
        )
    return rows


def unverified_calculation(proposed: float | None, unit: str, reason: str) -> DerivedCalculation:
    note = reason
    if proposed is not None:
        note = f"{reason} 模型给出的 {proposed} {unit} 未采用，不能当作已核实阈值。"
    return DerivedCalculation(assumptions=note.strip(), calculated_unit=unit, validation_status="not_verified")


def verify_derived(
    op: str,
    inputs: list[CalcInput],
    quote: str,
    *,
    observed_value: float,
    observed_unit: str,
    compare_op: str,
    limit_unit: str,
    limit_high: float | None,
    param_name: str,
    proposed_limit: float | None,
    evidence_ids: list[str],
) -> tuple[DerivedCalculation, dict | None, str]:
    """输入数字必须能在已核实的 quote 里找到。比较交给规则引擎。"""
    values: dict[str, float] = {}
    missing: list[str] = []
    for item in inputs:
        if _input_in_quote(item, quote):
            values[item.name] = item.value
        else:
            missing.append(item.name or "未命名输入")
    base = DerivedCalculation(
        input_values=[{"name": item.name, "value": item.value, "unit": item.unit} for item in inputs],
        input_units=[item.unit for item in inputs],
        input_evidence_ids=list(evidence_ids),
        calculated_unit=limit_unit or observed_unit,
        validation_status="not_verified",
    )
    if missing or op not in {"divide", "multiply", "percent_divide"}:
        base.assumptions = unverified_calculation(proposed_limit, limit_unit, _missing_reason(missing, op)).assumptions
        return base, None, base.assumptions
    result = _apply(op, values)
    if result is None:
        base.assumptions = unverified_calculation(proposed_limit, limit_unit, "计算公式缺少可用的输入。").assumptions
        return base, None, base.assumptions
    param = param_name.strip() or "reading"
    tree = _tree(compare_op, param, result, limit_unit, limit_high)
    judged = evaluate(tree, {param: {"value": observed_value, "unit": observed_unit}})
    note = "限值由程序按原文中的输入重算。"
    if proposed_limit is not None and abs(proposed_limit - result) > max(1e-6, abs(result) * 1e-4):
        note = f"{note} 模型给出的 {proposed_limit} 与重算结果不一致，未采用。"
    base.formula = _formula(op, values, result)
    base.calculated_value = result
    base.assumptions = note
    base.validation_status = "verified"
    return base, judged, ""


def _input_in_quote(item: CalcInput, quote: str) -> bool:
    if _number_in_clause(item.value, quote):
        return True
    if item.unit == "%" and _number_in_clause(item.value, quote):
        return True
    return False


def _apply(op: str, values: dict[str, float]) -> float | None:
    left = values.get("left")
    right = values.get("right")
    if left is None or right is None or right == 0:
        return None
    if op == "divide":
        return left / right
    if op == "multiply":
        return left * right
    if op == "percent_divide":
        return left / (right / 100.0)
    return None


def _formula(op: str, values: dict[str, float], result: float) -> str:
    left = values.get("left")
    right = values.get("right")
    if op == "percent_divide":
        return f"{left} / ({right} / 100) = {result}"
    symbol = "/" if op == "divide" else "*"
    return f"{left} {symbol} {right} = {result}"


def _tree(compare_op: str, param: str, result: float, unit: str, limit_high: float | None) -> dict:
    reading = {"var": param}
    low = {"value": result, "unit": unit}
    if compare_op == "between":
        high = {"value": limit_high if limit_high is not None else result, "unit": unit}
        return {"between": [reading, low, high]}
    return {compare_op: [reading, low]}


def _missing_reason(missing: list[str], op: str) -> str:
    if op not in {"divide", "multiply", "percent_divide"}:
        return "没有给出可重算的公式。"
    if missing:
        return "这些计算输入没有出现在原文里：" + "、".join(missing) + "。"
    return "计算输入不完整。"


def _eid(kind: str, quote: str) -> str:
    return sha256_text(f"{kind}\n{quote}")[:12]
