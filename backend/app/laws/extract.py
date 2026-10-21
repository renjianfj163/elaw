import io
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class TextExtractError(Exception):
    pass


def extract_text(filename: str, data: bytes) -> str:
    lower = filename.lower()
    if lower.endswith(".docx"):
        text = extract_docx(data)
    elif lower.endswith(".pdf"):
        text = extract_pdf(data)
    elif lower.endswith(".txt") or lower.endswith(".md"):
        text = extract_plain(data)
    else:
        raise TextExtractError("只接受 PDF、DOCX、TXT")
    text = text.strip()
    if not text:
        raise TextExtractError("没有抽出正文。扫描件请先做成可复制文本后再上传")
    return text


def extract_plain(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise TextExtractError("文本编码无法识别")


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
