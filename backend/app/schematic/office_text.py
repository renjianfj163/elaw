"""从产品定义 Word 和 Datasheet PDF 抽出文字层。"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class TextExtractError(Exception):
    pass


def extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            xml = package.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise TextExtractError("DOCX 无法读取") from exc
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for paragraph in root.iter(WORD_NS + "p"):
        chunks = [node.text or "" for node in paragraph.iter(WORD_NS + "t")]
        paragraphs.append("".join(chunks))
    return "\n".join(paragraphs)


def extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise TextExtractError("无法从 PDF 抽出正文") from exc
    return "\n".join(pages)
