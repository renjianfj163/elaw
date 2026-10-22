"""把字符偏移换成报告里的章节或表题。偏移仍保留，只作核对。"""

from __future__ import annotations

import re

_CAPTION = re.compile(r"表\s*(\d+)(?:\s*[-－—]\s*(\d+))?")
_HEADING = re.compile(r"^(\d+(?:\.\d+)+)(.*)$")
_SPACE = re.compile(r"\s+")


def describe_place(blocks, char_start: int) -> str:
    index = _index_at(blocks, char_start)
    if index is None:
        return ""
    block = blocks[index]
    caption = _nearest(blocks, index, _caption_label)
    heading = _nearest(blocks, index, _heading_label)
    chosen = _closer(caption, heading)
    if chosen is None:
        return getattr(block, "section_path", "") or ""
    label, source, _at = chosen
    parts = [label]
    if source == "caption":
        context = _table_context(blocks, block)
        if context and context not in label:
            parts.append(context)
        section = _section_number(blocks, block)
        if section and section not in label:
            parts.append(section)
    return " · ".join(parts)


def _index_at(blocks, char_start: int) -> int | None:
    for index, block in enumerate(blocks):
        if block.char_start <= char_start < block.char_end:
            return index
    return None


def _nearest(blocks, index: int, label_of):
    for pos in range(index - 1, -1, -1):
        label = label_of(blocks[pos])
        if label:
            return label, blocks[pos].char_start
    return None


def _closer(caption, heading):
    if caption and heading:
        if caption[1] >= heading[1]:
            return caption[0], "caption", caption[1]
        return heading[0], "heading", heading[1]
    if caption:
        return caption[0], "caption", caption[1]
    if heading:
        return heading[0], "heading", heading[1]
    return None


def _caption_label(block) -> str:
    text = _compact(getattr(block, "text", ""))
    if len(text) > 80:
        return ""
    match = _CAPTION.search(text)
    if match is None or match.start() > 2:
        return ""
    number, sub = match.group(1), match.group(2)
    code = f"表{number}-{sub}" if sub else f"表{number}"
    if sub:
        return code
    rest = text[match.end() :].strip(" ：:，,。")
    title = rest.split("（")[0].split("(")[0].strip()
    if title and len(title) <= 12:
        return f"{code} {title}"
    return code


def _heading_label(block) -> str:
    text = _compact(getattr(block, "text", ""))
    if not text or len(text) > 40:
        return ""
    match = _HEADING.match(text)
    if match is None:
        return ""
    number, rest = match.group(1), match.group(2).strip(" .、")
    if any(int(part) > 30 for part in number.split(".")):
        return ""
    if number.count(".") < 2 and not re.search(r"[\u4e00-\u9fff]", rest):
        return ""
    if rest and not re.search(r"[\u4e00-\u9fff]", rest):
        return ""
    if len(rest) > 18:
        rest = rest[:18]
    return f"{number} {rest}".strip() if rest else number


def _section_number(blocks, block) -> str:
    outer = _outer_table(block)
    if outer is None:
        return getattr(block, "section_path", "") or ""
    found = ""
    sticky = getattr(block, "section_path", "") or ""
    for candidate in blocks:
        if candidate.char_start >= block.char_start:
            break
        label = _heading_label(candidate)
        if not label or _outer_table(candidate) != outer:
            continue
        number = label.split(" ", 1)[0]
        if sticky and number != sticky:
            continue
        found = number
    return found


def _table_context(blocks, block) -> str:
    path = list(getattr(block, "table_path", []) or [])
    if not path:
        return ""
    inner = path[-1]
    prefix = path[:-1]
    if inner.col > 1:
        stub = _cell_label(blocks, prefix, inner.table, inner.row, 1)
        if stub:
            return stub
    if inner.row > 1:
        return _cell_label(blocks, prefix, inner.table, 1, inner.col)
    return ""


def _cell_label(blocks, prefix, table: int, row: int, col: int) -> str:
    for block in blocks:
        if not _same_cell(block, prefix, table, row, col):
            continue
        label = _short_label(getattr(block, "text", ""))
        if label:
            return label
    return ""


def _same_cell(block, prefix, table: int, row: int, col: int) -> bool:
    path = list(getattr(block, "table_path", []) or [])
    if len(path) != len(prefix) + 1:
        return False
    for got, expect in zip(path, prefix):
        if (got.table, got.row, got.col) != (expect.table, expect.row, expect.col):
            return False
    last = path[-1]
    return (last.table, last.row, last.col) == (table, row, col)


def _outer_table(block):
    path = list(getattr(block, "table_path", []) or [])
    if not path:
        return None
    return path[0].table


def _short_label(text: str) -> str:
    text = _compact(text)
    if not text or len(text) > 16 or text.startswith("表") or _HEADING.match(text):
        return ""
    if re.search(r"\d", text):
        return ""
    return text


def _compact(text: str) -> str:
    return _SPACE.sub(" ", (text or "").strip())
