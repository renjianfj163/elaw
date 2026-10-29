"""识读 Datasheet 里缺字或图很多的页。

文字层仍由 pypdf 抽取。这里只处理几乎没有可复制文字、或内嵌图较多的页。
读到的图中文字会写入可引用摘录，并标明来自图。
"""

from __future__ import annotations

import io
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pypdf import PdfReader, PdfWriter
from pypdf.generic import IndirectObject

from app.hashing import sha256_bytes
from app.llm.provider import MemoryCache, cache_key

THIN_CHARS = 80
MANY_IMAGES = 3
MAX_FIGURE_PAGES = 16
PROMPT_VERSION = "datasheet-figures-1"
_CACHE = MemoryCache()

Reason = Literal["little_text", "many_images"]
FigureStatus = Literal["none", "skipped", "read", "failed"]


class FigureReadError(RuntimeError):
    pass


class FigureLine(BaseModel):
    page: int
    text: str = ""


class FigureBatch(BaseModel):
    pages: list[FigureLine] = Field(default_factory=list)


class FigurePage(BaseModel):
    page: int
    reason: Reason
    text: str = ""


class FigureRead(BaseModel):
    pages: list[FigurePage] = Field(default_factory=list)
    note: str = ""
    status: FigureStatus = "none"


class _Selected(BaseModel):
    page: int
    reason: Reason
    layer: str = ""
    images: int = 0


def page_reason(text: str, image_count: int) -> str:
    """空白页不送去识图。有图但几乎没字，或图很多的页，才需要看图。"""
    chars = len((text or "").strip())
    images = max(0, int(image_count))
    if chars == 0 and images == 0:
        return ""
    if chars < THIN_CHARS:
        return "little_text"
    if images >= MANY_IMAGES:
        return "many_images"
    return ""


def select_figure_pages(data: bytes) -> list[_Selected]:
    reader = PdfReader(io.BytesIO(data))
    found: list[_Selected] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        images = _image_count(page)
        reason = page_reason(text, images)
        if not reason:
            continue
        found.append(_Selected(page=index, reason=reason, layer=text, images=images))  # type: ignore[arg-type]
    thin = [item for item in found if item.reason == "little_text"]
    busy = [item for item in found if item.reason == "many_images"]
    thin.sort(key=lambda item: (len(item.layer.strip()), item.page))
    busy.sort(key=lambda item: (-item.images, item.page))
    return (thin + busy)[:MAX_FIGURE_PAGES]


def subset_pdf(data: bytes, pages: list[int]) -> bytes:
    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    total = len(reader.pages)
    for number in pages:
        if number < 1 or number > total:
            raise FigureReadError("页码超出手册范围")
        writer.add_page(reader.pages[number - 1])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def read_datasheet_figures(
    data: bytes,
    *,
    reader: Callable[[bytes, list[int]], list[FigureLine]] | None = None,
    skip_reason: str = "",
) -> FigureRead:
    try:
        selected = select_figure_pages(data)
    except Exception:
        return FigureRead(status="failed", note="没有读出页面。文字层仍保留，图未识别。")
    if not selected:
        return FigureRead(status="none")
    if reader is None:
        reason = skip_reason.strip() or "没有识图。"
        return FigureRead(
            pages=[_public(item) for item in selected],
            status="skipped",
            note=f"有 {len(selected)} 页几乎没有可复制文字，或图比较多。{reason}",
        )
    numbers = [item.page for item in selected]
    try:
        packet = subset_pdf(data, numbers)
        lines = reader(packet, numbers)
    except Exception:
        return FigureRead(
            pages=[_public(item) for item in selected],
            status="failed",
            note="这些页的图没有识别成功。文字层仍保留，可以继续引用。",
        )
    filled = _keep_novel(selected, _align_pages(lines, numbers))
    if not filled:
        return FigureRead(
            status="read",
            note="看过这些图。读出的字都已经在文字层里，摘录沿用文字层。",
        )
    return FigureRead(
        pages=filled,
        status="read",
        note=f"识图读到 {len(filled)} 页图中文字，已写入摘录，审查可以引用。",
    )


