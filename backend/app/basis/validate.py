"""入库前核对条数、章节顺序和 sha256。不写死条号或限值。"""

from __future__ import annotations

import re

from app.hashing import sha256_text
from app.laws.splitter import cn_to_int

_PART = re.compile(r"第([零一二三四五六七八九十百千万0-9]+)编")
_CHAPTER = re.compile(r"第([零一二三四五六七八九十百千万0-9]+)章")
_SECTION = re.compile(r"第([零一二三四五六七八九十百千万0-9]+)节")
_ARTICLE = re.compile(r"第([零一二三四五六七八九十百千万0-9]+)条")


class BasisValidationError(Exception):
    pass


def validate_pieces(kind: str, pieces: list[dict], *, expect_count: int | None = None) -> dict:
    if not pieces:
        raise BasisValidationError("没有切出任何条款")
    paths = [piece["path"] for piece in pieces]
    if len(paths) != len(set(paths)):
        raise BasisValidationError("条款路径重复，不能入库")
    for piece in pieces:
        if sha256_text(piece["text"]) != piece.get("text_sha256", sha256_text(piece["text"])):
            raise BasisValidationError(f"sha256 对不上：{piece['path']}")
    if kind == "国家标准":
        _assert_standard_order(paths)
    elif kind == "法律":
        _assert_legal_order(paths)
    counted = _count_for_kind(kind, pieces)
    if expect_count is not None and counted != expect_count:
        raise BasisValidationError(f"条数不符：切出 {counted}，期望 {expect_count}")
    return {"clause_count": len(pieces), "counted": counted}


def attach_hashes(pieces: list[dict]) -> list[dict]:
    hashed = []
    for ordinal, piece in enumerate(pieces):
        text = piece["text"]
        hashed.append(
            {
                "ordinal": ordinal,
                "path": piece["path"],
                "text": text,
                "text_sha256": sha256_text(text),
            }
        )
    return hashed


def _count_for_kind(kind: str, pieces: list[dict]) -> int:
    if kind == "法律":
        return len({_number(_ARTICLE, piece["path"]) for piece in pieces if _ARTICLE.search(piece["path"])})
    return len(pieces)


def _assert_legal_order(paths: list[str]) -> None:
    part_no = None
    chapter_no = None
    section_no = None
    article_no = None
    current_part = None
    current_chapter = None
    for path in paths:
        part = _label(_PART, path)
        chapter = _label(_CHAPTER, path)
        section = _label(_SECTION, path)
        article = _number(_ARTICLE, path)
        if part != current_part:
            current_part = part
            part_value = _number(_PART, path) if part else None
            if part_no is not None and part_value is not None and part_value < part_no:
                raise BasisValidationError(f"编的顺序颠倒：{path}")
            if part_value is not None:
                part_no = part_value
            chapter_no = None
            section_no = None
            current_chapter = None
        if chapter != current_chapter:
            current_chapter = chapter
            chapter_value = _number(_CHAPTER, path) if chapter else None
            if chapter_no is not None and chapter_value is not None and chapter_value < chapter_no:
                raise BasisValidationError(f"章的顺序颠倒：{path}")
            if chapter_value is not None:
                chapter_no = chapter_value
            section_no = None
        section_value = _number(_SECTION, path) if section else None
        if section_value is not None:
            if section_no is not None and section_value < section_no:
                raise BasisValidationError(f"节的顺序颠倒：{path}")
            section_no = section_value
        if article is None:
            continue
        if article_no is not None and article < article_no:
            raise BasisValidationError(f"条的顺序颠倒：{path}")
        article_no = article


def _assert_standard_order(paths: list[str]) -> None:
    previous = None
    for path in paths:
        current = _clause_key(path)
        if previous is not None and not _key_le(previous, current):
            raise BasisValidationError(f"条款号顺序颠倒：{path}")
        if previous == current:
            raise BasisValidationError(f"条款号重复：{path}")
        previous = current


_APPENDIX_PATH = re.compile(r"^([A-Z])\.?(\d+(?:\.\d+)*)$")


def _clause_key(path: str) -> tuple:
    if path[0].isalpha():
        matched = _APPENDIX_PATH.match(path)
        if matched is None:
            raise BasisValidationError(f"无法识别条款号：{path}")
        letter, rest = matched.group(1), matched.group(2)
        return (1, letter, tuple(int(part) for part in rest.split(".")))
    return (0, "", tuple(int(part) for part in path.split(".")))


def _key_le(left: tuple, right: tuple) -> bool:
    if left[0] != right[0]:
        return left[0] < right[0]
    if left[1] != right[1]:
        return left[1] < right[1]
    left_nums, right_nums = left[2], right[2]
    shared = min(len(left_nums), len(right_nums))
    if left_nums[:shared] != right_nums[:shared]:
        return left_nums[:shared] < right_nums[:shared]
    return len(left_nums) <= len(right_nums)


def _label(pattern: re.Pattern[str], path: str) -> str | None:
    matched = pattern.search(path)
    if not matched:
        return None
    return matched.group(0)


def _number(pattern: re.Pattern[str], path: str) -> int | None:
    matched = pattern.search(path)
    if not matched:
        return None
    return cn_to_int(matched.group(1))
