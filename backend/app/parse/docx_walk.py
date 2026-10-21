"""按文档顺序遍历 docx，保留嵌套表和图片位置。

不使用 python-docx 的 cell.text：那个接口只拼单元格里的直接段落，
嵌套表里的文字会整段丢掉。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, Field

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V = "{urn:schemas-microsoft-com:vml}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_HEADING = re.compile(
    r"^(?P<num>\d+\.\d+(?:\.\d+)*)(?:[ \t\u3000]+|[、:：])?(?P<title>[\u4e00-\u9fff].*)?$"
)
_UNIT_TITLE = re.compile(
    r"^(天|日|倍|次|年|小时|分钟|秒|人|台|个|层|间|cm|mm|m|mSv|μSv|uSv|Sv|μGy|Gy)$",
    re.IGNORECASE,
)


class ParseError(Exception):
    pass


class TableStep(BaseModel):
    table: int
    row: int
    col: int


class Block(BaseModel):
    id: str
    type: Literal["para", "cell", "image"]
    text: str
    section_path: str
    table_path: list[TableStep]
    char_start: int
    char_end: int
    has_image: bool
    not_parsed: bool = False
    media_file: str | None = None


class ParseStats(BaseModel):
    table_count: int
    top_level_table_count: int
    nested_table_count: int
    image_block_count: int
    media_file_count: int


class ParseResult(BaseModel):
    source_name: str
    docx_sha256: str
    full_text: str
    stats: ParseStats
    blocks: list[Block] = Field(default_factory=list)


@dataclass
class _State:
    section_path: str = ""
    blocks: list[dict] = field(default_factory=list)
    table_count: int = 0
    top_level_table_count: int = 0
    image_block_count: int = 0


def parse_docx(path: Path) -> ParseResult:
    data = path.read_bytes()
    return parse_docx_bytes(data, source_name=path.name)


def parse_docx_bytes(data: bytes, source_name: str) -> ParseResult:
    from app.hashing import sha256_bytes

    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ParseError("DOCX 无法读取") from exc
    try:
        try:
            document_xml = package.read("word/document.xml")
        except KeyError as exc:
            raise ParseError("DOCX 缺少 word/document.xml") from exc
        except zipfile.BadZipFile as exc:
            raise ParseError("DOCX 无法读取") from exc
        rels = _load_rels(package)
        media_files = [name for name in package.namelist() if name.startswith("word/media/")]
    finally:
        package.close()

    root = ElementTree.fromstring(document_xml)
    body = root.find(W + "body")
    if body is None:
        raise ParseError("DOCX 缺少正文")

    state = _State()
    _walk_container(body, [], state, rels)
    full_text = _assign_offsets(state.blocks)
    blocks = [Block.model_validate(item) for item in state.blocks]
    stats = ParseStats(
        table_count=state.table_count,
        top_level_table_count=state.top_level_table_count,
        nested_table_count=state.table_count - state.top_level_table_count,
        image_block_count=state.image_block_count,
        media_file_count=len(media_files),
    )
    return ParseResult(
        source_name=source_name,
        docx_sha256=sha256_bytes(data),
        full_text=full_text,
        stats=stats,
        blocks=blocks,
    )


def write_blocks(result: ParseResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def heading_number(text: str) -> str | None:
    """识别单元格或段落开头的标题编号，如 7.3.1、10.1.2。

    单独的小数（如 2.5、74.0天）不是标题。带句号的长句也不是标题。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 80 or "\n" in stripped or "。" in stripped:
        return None
    matched = _HEADING.fullmatch(stripped)
    if not matched:
        return None
    number = matched.group("num")
    title = matched.group("title")
    parts = number.split(".")
    if any(not part.isdigit() or str(int(part)) != part or int(part) == 0 for part in parts):
        return None
    if title is None and len(parts) < 3:
        return None
    if title and _UNIT_TITLE.fullmatch(title):
        return None
    return number


def _load_rels(package: zipfile.ZipFile) -> dict[str, str]:
    try:
        xml = package.read("word/_rels/document.xml.rels")
    except KeyError:
        return {}
    root = ElementTree.fromstring(xml)
    mapping: dict[str, str] = {}
    for rel in root:
        if rel.tag != REL_NS + "Relationship":
            continue
        rel_id = rel.get("Id")
        target = rel.get("Target")
        if not rel_id or not target:
            continue
        mapping[rel_id] = _zip_media_path(target)
    return mapping


def _zip_media_path(target: str) -> str:
    target = target.replace("\\", "/").split("?")[0]
    if target.startswith("/"):
        return target.lstrip("/")
    while target.startswith("../"):
        target = target[3:]
    if target.startswith("word/"):
        return target
    return "word/" + target.lstrip("./")


def _walk_container(el: ElementTree.Element, table_path: list[tuple[int, int, int]], state: _State, rels: dict[str, str]) -> None:
    sibling_table = 0
    for child in _iter_blocks(el):
        if child.tag == W + "tbl":
            sibling_table += 1
            _walk_table(child, table_path, sibling_table, state, rels)
        elif child.tag == W + "p":
            _emit_paragraph(child, table_path, state, rels)
            for box in _text_boxes(child):
                _walk_container(box, table_path, state, rels)
        elif child.tag == W + "txbxContent":
            _walk_container(child, table_path, state, rels)


