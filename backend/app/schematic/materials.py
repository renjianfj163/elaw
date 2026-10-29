"""从产品定义 Word 和 Datasheet PDF 抽出文字。

抽出的全文可以再删成与图上器件相关的章节。
图中文字会接在文字层后面，一并进入可引用摘录。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.schematic.office_text import TextExtractError, extract_docx, extract_pdf
from app.parse.convert import ConvertError, convert_doc_to_docx
from app.public_trial import trial_caps
from app.schematic.review import MAX_BASIS_CHARS, MAX_PDF_BYTES


class MaterialError(Exception):
    pass


def extract_material(filename: str, data: bytes, role: str, *, read_figures: bool = False) -> dict:
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
    cap = MAX_PDF_BYTES if trial_caps() is None else trial_caps()[1]
    if len(data) > cap:
        raise MaterialError(f"文件超过 {cap // (1024 * 1024)}MB")
    text = _read(name, suffix, data).strip()
    figures = _figures(data) if role == "datasheet" and read_figures else None
    block = ""
    if figures is not None:
        from app.schematic.datasheet_figures import quotable_figure_block

        block = quotable_figure_block(figures)
        if block:
            text = f"{text}\n\n{block}".strip() if text else block
    if not text and not _has_figure_note(figures):
        raise MaterialError("没有抽出正文。扫描件请先做成可复制文本后再上传")
    body = {
        "role": role,
        "filename": name,
        "text": text,
        "chars": len(text),
        "limit": MAX_BASIS_CHARS,
        "too_long": len(text) > MAX_BASIS_CHARS,
    }
    if figures is not None:
        body["figures"] = [item.model_dump() for item in figures.pages if item.text.strip()]
        body["figure_pages"] = [item.page for item in figures.pages]
        body["figure_status"] = figures.status
        body["figure_note"] = figures.note
    return body


def _figures(data: bytes):
    from app.schematic.datasheet_figures import figure_reader_or_none, read_datasheet_figures

    reader, reason = figure_reader_or_none()
    return read_datasheet_figures(data, reader=reader, skip_reason=reason)


def _has_figure_note(figures) -> bool:
    return figures is not None and bool(figures.note or any(item.text.strip() for item in figures.pages))


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
