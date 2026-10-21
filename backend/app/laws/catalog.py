import csv
import io
import re


def parse_catalog(text: str) -> list[dict]:
    """把名录表格解析成待校对的行。每一行都还不能当事实用。"""
    rows = _rows(text)
    if not rows:
        return []
    header = rows[0]
    index = {_norm(name): pos for pos, name in enumerate(header)}
    category_at = _find(index, ("项目类别", "类别", "项目"))
    book_at = _find(index, ("报告书",))
    form_at = _find(index, ("报告表",))
    reg_at = _find(index, ("登记表",))
    if category_at is None:
        return []
    parsed = []
    for raw in rows[1:]:
        if not any(cell.strip() for cell in raw):
            continue
        parsed.append(
            {
                "项目类别": _cell(raw, category_at),
                "报告书": _cell(raw, book_at),
                "报告表": _cell(raw, form_at),
                "登记表": _cell(raw, reg_at),
                "needs_human_review": True,
            }
        )
    return parsed


def catalog_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["项目类别", "报告书", "报告表", "登记表", "needs_human_review"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _rows(text: str) -> list[list[str]]:
    stripped = text.strip()
    if not stripped:
        return []
    if "\t" in stripped:
        return [line.split("\t") for line in stripped.splitlines() if line.strip()]
    lines = []
    for line in stripped.splitlines():
        if not line.strip() or re.fullmatch(r"\|?\s*-+\s*(\|\s*-+\s*)+\|?", line.strip()):
            continue
        if "|" in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            lines.append(cells)
    return lines


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name or "")


def _find(index: dict[str, int], names: tuple[str, ...]) -> int | None:
    for name in names:
        if name in index:
            return index[name]
    return None


def _cell(row: list[str], pos: int | None) -> str:
    if pos is None or pos >= len(row):
        return ""
    return row[pos].strip()
