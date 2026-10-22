"""评价依据清单对照人工失效映射。替代编章只读取映射表，不推断。"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.db import repo_root
from app.laws.titles import clean_title

_BOOK = re.compile(r"《([^》]+)》")
_PREFIX = "中华人民共和国"
_HEADERS = {"法规", "文件", "技术标准", "其他"}


@dataclass(frozen=True)
class RepealRow:
    name: str
    repealed_on: date
    replaced_by: str
    source: str


@dataclass(frozen=True)
class BibliographyEntry:
    name: str
    text: str
    char_start: int
    char_end: int


def default_map_path() -> Path:
    override = os.environ.get("REPEALED_MAP")
    if override:
        return Path(override)
    return repo_root() / "data" / "repealed_map.csv"


def load_repeal_map(path: Path | None = None) -> list[RepealRow]:
    file_path = path or default_map_path()
    rows: list[RepealRow] = []
    with file_path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            name = (record.get("name") or "").strip()
            if not name:
                continue
            year, month, day = (int(part) for part in record["repealed_on"].split("-"))
            rows.append(
                RepealRow(
                    name=name,
                    repealed_on=date(year, month, day),
                    replaced_by=(record.get("replaced_by") or "").strip(),
                    source=(record.get("source") or "").strip(),
                )
            )
    return rows


def evaluation_basis_entries(blocks) -> list[BibliographyEntry]:
    caption_at = None
    for index, block in enumerate(blocks):
        compact = clean_title(block.text)
        if "表6" in compact and "评价依据" in compact:
            caption_at = index
    if caption_at is None:
        return []
    top = None
    for block in blocks[caption_at + 1 :]:
        if block.table_path:
            top = block.table_path[0].table
            break
    if top is None:
        return []
    entries: list[BibliographyEntry] = []
    started = False
    for block in blocks[caption_at + 1 :]:
        if not block.table_path or block.table_path[0].table != top:
            if started:
                break
            continue
        started = True
        raw = block.text or ""
        leading = len(raw) - len(raw.lstrip())
        text = raw.strip()
        if not text or text in _HEADERS:
            continue
        books = _BOOK.findall(text)
        name = books[0].strip() if books else text
        start = block.char_start + leading
        entries.append(
            BibliographyEntry(
                name=name,
                text=text,
                char_start=start,
                char_end=start + len(text),
            )
        )
    return entries


def matching_repeal(name: str, rows: list[RepealRow]) -> RepealRow | None:
    hits = [row for row in rows if _refers_to(row.name, name, [item.name for item in rows])]
    if not hits:
        return None
    hits.sort(key=lambda row: len(clean_title(row.name)), reverse=True)
    return hits[0]


def _refers_to(name: str, candidate: str, names: list[str]) -> bool:
    target = clean_title(name)
    text = clean_title(candidate)
    if not target or not text:
        return False
    start = 0
    while True:
        index = text.find(target, start)
        if index < 0:
            return False
        end = index + len(target)
        before = text[max(0, index - len(_PREFIX)) : index]
        left_ok = index == 0 or before == _PREFIX or not _cjk(text[index - 1])
        right_ok = end == len(text) or not _cjk(text[end])
        longer = False
        for other in names:
            cleaned = clean_title(other)
            if cleaned == target or not cleaned.endswith(target):
                continue
            if text[end - len(cleaned) : end] == cleaned:
                longer = True
                break
        if left_ok and right_ok and not longer:
            return True
        start = index + 1


def _cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"
