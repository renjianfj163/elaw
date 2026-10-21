"""把人工填写的条件编译成规则，再决定环评类型。代码里不写名录限值。"""

from app.rules.evaluator import evaluate

PARAMS = (
    {"key": "bed_count", "label": "床位数", "type": "number"},
    {"key": "floor_area_m2", "label": "建筑面积（平方米）", "type": "number"},
    {"key": "has_radiology", "label": "含放射诊疗或核医学", "type": "bool"},
    {"key": "infectious", "label": "传染病医院", "type": "bool"},
    {"key": "in_sensitive_area", "label": "位于环境敏感区", "type": "bool"},
    {"key": "has_boiler", "label": "有锅炉", "type": "bool"},
    {"key": "has_generator", "label": "有备用发电机", "type": "bool"},
    {"key": "has_wwtp", "label": "有污水站", "type": "bool"},
)
PARAM_TYPES = {item["key"]: item["type"] for item in PARAMS}
OPS = {"==", "!=", "<", "<=", ">", ">="}
COLUMNS = (("报告书", "book_rule"), ("报告表", "form_rule"), ("登记表", "register_rule"))


def compile_when(spec: dict | None) -> dict | None:
    if spec is None:
        return None
    mode = spec.get("mode")
    if mode == "always":
        return {"==": [True, True]}
    if mode == "never":
        return {"==": [True, False]}
    if mode != "when":
        raise ValueError("每一列都要选择「始终」「从不」或「按条件」")
    clauses = spec.get("all") or []
    if not isinstance(clauses, list) or not clauses:
        raise ValueError("按条件时至少写一条，数值请对照名录原文填写")
    compiled = [_clause(item) for item in clauses]
    if len(compiled) == 1:
        return compiled[0]
    return {"and": compiled}


def _clause(item: dict) -> dict:
    key = item.get("var")
    op = item.get("op")
    if key not in PARAM_TYPES or op not in OPS:
        raise ValueError("条件里有不能识别的项目或比较符")
    value = item.get("value")
    if PARAM_TYPES[key] == "bool":
        if not isinstance(value, bool) or op not in {"==", "!="}:
            raise ValueError(f"{key} 只能判断是或否")
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} 需要一个数值")
    return {op: [{"var": key}, value]}


def decide(rows: list[dict], category: str, params: dict) -> dict:
    matched = [row for row in rows if row.get("category") == category]
    if not matched:
        return _stop("无法判定", "没有已校对的对应项目类别")
    if len(matched) > 1:
        return _stop("无法判定", "同一类别有多行，请先合成一行")
    row = matched[0]
    columns = []
    missing: list[str] = []
    for label, field in COLUMNS:
        rule = row.get(field)
        if not rule:
            columns.append({"type": label, "result": "unknown", "missing": []})
            missing.append(label)
            continue
        outcome = evaluate(rule, params)
        columns.append({"type": label, "result": outcome["result"], "missing": outcome["missing"], "trace": outcome["trace"]})
        missing.extend(outcome["missing"])
    trues = [item["type"] for item in columns if item["result"] == "true"]
    unknowns = [item["type"] for item in columns if item["result"] == "unknown"]
    base = {"row_id": row.get("id"), "category": category, "columns": columns, "missing": list(dict.fromkeys(missing))}
    if len(trues) == 1 and not unknowns:
        return {**base, "determination": "已判定", "eia_type": trues[0], "reason": "已校对名录里只有这一列成立"}
    if len(trues) > 1:
        return {**base, "determination": "无法判定", "eia_type": None, "reason": "报告书、报告表、登记表里有多列同时成立"}
    if unknowns:
        return {**base, "determination": "无法判定", "eia_type": None, "reason": "还有条件没写全，或项目参数不够"}
    return {**base, "determination": "不适用", "eia_type": None, "reason": "三列都不成立"}


def diff_types(old_rows: list[dict], new_rows: list[dict], snapshots: list[dict]) -> list[dict]:
    changes = []
    for snap in snapshots:
        before = decide(old_rows, snap["category"], snap["params"])
        after = decide(new_rows, snap["category"], snap["params"])
        if before.get("eia_type") != after.get("eia_type") or before.get("determination") != after.get("determination"):
            changes.append(
                {
                    "label": snap.get("label") or snap["category"],
                    "category": snap["category"],
                    "before_type": before.get("eia_type"),
                    "after_type": after.get("eia_type"),
                    "before": before.get("determination"),
                    "after": after.get("determination"),
                }
            )
    return changes


def _stop(determination: str, reason: str) -> dict:
    return {
        "determination": determination,
        "eia_type": None,
        "reason": reason,
        "columns": [],
        "missing": [],
        "row_id": None,
        "category": None,
    }
