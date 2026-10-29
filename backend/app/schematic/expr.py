"""检查计算式的形状，并确认式子里的数字来自资料原文。"""

from __future__ import annotations

import re
import unicodedata

from app.rules.evaluator import OPS

_LITERAL_KEYS = {"value", "unit", "cmp", "fact_id"}


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
    folded = unicodedata.normalize("NFKC", text or "")
    folded = folded.translate(str.maketrans({"−": "-", "–": "-", "—": "-", "－": "-"}))
    folded = folded.replace(",", "")
    folded = re.sub(r"(?<=-)\s+(?=\d)", "", folded)
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
