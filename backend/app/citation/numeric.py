"""引用里的数值只交给规则引擎比较。"""

from __future__ import annotations

from app.citation.quantities import Quantity
from app.rules.evaluator import evaluate

RULE_VERSION = "citation-2"


def compare_values(op: str, left: Quantity, right: Quantity) -> dict:
    return evaluate(
        {op: [{"var": "left"}, {"var": "right"}]},
        {
            "left": {"fact_id": "left", "value": left.value, "unit": left.unit},
            "right": {"fact_id": "right", "value": right.value, "unit": right.unit},
        },
    )


def same_subject(left: Quantity, right: Quantity) -> bool:
    if left.unit != right.unit:
        return False
    if not (left.population & right.population):
        return False
    if left.organs != right.organs:
        return False
    if left.periods and right.periods and not (left.periods & right.periods):
        return False
    return True


def verdict_of(result: str) -> str:
    if result == "true":
        return "符合"
    if result == "false":
        return "不符合"
    return "无法判定"
