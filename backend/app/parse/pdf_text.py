"""把带文字层的 PDF 拆成段落 block。

不重建表格，不 OCR。整页没有可复制文字时记一个未解析的图片 block。
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader

from app.parse.docx_walk import (
    Block,
    ParseError,
    ParseResult,
    ParseStats,
    _assign_offsets,
    heading_number,
)

PDF_PARSER_VERSION = "pdf-text-1"
PDF_PARSE_NOTE = (
    "PDF 只读取了可复制的文字。表格没有按单元格拆开，图片里的字未识别。"
    "评价依据表、表 7-2 这类按单元格核对的项目可能不会出现。"
)
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])")
_SPACE_RUN = re.compile(r"[ \t\u3000]+")


def parse_pdf(path: Path) -> ParseResult:
    return parse_pdf_bytes(path.read_bytes(), source_name=path.name)


def parse_pdf_bytes(data: bytes, source_name: str) -> ParseResult:
    from app.hashing import sha256_bytes

    pages = _page_texts(data)
    raw: list[dict] = []
    section_path = ""
    image_pages = 0
    for text in pages:
        lines = _lines(text)
        if not lines:
            image_pages += 1
            raw.append(_image_block(section_path))
            continue
        for line in lines:
            number = heading_number(line)
            if number:
                section_path = number
            raw.append(
                {
                    "type": "para",
                    "text": line,
                    "section_path": section_path,
                    "table_path": [],
                    "has_image": False,
                    "not_parsed": False,
                    "media_file": None,
                }
            )
    if not any(block["type"] == "para" for block in raw):
        raise ParseError("这份 PDF 没有可复制的文字。扫描件请先另存为 .doc 或 .docx。")
    full_text = _assign_offsets(raw)
    stats = ParseStats(
        table_count=0,
        top_level_table_count=0,
        nested_table_count=0,
        image_block_count=image_pages,
        media_file_count=0,
    )
    return ParseResult(
        source_name=source_name,
        docx_sha256=sha256_bytes(data),
        full_text=full_text,
        stats=stats,
        blocks=[Block.model_validate(item) for item in raw],
        parser_version=PDF_PARSER_VERSION,
        parse_note=PDF_PARSE_NOTE,
    )


def _page_texts(data: bytes) -> list[str]:
    if not data.startswith(b"%PDF"):
        raise ParseError("PDF 无法读取")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("PDF 有密码，请先解除密码再上传")
        return [(page.extract_text() or "") for page in reader.pages]
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("PDF 无法读取") from exc


def _lines(text: str) -> list[str]:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    text = _CJK_GAP.sub("", text)
    lines = []
    for line in text.split("\n"):
        cleaned = _SPACE_RUN.sub(" ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _image_block(section_path: str) -> dict:
    return {
        "type": "image",
        "text": "",
        "section_path": section_path,
        "table_path": [],
        "has_image": True,
        "not_parsed": True,
        "media_file": None,
    }
