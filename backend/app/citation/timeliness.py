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
_SECTIONS = {
    "法律": "法规",
    "法规": "法规",
    "行政法规": "法规",
    "规章": "法规",
    "文件": "文件",
    "技术标准": "技术标准",
    "标准": "技术标准",
    "其他": "其他",
    "技术资料": "技术资料",
    "教材": "教材",
    "参考资料": "参考资料",
    "参考文献": "参考资料",
}
_SKIP_SECTIONS = {"技术资料", "教材", "参考资料"}
_PUBLICATION = re.compile(r"NCRP|ICRP|UNSCEAR|教材|技术资料|培训教材", re.IGNORECASE)
_CODE = re.compile(
    r"(?:GBZ?|HJ|GA|WS|JJG)\s*/?\s*T?\s*\d+(?:\.\d+)?\s*[-—]\s*\d{4}",
    re.IGNORECASE,
)
_DOC_NO = re.compile(r"第\s*\d+\s*号|〔\d{4}〕|\[\d{4}\]|令第\s*\d+")
_LEGAL_TAIL = ("法", "条例", "规定", "办法", "细则", "决定", "法典")


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
    section: str = ""


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
    section = ""
    for block in blocks[caption_at + 1 :]:
        if not block.table_path or block.table_path[0].table != top:
            if started:
                break
            continue
        started = True
        raw = block.text or ""
        leading = len(raw) - len(raw.lstrip())
        text = raw.strip()
        if not text:
            continue
        heading = _SECTIONS.get(clean_title(text))
        if heading and len(clean_title(text)) <= 8:
            section = heading
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
                section=section,
            )
        )
    return entries


def designated_basis(text: str) -> str | None:
    """项目预定依据的标准号或法律名。只用于把它们从「超出依据」里分开，不带限值。"""
    compact = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    if "GB18871" in compact:
        return "GB 18871"
    if "生态环境法典" in (text or ""):
        return "生态环境法典"
    return None


def is_non_normative(name: str, text: str, section: str = "") -> bool:
    """没有标准号、文号，也不像法律或行政法规标题的条目。教材、技术资料、NCRP 报告走这里。"""
    if section in _SKIP_SECTIONS:
        return True
    blob = f"{name}\n{text}"
    if _CODE.search(blob):
        return False
    if _PUBLICATION.search(blob):
        return True
    if _DOC_NO.search(blob):
        return False
    title = clean_title(name)
    if any(title.endswith(suffix) for suffix in _LEGAL_TAIL):
        return False
    return True


def repeal_fingerprint(rows: list[RepealRow]) -> str:
    from app.hashing import stable_hash

    payload = [
        {"name": row.name, "repealed_on": row.repealed_on.isoformat(), "replaced_by": row.replaced_by}
        for row in sorted(rows, key=lambda item: item.name)
    ]
    return stable_hash({"rows": payload})


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