def render_ocr(figures: FigureRead) -> str:
    blocks: list[str] = []
    for item in figures.pages:
        body = item.text.strip()
        if not body:
            continue
        label = "几乎没有可复制文字" if item.reason == "little_text" else "图比较多"
        blocks.append(f"第 {item.page} 页（图中文字，{label}）\n{body}")
    return "\n\n".join(blocks)


def quotable_figure_block(figures: FigureRead) -> str:
    """图中文字并进手册正文。标题含「引脚说明」「典型应用」，裁剪时会保留。"""
    body = render_ocr(figures)
    if not body:
        return ""
    return f"引脚说明与典型应用（图中文字）\n{body}"


def figure_reader_or_none(
    cache: MemoryCache | None = None,
) -> tuple[Callable[[bytes, list[int]], list[FigureLine]] | None, str]:
    from app.llm.provider import load_llm_config
    from app.llm.runtime_env import runtime_env

    cfg = load_llm_config()
    if not cfg.get("outbound_allowed"):
        return None, "外发未开启，所以没有把这些页送去识图。"
    provider = str(cfg.get("provider") or "gemini")
    if provider not in {"", "gemini"}:
        return None, "当前模型不是 Gemini，所以没有识图。"
    if not runtime_env("GEMINI_API_KEY"):
        return None, "未配置 GEMINI_API_KEY，所以没有识图。"
    return gemini_figure_reader(cfg, cache or _CACHE), ""


def gemini_figure_reader(cfg: dict, cache: MemoryCache) -> Callable[[bytes, list[int]], list[FigureLine]]:
    def read(pdf_bytes: bytes, pages: list[int]) -> list[FigureLine]:
        return transcribe_figures(
            pdf_bytes,
            pages,
            complete=lambda payload, page_numbers: _gemini_complete(cfg, payload, page_numbers),
            model=str(cfg.get("model") or "gemini-3.5-flash"),
            cache=cache,
            retries=int(cfg.get("retries", 2)),
            use_cache=bool(cfg.get("cache", True)),
        )

    return read


def transcribe_figures(
    pdf_bytes: bytes,
    pages: list[int],
    *,
    complete: Callable[[bytes, list[int]], str],
    model: str,
    cache: MemoryCache,
    retries: int = 2,
    use_cache: bool = True,
) -> list[FigureLine]:
    payload = {"pdf_sha256": sha256_bytes(pdf_bytes), "pages": list(pages)}
    key = cache_key(model, PROMPT_VERSION, payload)
    if use_cache:
        hit = cache.get(key)
        if isinstance(hit, dict):
            return FigureBatch.model_validate(hit).pages
    attempts = max(1, int(retries)) + 1
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            raw = complete(pdf_bytes, pages)
            parsed = FigureBatch.model_validate(json.loads(raw))
            if use_cache:
                cache.put(key, parsed.model_dump())
            return parsed.pages
        except Exception as exc:
            last_error = exc
            continue
    raise FigureReadError("图未识别") from last_error


def _gemini_complete(cfg: dict, pdf_bytes: bytes, pages: list[int]) -> str:
    from app.llm.endpoint import gemini_proxy_base
    from app.llm.runtime_env import runtime_env
    from app.schematic.gemini_client import GeminiCallError

    api_key = runtime_env("GEMINI_API_KEY")
    if not api_key:
        raise FigureReadError("未配置 GEMINI_API_KEY")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise FigureReadError("未安装 google-genai") from exc
    base = gemini_proxy_base(cfg)
    options = types.HttpOptions(base_url=base, timeout=180_000) if base else types.HttpOptions(timeout=180_000)
    client = genai.Client(api_key=api_key, http_options=options)
    folder = tempfile.TemporaryDirectory(prefix="datasheet-figures-")
    uploaded = None
    try:
        path = Path(folder.name) / "pages.pdf"
        path.write_bytes(pdf_bytes)
        uploaded = client.files.upload(file=str(path))
        uploaded = _wait_ready(client, uploaded)
        response = client.models.generate_content(
            model=str(cfg.get("model") or "gemini-3.5-flash"),
            contents=[uploaded, _user_prompt(pages)],
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM,
                response_mime_type="application/json",
                response_schema=FigureBatch,
                max_output_tokens=int(cfg.get("max_tokens") or 8192),
            ),
        )
    except FigureReadError:
        raise
    except Exception as exc:
        text = str(exc).strip() or "识图没有返回结果"
        if api_key:
            text = text.replace(api_key, "***")
        raise GeminiCallError(text[:300]) from exc
    finally:
        name = getattr(uploaded, "name", None) if uploaded is not None else None
        if name:
            try:
                client.files.delete(name=name)
            except Exception:
                pass
        folder.cleanup()
    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise FigureReadError("识图没有返回 JSON")
    return text


