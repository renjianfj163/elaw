"""从产品定义 Word 和 Datasheet PDF 抽出文字。

抽出的全文可以再删成与图上器件相关的章节。审查仍只接收文本，不把整份文件直接送进模型。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.laws.extract import TextExtractError, extract_docx, extract_pdf
from app.parse.convert import ConvertError, convert_doc_to_docx
from app.schematic.review import MAX_BASIS_CHARS, MAX_PDF_BYTES


class MaterialError(Exception):
    pass


def extract_material(filename: str, data: bytes, role: str) -> dict:
    name = Path(filename or "").name or "upload"
    suffix = Path(name).suffix.lower()
    if role == "product":
        if suffix not in {".doc", ".docx"}:
            raise MaterialError("产品定义需要是 Word（.doc 或 .docx）")
    elif role == "datasheet":
        if suffix != ".pdf":
            raise MaterialError("Datasheet 需要是 PDF")
    else:
        raise MaterialError("资料类型无法识别")
    if not data:
        raise MaterialError("文件是空的")
    if len(data) > MAX_PDF_BYTES:
        raise MaterialError("文件超过 40MB")
    text = _read(name, suffix, data).strip()
    if not text:
        raise MaterialError("没有抽出正文。扫描件请先做成可复制文本后再上传")
    return {
        "role": role,
        "filename": name,
        "text": text,
        "chars": len(text),
        "limit": MAX_BASIS_CHARS,
        "too_long": len(text) > MAX_BASIS_CHARS,
    }


def _read(name: str, suffix: str, data: bytes) -> str:
    try:
        if suffix == ".pdf":
            return extract_pdf(data)
        if suffix == ".docx":
            return extract_docx(data)
        return _read_doc(name, data)
    except (TextExtractError, ConvertError) as exc:
        raise MaterialError(str(exc)) from exc


def _read_doc(name: str, data: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="material-") as folder:
        folder_path = Path(folder)
        source = folder_path / name
        source.write_bytes(data)
        docx_path = convert_doc_to_docx(source, folder_path / "out")
        return extract_docx(docx_path.read_bytes())
