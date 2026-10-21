"""三值规则求值器。无 IO，无 eval，缺参数得到 unknown。"""

from __future__ import annotations

from dataclasses import dataclass

from pint import UnitRegistry

OPS = {"==", "!=", "<", "<=", ">", ">=", "and", "or", "not", "in", "between", "var"}

_UREG = UnitRegistry()
_UNIT_ALIAS = {
    "mg/l": "milligram / liter",
    "mg/L": "milligram / liter",
    "g/l": "gram / liter",
    "g/L": "gram / liter",
    "ug/l": "microgram / liter",
    "ug/L": "microgram / liter",
    "μg/l": "microgram / liter",
    "μg/L": "microgram / liter",
    "mg/m3": "milligram / meter ** 3",
    "mg/m³": "milligram / meter ** 3",
    "mSv": "millisievert",
    "uSv": "microsievert",
    "μSv": "microsievert",
    "µSv": "microsievert",
    "Sv": "sievert",
    "mSv/a": "millisievert / year",
    "mSv/年": "millisievert / year",
    "uSv/h": "microsievert / hour",
    "μSv/h": "microsievert / hour",
    "µSv/h": "microsievert / hour",
}


class RuleEngineRefusal(Exception):
    pass


@dataclass(frozen=True)
class Interval:
    low: float | None
    high: float | None
    low_closed: bool
    high_closed: bool

    @property
    def is_point(self) -> bool:
        return (
            self.low is not None
            and self.high is not None
            and self.low == self.high
            and self.low_closed
            and self.high_closed
        )


def _point(value: float) -> Interval:
    return Interval(value, value, True, True)


def _censored(op: str, value: float) -> Interval:
    if op == "<":
        return Interval(None, value, False, False)
    if op == "<=":
        return Interval(None, value, False, True)
    if op == ">":
        return Interval(value, None, False, False)
    if op == ">=":
        return Interval(value, None, True, False)
    if op in {"nd", "not_detected"}:
        return Interval(None, value, False, False)
    return None  # type: ignore[return-value]


def _convert(value: float, unit: str | None, target: str | None) -> float | None:
    if unit is None or target is None or unit == target:
        return value
    try:
        quantity = _UREG.Quantity(value, _UNIT_ALIAS.get(unit, unit))
        return float(quantity.to(_UNIT_ALIAS.get(target, target)).magnitude)
    except Exception:
        return None


def _as_interval(raw, target_unit: str | None) -> tuple[Interval | None, str | None]:
    """返回 (区间, fact_id)。无法换算时区间为 None。"""
    fact_id = None
    unit = None
    if isinstance(raw, dict) and "fact_id" in raw and "value" in raw:
        fact_id = raw.get("fact_id")
        unit = raw.get("unit")
        raw = raw["value"]
    if isinstance(raw, bool) or raw is None:
        return None, fact_id
    if isinstance(raw, (int, float)):
        converted = _convert(float(raw), unit, target_unit if unit else None)
        if converted is None:
            return None, fact_id
        return _point(converted if unit else float(raw)), fact_id
    if isinstance(raw, dict) and "cmp" in raw and "value" in raw:
        unit = raw.get("unit") or unit
        number = _convert(float(raw["value"]), unit, target_unit if unit and target_unit else None)
        if number is None:
            return None, fact_id
        interval = _censored(str(raw["cmp"]), number if unit else float(raw["value"]))
        if interval is None:
            return None, fact_id
        return interval, fact_id
    return None, fact_id


def _le(interval: Interval, limit: float) -> str:
    if interval.high is not None and interval.high <= limit:
        return "true"
    if interval.low is not None and (
        interval.low > limit or (interval.low == limit and not interval.low_closed)
    ):
        return "false"
    return "unknown"


def _lt(interval: Interval, limit: float) -> str:
    if interval.high is not None and (
        interval.high < limit or (interval.high == limit and not interval.high_closed)
    ):
        return "true"
    if interval.low is not None and interval.low >= limit:
        return "false"
    return "unknown"


def _ge(interval: Interval, limit: float) -> str:
    if interval.low is not None and (
        interval.low > limit or (interval.low == limit and interval.low_closed)
    ):
        return "true"
    if interval.high is not None and (
        interval.high < limit or (interval.high == limit and not interval.high_closed)
    ):
        return "false"
    return "unknown"


def _gt(interval: Interval, limit: float) -> str:
    if interval.low is not None and (
        interval.low > limit or (interval.low == limit and not interval.low_closed)
    ):
        return "true"
    if interval.high is not None and interval.high <= limit:
        return "false"
    return "unknown"


def _eq_interval(interval: Interval, limit: float) -> str:
    if interval.is_point and interval.low == limit:
        return "true"
    if interval.high is not None and interval.high < limit:
        return "false"
    if interval.low is not None and interval.low > limit:
        return "false"
    if interval.high is not None and interval.high == limit and not interval.high_closed:
        return "false"
    if interval.low is not None and interval.low == limit and not interval.low_closed:
        return "false"
    if interval.is_point:
        return "false"
    return "unknown"