_SYSTEM = """你只抄 PDF 图里能看见的文字，例如引脚名、网络名、电压标注和典型应用图上的句子。
不要编造看不见的内容，不要计算，不要比较数值，不要把整页正文再抄一遍。
看不清的页 text 留空字符串。page 使用用户给出的原页码。
只输出 JSON。"""


def _user_prompt(pages: list[int]) -> str:
    mapping = "，".join(f"子集第 {index} 页对应原第 {number} 页" for index, number in enumerate(pages, start=1))
    listed = "、".join(str(number) for number in pages)
    return (
        f"原页码依次是：{listed}。{mapping}。"
        "page 填原页码。text 只抄该页图里看得见的字。没有就留空字符串。"
    )


def _wait_ready(client, uploaded, attempts: int = 30):
    import time

    for _ in range(attempts):
        state = getattr(uploaded, "state", None)
        name = getattr(state, "name", None) if state is not None else None
        label = str(name or state or "")
        if label in {"", "ACTIVE", "FileState.ACTIVE"}:
            return uploaded
        if label == "FAILED":
            raise FigureReadError("手册页面上传后未能处理")
        time.sleep(1)
        file_name = getattr(uploaded, "name", None)
        if not file_name:
            return uploaded
        uploaded = client.files.get(name=file_name)
    raise FigureReadError("手册页面上传后长时间未能处理")


def _align_pages(lines: list[FigureLine], pages: list[int]) -> list[FigureLine]:
    originals = set(pages)
    aligned: list[FigureLine] = []
    for line in lines:
        text = line.text.strip()[:4000]
        if not text:
            continue
        if line.page in originals:
            aligned.append(FigureLine(page=line.page, text=text))
            continue
        if 1 <= line.page <= len(pages):
            aligned.append(FigureLine(page=pages[line.page - 1], text=text))
    return aligned


def _keep_novel(selected: list[_Selected], lines: list[FigureLine]) -> list[FigurePage]:
    layers = {item.page: item for item in selected}
    found: list[FigurePage] = []
    seen: set[int] = set()
    for line in lines:
        slot = layers.get(line.page)
        if slot is None or line.page in seen:
            continue
        if _compact(line.text) and _compact(line.text) in _compact(slot.layer):
            continue
        seen.add(line.page)
        found.append(FigurePage(page=slot.page, reason=slot.reason, text=line.text.strip()))
    found.sort(key=lambda item: item.page)
    return found


def _public(item: _Selected) -> FigurePage:
    return FigurePage(page=item.page, reason=item.reason)


def _image_count(page) -> int:
    try:
        return _count_images(page["/Resources"], depth=0, seen=set())
    except Exception:
        return 0


def _count_images(resources, depth: int, seen: set[int]) -> int:
    if resources is None or depth > 2:
        return 0
    if isinstance(resources, IndirectObject):
        resources = resources.get_object()
    marker = id(resources)
    if marker in seen:
        return 0
    seen.add(marker)
    xobjects = resources.get("/XObject") if hasattr(resources, "get") else None
    if xobjects is None:
        return 0
    if isinstance(xobjects, IndirectObject):
        xobjects = xobjects.get_object()
    count = 0
    for value in xobjects.values():
        obj = value.get_object() if isinstance(value, IndirectObject) else value
        subtype = str(obj.get("/Subtype")) if hasattr(obj, "get") else ""
        if subtype == "/Image":
            count += 1
        elif subtype == "/Form":
            count += _count_images(obj.get("/Resources") if hasattr(obj, "get") else None, depth + 1, seen)
    return count


def _compact(text: str) -> str:
    return "".join(text.split())