def _walk_table(
    table: ElementTree.Element,
    parent_path: list[tuple[int, int, int]],
    sibling_index: int,
    state: _State,
    rels: dict[str, str],
) -> None:
    state.table_count += 1
    if not parent_path:
        state.top_level_table_count += 1
    for row_index, row in enumerate(_iter_rows(table), start=1):
        for col_index, cell in enumerate(_iter_cells(row), start=1):
            cell_path = parent_path + [(sibling_index, row_index, col_index)]
            _walk_container(cell, cell_path, state, rels)


def _emit_paragraph(
    paragraph: ElementTree.Element,
    table_path: list[tuple[int, int, int]],
    state: _State,
    rels: dict[str, str],
) -> None:
    text = _paragraph_text(paragraph)
    pictures = _pictures(paragraph, rels)
    if not text and not pictures:
        return
    number = heading_number(text) if text else None
    if number:
        state.section_path = number
    if text:
        state.blocks.append(
            {
                "type": "cell" if table_path else "para",
                "text": text,
                "section_path": state.section_path,
                "table_path": [_step(item) for item in table_path],
                "has_image": bool(pictures),
                "not_parsed": False,
                "media_file": None,
            }
        )
    for media in pictures:
        state.image_block_count += 1
        state.blocks.append(
            {
                "type": "image",
                "text": "",
                "section_path": state.section_path,
                "table_path": [_step(item) for item in table_path],
                "has_image": True,
                "not_parsed": True,
                "media_file": media,
            }
        )


def _step(item: tuple[int, int, int]) -> dict[str, int]:
    return {"table": item[0], "row": item[1], "col": item[2]}


def _assign_offsets(blocks: list[dict]) -> str:
    parts: list[str] = []
    cursor = 0
    sequence = 0
    for block in blocks:
        sequence += 1
        block["id"] = f"b{sequence:06d}"
        text = block["text"]
        if not text:
            block["char_start"] = cursor
            block["char_end"] = cursor
            continue
        if parts:
            parts.append("\n")
            cursor += 1
        block["char_start"] = cursor
        parts.append(text)
        cursor += len(text)
        block["char_end"] = cursor
    return "".join(parts)


def _iter_blocks(el: ElementTree.Element):
    for child in list(el):
        if child.tag == MC + "AlternateContent":
            chosen = child.find(MC + "Choice")
            if chosen is None:
                chosen = child.find(MC + "Fallback")
            if chosen is not None:
                yield from _iter_blocks(chosen)
            continue
        if child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _iter_blocks(content)
            continue
        if child.tag == W + "customXml":
            yield from _iter_blocks(child)
            continue
        if child.tag in {W + "p", W + "tbl", W + "txbxContent"}:
            yield child


def _iter_rows(table: ElementTree.Element):
    for child in list(table):
        if child.tag == W + "tr":
            yield child
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _iter_rows(content)
        elif child.tag == MC + "AlternateContent":
            chosen = child.find(MC + "Choice")
            if chosen is None:
                chosen = child.find(MC + "Fallback")
            if chosen is not None:
                yield from _iter_rows(chosen)


def _iter_cells(row: ElementTree.Element):
    for child in list(row):
        if child.tag == W + "tc":
            yield child
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                yield from _iter_cells(content)
        elif child.tag == MC + "AlternateContent":
            chosen = child.find(MC + "Choice")
            if chosen is None:
                chosen = child.find(MC + "Fallback")
            if chosen is not None:
                yield from _iter_cells(chosen)


def _visible_children(el: ElementTree.Element):
    for child in list(el):
        if child.tag == W + "del":
            continue
        if child.tag == MC + "AlternateContent":
            chosen = child.find(MC + "Choice")
            if chosen is None:
                chosen = child.find(MC + "Fallback")
            if chosen is not None:
                yield from _visible_children(chosen)
            continue
        yield child


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    chunks: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        for child in _visible_children(node):
            if child.tag in {W + "drawing", W + "pict", W + "txbxContent"}:
                continue
            if child.tag == W + "t":
                chunks.append(child.text or "")
            elif child.tag == W + "tab":
                chunks.append("\t")
            elif child.tag in {W + "br", W + "cr"}:
                chunks.append("\n")
            else:
                walk(child)

    walk(paragraph)
    return "".join(chunks)


def _pictures(paragraph: ElementTree.Element, rels: dict[str, str]) -> list[str | None]:
    found: list[str | None] = []

    def walk(node: ElementTree.Element) -> None:
        for child in _visible_children(node):
            if child.tag in {W + "drawing", W + "pict"}:
                if _is_graphic(child):
                    found.append(_media_target(child, rels))
                continue
            if child.tag == W + "txbxContent":
                continue
            walk(child)

    walk(paragraph)
    return found


def _text_boxes(paragraph: ElementTree.Element):
    found = []

    def walk(node: ElementTree.Element) -> None:
        for child in _visible_children(node):
            if child.tag == W + "txbxContent":
                found.append(child)
                continue
            if child.tag in {W + "drawing", W + "pict"}:
                walk(child)
                continue
            walk(child)

    walk(paragraph)
    return found


def _is_graphic(node: ElementTree.Element) -> bool:
    for child in node.iter():
        if child.tag in {A + "blip", V + "imagedata", W + "binData"}:
            return True
        if child.get(R + "embed") or child.get(R + "id") or child.get(R + "link"):
            return True
    return False


def _media_target(node: ElementTree.Element, rels: dict[str, str]) -> str | None:
    for child in node.iter():
        for attr in (R + "embed", R + "id", R + "link"):
            rel_id = child.get(attr)
            if rel_id and rel_id in rels:
                return rels[rel_id]
    return None