_CMP = {"<=": _le, "<": _lt, ">=": _ge, ">": _gt, "==": _eq_interval, "!=": None}


def _blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, dict) and "value" in value and value["value"] is None:
        return True
    return False


def _target_unit(literal) -> str | None:
    if isinstance(literal, dict) and "unit" in literal and "cmp" not in literal:
        return literal.get("unit")
    return None


def _literal_number(literal) -> float | None:
    if isinstance(literal, bool):
        return None
    if isinstance(literal, (int, float)):
        return float(literal)
    if isinstance(literal, dict) and "value" in literal and "cmp" not in literal:
        try:
            return float(literal["value"])
        except (TypeError, ValueError):
            return None
    return None


def evaluate(expr, variables: dict) -> dict:
    missing: list[str] = []
    trace: list[dict] = []
    result = _eval(expr, variables, missing, trace)
    return {"result": result, "missing": list(dict.fromkeys(missing)), "trace": trace}


def _eval(expr, variables: dict, missing: list[str], trace: list[dict]) -> str:
    if isinstance(expr, dict):
        if set(expr) - OPS or len(expr) != 1:
            trace.append({"op": "unsupported", "expr": expr, "result": "unknown"})
            return "unknown"
        op = next(iter(expr))
        arg = expr[op]
        if op == "var":
            return _eval_var(arg, variables, missing, trace)
        if op == "not":
            inner = _eval(arg, variables, missing, trace)
            result = {"true": "false", "false": "true", "unknown": "unknown"}[inner]
            trace.append({"op": "not", "result": result})
            return result
        if op in {"and", "or"}:
            return _eval_bool(op, arg, variables, missing, trace)
        if op in {"==", "!=", "<", "<=", ">", ">="}:
            return _eval_cmp(op, arg, variables, missing, trace)
        if op == "in":
            return _eval_in(arg, variables, missing, trace)
        if op == "between":
            return _eval_between(arg, variables, missing, trace)
    trace.append({"op": "unsupported", "result": "unknown"})
    return "unknown"


def _eval_var(name, variables, missing, trace) -> str:
    if not isinstance(name, str) or name not in variables or _blank(variables.get(name)):
        if isinstance(name, str):
            missing.append(name)
        trace.append({"op": "var", "var": name, "result": "unknown"})
        return "unknown"
    trace.append({"op": "var", "var": name, "result": "true"})
    return "true"


def _eval_bool(op, args, variables, missing, trace) -> str:
    if not isinstance(args, list) or not args:
        trace.append({"op": op, "result": "unknown"})
        return "unknown"
    parts = [_eval(item, variables, missing, trace) for item in args]
    if op == "and":
        result = "false" if "false" in parts else "unknown" if "unknown" in parts else "true"
    else:
        result = "true" if "true" in parts else "unknown" if "unknown" in parts else "false"
    trace.append({"op": op, "parts": parts, "result": result})
    return result


def _lookup_raw(node, variables, missing):
    if isinstance(node, dict) and set(node) == {"var"}:
        name = node["var"]
        if not isinstance(name, str) or name not in variables or _blank(variables.get(name)):
            if isinstance(name, str):
                missing.append(name)
            return name, None
        return name, variables[name]
    return None, node


def _eval_cmp(op, args, variables, missing, trace) -> str:
    if not isinstance(args, list) or len(args) != 2:
        trace.append({"op": op, "result": "unknown"})
        return "unknown"
    left_name, left_raw = _lookup_raw(args[0], variables, missing)
    right_name, right_raw = _lookup_raw(args[1], variables, missing)
    if left_raw is None or right_raw is None:
        trace.append({"op": op, "var": left_name or right_name, "result": "unknown"})
        return "unknown"
    if _is_numericish(left_raw) or _is_numericish(right_raw):
        return _eval_numeric(op, left_name, left_raw, right_raw, trace)
    if op == "==":
        result = "true" if left_raw == right_raw else "false"
    elif op == "!=":
        result = "true" if left_raw != right_raw else "false"
    else:
        result = "unknown"
    trace.append({"op": op, "var": left_name, "left": left_raw, "right": right_raw, "result": result})
    return result


def _is_numericish(raw) -> bool:
    if isinstance(raw, bool):
        return False
    if isinstance(raw, (int, float)):
        return True
    if isinstance(raw, dict) and "value" in raw and (
        "unit" in raw or "cmp" in raw or "fact_id" in raw
    ):
        inner = raw["value"]
        return not isinstance(inner, bool) and isinstance(inner, (int, float, dict))
    return False


