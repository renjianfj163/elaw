"""从已有文字层抽出约束条目。

只接受资料里写明的条目和摘录。扫描页、缺页码或摘录对不上时不生成可用约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schematic.knowledge.model import (
    Applicability,
    DesignConstraint,
    DesignParameter,
    KnowledgeError,
)

_TYPES = {"hard_limit", "recommended", "conditional", "reference", "intent"}
_CATEGORIES = {"electrical", "power", "interface", "boot_reset_clock", "reference", "product"}
_BLOCK = re.compile(r":::constraint\s*\n(.*?)\n:::", re.DOTALL)
_TABLE = re.compile(r":::table\s*\n(.*?)\n:::", re.DOTALL)


@dataclass
class Extracted:
    constraints: list[DesignConstraint] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    parse_status: str = "needs_review"
    parse_note: str = ""
    page_count: int | None = None
    unparsed_pages: list[int] = field(default_factory=list)


def extract_text(text: str, *, pages: dict[int, str] | None = None) -> Extracted:
    """pages 给出时，摘录必须落在声明的那一页。文本资料不另造页码。"""
    result = Extracted()
    if pages is None:
        result.page_count = 1
        page_map = {1: text}
    else:
        result.page_count = len(pages)
        result.unparsed_pages = [number for number, body in sorted(pages.items()) if not body.strip()]
        page_map = pages
    if pages is not None and not any(body.strip() for body in pages.values()):
        result.parse_status = "not_parsed"
        result.parse_note = "没有抽出文字。未做 OCR，也不根据版面猜测参数。"
        return result
    for match in _BLOCK.finditer(text):
        _take_block(result, match.group(1), text, page_map, pages is not None, "text")
    for match in _TABLE.finditer(text):
        _take_table(result, match.group(1), text, page_map, pages is not None)
    if result.rejected and result.constraints:
        result.parse_status = "partial" if result.unparsed_pages else "needs_review"
        result.parse_note = "有条目因摘录或页码不能对应而未保存。"
    elif result.rejected and not result.constraints:
        result.parse_status = "needs_review"
        result.parse_note = "没有保存任何约束。"
    elif result.unparsed_pages:
        result.parse_status = "partial"
        result.parse_note = "部分页面没有文字。这些页不参与约束提取。"
    elif result.constraints:
        result.parse_status = "parsed"
        result.parse_note = "条目来自文本中的约束块或表。复核前不能用于确定结论。"
    else:
        result.parse_status = "needs_review"
        result.parse_note = "文字已读取，但没有可保存的约束条目。"
    return result


def contains_quote(text: str, quote: str) -> bool:
    if not quote.strip():
        return False
    return _compact(quote) in _compact(text)


def quote_supported(text: str, quote: str) -> bool:
    """摘录必须出现在约束块之外的正文里，不能只写在字段中。"""
    prose = _BLOCK.sub(" ", text)
    prose = _TABLE.sub(" ", prose)
    return contains_quote(prose, quote)


def _take_block(result: Extracted, body: str, text: str, pages: dict[int, str], paged: bool, method: str) -> None:
    try:
        fields = _fields(body)
        result.constraints.append(_constraint_from(fields, text, pages, paged, method))
    except KnowledgeError as exc:
        result.rejected.append(str(exc))


def _take_table(result: Extracted, body: str, text: str, pages: dict[int, str], paged: bool) -> None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        result.rejected.append("表至少需要表头和一行数据。")
        return
    shared: dict[str, str] = {}
    rows: list[str] = []
    header = ""
    for line in lines:
        if "|" in line and not header:
            header = line
            continue
        if header:
            rows.append(line)
            continue
        if ":" not in line:
            result.rejected.append(f"无法读取的表行：{line}")
            return
        key, value = line.split(":", 1)
        shared[key.strip()] = value.strip()
    columns = [item.strip() for item in header.split("|")]
    for row in rows:
        cells = [item.strip() for item in row.split("|")]
        if len(cells) != len(columns):
            result.rejected.append("表的列数和表头不一致，这一行没有保存。")
            continue
        fields = dict(shared)
        fields.update(dict(zip(columns, cells, strict=True)))
        try:
            result.constraints.append(_constraint_from(fields, text, pages, paged, "table"))
        except KnowledgeError as exc:
            result.rejected.append(str(exc))


def _constraint_from(fields: dict[str, str], text: str, pages: dict[int, str], paged: bool, method: str) -> DesignConstraint:
    category = fields.get("category", "")
    constraint_type = fields.get("constraint_type", "")
    parameter = fields.get("parameter", "")
    quote = fields.get("quote", "")
    title = fields.get("title", "")
    if category not in _CATEGORIES:
        raise KnowledgeError(f"约束类别无法识别：{category or '空'}")
    if constraint_type not in _TYPES:
        raise KnowledgeError(f"约束类型无法识别：{constraint_type or '空'}")
    if not parameter:
        raise KnowledgeError("约束缺少参数名。")
    if not quote:
        raise KnowledgeError(f"{parameter} 缺少原文摘录。")
    if not quote_supported(text, quote):
        raise KnowledgeError(f"{parameter} 的摘录不在资料正文中。")
    if "source_page" not in fields or fields["source_page"] == "":
        raise KnowledgeError(f"{parameter} 缺少来源页码。")
    page = _int(fields["source_page"])
    if page is None:
        raise KnowledgeError(f"{parameter} 的来源页码不是整数。")
    if paged:
        body = pages.get(page, "")
        if not body.strip():
            raise KnowledgeError(f"{parameter} 声明的第 {page} 页没有文字。")
        if not quote_supported(body, quote):
            raise KnowledgeError(f"{parameter} 的摘录不在第 {page} 页。")
    if not title:
        pin = fields.get("pin", "")
        title = f"{parameter} {pin}".strip()
    return DesignConstraint(
        constraint_id=fields.get("id", ""),
        category=category,
        title=title,
        description=fields.get("description", ""),
        constraint_type=constraint_type,  # type: ignore[arg-type]
        applicability=Applicability(
            manufacturer=fields.get("manufacturer", ""),
            mpn=fields.get("mpn", ""),
            order_suffix=fields.get("order_suffix", ""),
            package=fields.get("package", ""),
            revision=fields.get("revision", ""),
            symbol=fields.get("symbol", ""),
            refdes=fields.get("refdes", ""),
            pin=fields.get("pin", ""),
            pin_name=fields.get("pin_name", ""),
            net=fields.get("net", ""),
            voltage_domain=fields.get("voltage_domain", ""),
            interface=fields.get("interface", ""),
            side=fields.get("side", ""),
            role=fields.get("role", ""),
            condition=fields.get("condition", ""),
            operating_mode=fields.get("operating_mode", ""),
        ),
        parameters=[_parameter(fields, parameter)],
        validation_method=fields.get("validation_method", ""),
        source_page=page,
        source_section=fields.get("source_section", ""),
        source_quote=quote,
        extraction_method=method,  # type: ignore[arg-type]
        origin="extracted_candidate",
        verification_status="unverified",
        effective_status="current",
    )


def _parameter(fields: dict[str, str], name: str) -> DesignParameter:
    return DesignParameter(
        name=name,
        value=_optional_float(fields, "value"),
        value_text=fields.get("value_text", ""),
        min_value=_optional_float(fields, "min"),
        max_value=_optional_float(fields, "max"),
        unit=fields.get("unit", ""),
        tolerance_pct=_optional_float(fields, "tolerance"),
        condition=fields.get("condition", ""),
    )


def _fields(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in body.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        found[key.strip()] = value.strip()
    return found


def _optional_float(fields: dict[str, str], key: str) -> float | None:
    if key not in fields or fields[key] == "":
        return None
    number = _float(fields[key])
    if number is None:
        raise KnowledgeError(f"{key} 不是数值，已放弃这条约束。")
    return number


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _int(text: str) -> int | None:
    if not re.fullmatch(r"\d+", text.strip()):
        return None
    return int(text)


def _compact(text: str) -> str:
    return "".join(text.split())