def _eval_numeric(op, left_name, left_raw, right_raw, trace) -> str:
    target = _target_unit(right_raw) or _target_unit(left_raw)
    if isinstance(right_raw, dict) and "fact_id" in right_raw:
        target = right_raw.get("unit") or target
    left_interval, fact_id = _unwrap_numeric(left_raw, target)
    right_number = _literal_number(right_raw if not _is_var_value(right_raw) else right_raw.get("value"))
    if _is_var_value(right_raw) and right_number is None:
        right_interval, _ = _unwrap_numeric(right_raw, target)
        if left_interval and right_interval and left_interval.is_point and right_interval.is_point:
            right_number = right_interval.low
        else:
            trace.append({"op": op, "var": left_name, "result": "unknown", "note": "两侧都不是确定数值"})
            return "unknown"
    if left_interval is None or right_number is None:
        trace.append({"op": op, "var": left_name, "fact_id": fact_id, "result": "unknown", "note": "单位换算失败或不是数值"})
        return "unknown"
    if op == "!=":
        eq = _eq_interval(left_interval, right_number)
        result = {"true": "false", "false": "true", "unknown": "unknown"}[eq]
    else:
        fn = _CMP[op]
        result = fn(left_interval, right_number)
    trace.append(
        {
            "op": op,
            "var": left_name,
            "fact_id": fact_id,
            "left": left_raw if not isinstance(left_raw, dict) else left_raw.get("value"),
            "right": right_number,
            "result": result,
        }
    )
    return result


def _is_var_value(raw) -> bool:
    return isinstance(raw, dict) and "fact_id" in raw


def _unwrap_numeric(raw, target_unit: str | None) -> tuple[Interval | None, str | None]:
    fact_id = None
    unit = None
    body = raw
    if isinstance(raw, dict) and "fact_id" in raw:
        fact_id = raw.get("fact_id")
        unit = raw.get("unit")
        body = raw.get("value")
    if isinstance(body, dict) and "unit" in body and "cmp" not in body and "value" in body:
        unit = body.get("unit") or unit
        body = body["value"]
    if isinstance(body, bool) or body is None:
        return None, fact_id
    if isinstance(body, (int, float)):
        number = _convert(float(body), unit, target_unit if unit else None)
        if number is None:
            return None, fact_id
        return _point(number), fact_id
    if isinstance(body, dict) and "cmp" in body:
        unit = body.get("unit") or unit
        try:
            bound = float(body["value"])
        except (TypeError, ValueError):
            return None, fact_id
        number = _convert(bound, unit, target_unit if unit else None)
        if number is None:
            return None, fact_id
        interval = _censored(str(body["cmp"]), number)
        return interval, fact_id
    return None, fact_id


def _eval_in(args, variables, missing, trace) -> str:
    if not isinstance(args, list) or len(args) != 2 or not isinstance(args[1], list):
        trace.append({"op": "in", "result": "unknown"})
        return "unknown"
    name, raw = _lookup_raw(args[0], variables, missing)
    if raw is None or isinstance(raw, dict):
        trace.append({"op": "in", "var": name, "result": "unknown"})
        return "unknown"
    result = "true" if raw in args[1] else "false"
    trace.append({"op": "in", "var": name, "left": raw, "result": result})
    return result


def _eval_between(args, variables, missing, trace) -> str:
    if not isinstance(args, list) or len(args) != 3:
        trace.append({"op": "between", "result": "unknown"})
        return "unknown"
    low = _eval_cmp(">=", [args[0], args[1]], variables, missing, trace)
    high = _eval_cmp("<=", [args[0], args[2]], variables, missing, trace)
    parts = [low, high]
    result = "false" if "false" in parts else "unknown" if "unknown" in parts else "true"
    trace.append({"op": "between", "parts": parts, "result": result})
    return result


def judge(item: dict, variables: dict) -> dict:
    if item.get("kind") == "qualitative":
        raise RuleEngineRefusal("定性项不走规则引擎")
    status = item.get("status")
    if status is not None and status != "active":
        raise RuleEngineRefusal("只有 active 规则可以评估")
    applies_expr = item.get("applies_when")
    if not applies_expr:
        applies = {"result": "true", "missing": [], "trace": []}
    else:
        applies = evaluate(applies_expr, variables)
    if applies["result"] == "false":
        return {"verdict": "不适用", "missing": [], "trace": applies["trace"]}
    if applies["result"] == "unknown":
        return {"verdict": "无法判定", "missing": applies["missing"], "trace": applies["trace"]}
    required = [
        key
        for key in item.get("required_params") or []
        if key not in variables or _blank(variables.get(key))
    ]
    if required:
        return {"verdict": "无法判定", "missing": required, "trace": applies["trace"]}
    requirement = evaluate(item["requirement"], variables)
    verdict = {"true": "符合", "false": "不符合", "unknown": "无法判定"}[requirement["result"]]
    missing = requirement["missing"] if verdict == "无法判定" else []
    return {
        "verdict": verdict,
        "missing": missing,
        "trace": applies["trace"] + requirement["trace"],
    }


def assert_can_evaluate(*, item_status: str, verified_at, in_force: bool) -> None:
    if item_status != "active":
        raise RuleEngineRefusal("draft 规则不能进入评估")
    if verified_at is None or not in_force:
        raise RuleEngineRefusal("版本未核验或在基准日无效")
