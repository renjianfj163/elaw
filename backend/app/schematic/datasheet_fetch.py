"""按料号查找 Datasheet PDF，验真后才写入本机缓存。

顺序是：本机缓存，Nexar 直链，Jina 搜索里的 PDF 链接，再试大厂固定下载地址。
不解析厂商网页。下载结果必须是 PDF，首页文字要对得上料号，并通过验真后才保存。
"""

from __future__ import annotations

import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel
from pypdf import PdfReader

from app.db import data_dir
from app.hashing import sha256_bytes, sha256_text
from app.llm.provider import MemoryCache, cache_key
from app.schematic.mpn import NormalizedMpn, normalize_mpn
from app.schematic.review import MAX_BASIS_CHARS, MAX_PDF_BYTES

VERIFY_PROMPT = "datasheet-verify-1"
_PREFERRED = (
    "ti.com",
    "st.com",
    "analog.com",
    "nxp.com",
    "infineon.com",
    "renesas.com",
    "microchip.com",
    "onsemi.com",
    "diodes.com",
    "richtek.com",
    "alldatasheet.com",
    "datasheetspdf.com",
    "lcsc.com",
    "szlcsc.com",
)
_PDF_URL = re.compile(r"https?://[^\s<>\"')\]]+?\.pdf(?:\?[^\s<>\"')\]]*)?", re.IGNORECASE)
_TI_URL = re.compile(r"https?://(?:www\.)?ti\.com/lit/gpn/[a-z0-9_-]+", re.IGNORECASE)
_DOC_ZH = {
    "Errata": "勘误表",
    "User Manual": "用户手册",
    "Application Note": "应用笔记",
    "Other": "其他文件",
}
_VENDOR_URL = {
    "ti": "https://www.ti.com/lit/gpn/{part}",
    "st": "https://www.st.com/resource/en/datasheet/{part}.pdf",
    "adi": "https://www.analog.com/media/en/technical-documentation/data-sheets/{part}.pdf",
    "nxp": "https://www.nxp.com/docs/en/data-sheet/{part}.pdf",
}
_NEXAR_TOKEN = "https://identity.nexar.com/connect/token"
_NEXAR_GRAPHQL = "https://api.nexar.com/graphql"
_NEXAR_QUERY = """
query SupSearch($q: String!) {
  supSearch(q: $q, limit: 3) {
    results {
      item {
        mpn
        datasheets { url }
      }
    }
  }
}
"""
_VERIFY_SYSTEM = """你只判断一份 PDF 首页文本是不是目标芯片的官方 Datasheet（数据手册）。
不要审查电路。看不清、对不上型号，或只是勘误、用户手册、应用笔记时，判为不是。
document_type 只能是 Datasheet、Errata、User Manual、Application Note、Other。
matched_mpn 写封面上的型号。confidence 取 0 到 1。
只输出一个 JSON 对象。"""

_token_lock = threading.Lock()
_token_box = {"access_token": "", "expires_at": 0.0}


class DatasheetFetchError(Exception):
    pass


class DatasheetVerdict(BaseModel):
    is_valid_datasheet: bool = False
    matched_mpn: str = ""
    confidence: float = 0
    document_type: Literal["Datasheet", "Errata", "User Manual", "Application Note", "Other"] = "Other"


class DatasheetMatch(BaseModel):
    mpn: str
    base_mpn: str = ""
    status: Literal["official_api", "fetched", "manual", "missing", "rejected", "blocked"] = "missing"
    level: Literal["ok", "warn", "bad"] = "bad"
    label: str = "抓取失败，请手动补充 PDF"
    tier: str = ""
    confidence: float | None = None
    document_type: str = ""
    matched_mpn: str = ""
    sha256: str = ""
    note: str = ""
    preview_text: str = ""
    cached: bool = False


@dataclass
class HttpFetched:
    status: int
    content: bytes
    content_type: str = ""
    url: str = ""


@dataclass(frozen=True)
class DatasheetCredentials:
    nexar_client_id: str = ""
    nexar_client_secret: str = ""
    jina_api_key: str = ""


Verifier = Callable[[str, str], DatasheetVerdict]


def credentials_from_env() -> DatasheetCredentials:
    from app.llm.runtime_env import runtime_env

    return DatasheetCredentials(
        nexar_client_id=runtime_env("NEXAR_CLIENT_ID"),
        nexar_client_secret=runtime_env("NEXAR_CLIENT_SECRET"),
        jina_api_key=runtime_env("JINA_API_KEY"),
    )


def resolve_datasheets(
    mpns: list[str],
    *,
    refresh: bool = False,
    http=None,
    verifier: Verifier | None = None,
    store: DatasheetStore | None = None,
    allow_network: bool | None = None,
    credentials: DatasheetCredentials | None = None,
) -> list[DatasheetMatch]:
    ordered = _unique_mpns(mpns)
    if not ordered:
        return []
    network = _network_allowed() if allow_network is None else allow_network
    creds = credentials if credentials is not None else credentials_from_env()
    library = store if store is not None else DatasheetStore(data_dir() / "datasheets")
    checker = verifier
    if checker is None and network:
        checker = gemini_datasheet_verifier()
    own_http = http is None and network
    client = HttpxDatasheetHttp() if own_http else http
    try:
        if len(ordered) == 1:
            return [_safe_one(ordered[0], refresh, client, checker, library, network, creds)]
        workers = min(3, len(ordered))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_safe_one, mpn, refresh, client, checker, library, network, creds) for mpn in ordered
            ]
            return [item.result() for item in futures]
    finally:
        if own_http and client is not None:
            client.close()


def store_manual(
    mpn: str,
    data: bytes,
    *,
    verifier: Verifier | None = None,
    store: DatasheetStore | None = None,
    allow_verify: bool = False,
) -> DatasheetMatch:
    parsed = normalize_mpn(mpn)
    if not parsed.cleaned:
        raise DatasheetFetchError("没有识别出料号")
    pdf = _as_pdf(data)
    if pdf is None:
        raise DatasheetFetchError("请上传 PDF")
    library = store if store is not None else DatasheetStore(data_dir() / "datasheets")
    text = first_pages_text(pdf, pages=2)
    if text and not page_mentions(text, parsed):
        raise DatasheetFetchError("首页文字里没有这个料号")
    confidence = None
    document_type = ""
    matched = parsed.base or parsed.cleaned
    note = "人工上传。请对照封面确认型号。"
    if not text:
        note = "人工上传。这份 PDF 没有可复制的文字，请对照封面确认。"
    elif allow_verify and verifier is not None:
        verdict = _checked(verifier, text, parsed.cleaned)
        problem = verdict_problem(verdict, parsed) if verdict is not None else "验真没有完成"
        if problem:
            raise DatasheetFetchError(problem)
        confidence = _unit_confidence(verdict.confidence)
        document_type = verdict.document_type
        matched = verdict.matched_mpn or matched
        note = "人工上传，首页验真通过。建议再看一眼封面型号。"
    preview = text[:800]
    return library.put(
        parsed,
        pdf,
        tier="manual",
        confidence=confidence,
        document_type=document_type or "Datasheet",
        matched_mpn=matched,
        note=note,
        preview_text=preview,
    )


def first_pages_text(data: bytes, pages: int = 2, limit: int = 6000) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        return ""
    chunks: list[str] = []
    for page in list(reader.pages)[:pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks).strip()[:limit]


def page_mentions(text: str, parsed: NormalizedMpn) -> bool:
    hay = re.sub(r"[^A-Z0-9]", "", text.upper())
    return any(len(token) >= 6 and token in hay for token in parsed.needles)


def verdict_problem(verdict: DatasheetVerdict, parsed: NormalizedMpn) -> str:
    if not verdict.is_valid_datasheet:
        kind = _DOC_ZH.get(verdict.document_type, "")
        if kind:
            return f"下载到的是{kind}，不是 Datasheet，已丢弃"
        return "验真没有通过，已丢弃"
    if verdict.document_type != "Datasheet":
        kind = _DOC_ZH.get(verdict.document_type, "其他文件")
        return f"下载到的是{kind}，不是 Datasheet，已丢弃"
    if _unit_confidence(verdict.confidence) < 0.8:
        return "首页像目标型号，但匹配度不够，已丢弃"
    matched = re.sub(r"[^A-Z0-9]", "", verdict.matched_mpn.upper())
    if not _compatible(matched, parsed):
        return "首页型号和目标料号对不上，已丢弃"
    return ""


def parse_verdict(content: str) -> DatasheetVerdict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("验真结果不是 JSON 对象")
    kind = str(data.get("document_type") or "Other")
    if kind not in {"Datasheet", "Errata", "User Manual", "Application Note", "Other"}:
        kind = "Other"
    return DatasheetVerdict(
        is_valid_datasheet=bool(data.get("is_valid_datasheet")),
        matched_mpn=str(data.get("matched_mpn") or "").strip(),
        confidence=_unit_confidence(data.get("confidence")),
        document_type=kind,
    )


def public_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.endswith(".local"):
        return False
    if "." not in host and ":" not in host:
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast)


def gemini_datasheet_verifier(cache: MemoryCache | None = None) -> Verifier:
    store = cache if cache is not None else MemoryCache()
    lock = threading.Lock()

    def verify(page_text: str, mpn: str) -> DatasheetVerdict:
        from app.llm.endpoint import gemini_proxy_base, normalize_gemini_base
        from app.llm.provider import load_llm_config
        from app.llm.runtime_env import runtime_env
        from app.schematic.review import load_gemini_config
        cfg = load_llm_config()
        if not cfg.get("outbound_allowed"):
            raise DatasheetFetchError("尚未允许外发")
        api_key = runtime_env("GEMINI_API_KEY")
        if not api_key:
            raise DatasheetFetchError("未配置 GEMINI_API_KEY")
        model = str(load_gemini_config().get("model") or "gemini-3.5-flash")
        key = cache_key(model, VERIFY_PROMPT, {"mpn": mpn, "page": sha256_text(page_text)})
        with lock:
            hit = store.get(key) if cfg.get("cache", True) else None
        if isinstance(hit, dict) and hit.get("document_type"):
            return DatasheetVerdict.model_validate(hit)
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise DatasheetFetchError("未安装 google-genai") from exc
        base = gemini_proxy_base(cfg)
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(base_url=normalize_gemini_base(base), timeout=45_000) if base else None,
        )
        user = f"目标芯片料号: {mpn}\n下载到的 PDF 首页文本:\n{page_text[:4000]}"
        attempts = int(cfg.get("retries", 2)) + 1
        last_error = "验真没有完成"
        for _ in range(attempts):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=_VERIFY_SYSTEM,
                        response_mime_type="application/json",
                        response_schema=DatasheetVerdict,
                        max_output_tokens=512,
                    ),
                )
                raw = getattr(response, "text", None) or ""
                verdict = parse_verdict(raw)
            except Exception as exc:
                last_error = _safe_error(exc, api_key)
                continue
            if cfg.get("cache", True):
                with lock:
                    store.put(key, verdict.model_dump())
            return verdict
        raise DatasheetFetchError(last_error)

    return verify


class DatasheetStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.files = self.root / "files"
        self.index_path = self.root / "index.json"
        self._lock = threading.Lock()

    def get(self, parsed: NormalizedMpn) -> DatasheetMatch | None:
        with self._lock:
            payload = self._load()
            for key in parsed.search_order:
                entry = payload["entries"].get(key)
                if isinstance(entry, dict):
                    match = self._from_entry(parsed, entry, payload)
                    if match is not None:
                        return match
        return None

    def put(
        self,
        parsed: NormalizedMpn,
        pdf: bytes,
        *,
        tier: str,
        confidence: float | None,
        document_type: str,
        matched_mpn: str,
        note: str,
        preview_text: str,
    ) -> DatasheetMatch:
        digest = sha256_bytes(pdf)
        with self._lock:
            self.files.mkdir(parents=True, exist_ok=True)
            target = self.files / f"{digest}.pdf"
            if not target.is_file():
                temporary = target.with_suffix(".pdf.tmp")
                temporary.write_bytes(pdf)
                temporary.replace(target)
            payload = self._load()
            entry = {
                "mpn": parsed.cleaned,
                "base_mpn": parsed.base,
                "sha256": digest,
                "tier": tier,
                "confidence": confidence,
                "document_type": document_type,
                "matched_mpn": matched_mpn,
                "note": note,
                "bytes": len(pdf),
            }
            for key in parsed.search_order:
                payload["entries"][key] = entry
            self._write(payload)
        return self._match(parsed, entry, preview_text, cached=False)

    def file_for(self, digest: str) -> Path | None:
        if not re.fullmatch(r"[a-f0-9]{64}", digest or ""):
            return None
        path = (self.files / f"{digest}.pdf").resolve()
        root = self.files.resolve()
        if path.parent != root or not path.is_file():
            return None
        return path

    def text_for(self, digest: str) -> str:
        path = self.file_for(digest)
        if path is None:
            raise DatasheetFetchError("没有这份手册")
        text = first_pages_text(path.read_bytes(), pages=30, limit=MAX_BASIS_CHARS)
        if not text:
            raise DatasheetFetchError("没有抽出正文。扫描件请先做成可复制文本后再上传")
        return text

    def _from_entry(self, parsed: NormalizedMpn, entry: dict, payload: dict) -> DatasheetMatch | None:
        digest = str(entry.get("sha256") or "")
        path = self.file_for(digest)
        if path is None:
            for key in list(payload["entries"]):
                item = payload["entries"].get(key)
                if isinstance(item, dict) and item.get("sha256") == digest:
                    payload["entries"].pop(key, None)
            self._write(payload)
            return None
        preview = first_pages_text(path.read_bytes(), pages=2)[:800]
        return self._match(parsed, entry, preview, cached=True)

    def _match(self, parsed: NormalizedMpn, entry: dict, preview: str, *, cached: bool) -> DatasheetMatch:
        tier = str(entry.get("tier") or "")
        confidence = entry.get("confidence")
        level, label, status = _presentation(tier, confidence, cached)
        return DatasheetMatch(
            mpn=parsed.raw or parsed.cleaned,
            base_mpn=str(entry.get("base_mpn") or parsed.base),
            status=status,
            level=level,
            label=label,
            tier=tier,
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            document_type=str(entry.get("document_type") or ""),
            matched_mpn=str(entry.get("matched_mpn") or ""),
            sha256=str(entry.get("sha256") or ""),
            note=str(entry.get("note") or ""),
            preview_text=preview,
            cached=cached,
        )

    def _load(self) -> dict:
        if not self.index_path.is_file():
            return {"entries": {}}
        try:
            loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"entries": {}}
        if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), dict):
            return {"entries": {}}
        return loaded

    def _write(self, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.index_path)


class HttpxDatasheetHttp:
    def __init__(self) -> None:
        import httpx

        self._client = httpx.Client(
            timeout=httpx.Timeout(20.0, read=40.0),
            follow_redirects=True,
            headers={
                "User-Agent": "sch-check/1 (local datasheet lookup)",
                "Accept": "application/pdf,application/json;q=0.9,*/*;q=0.8",
            },
        )

    def close(self) -> None:
        self._client.close()

    def post_form(self, url: str, data: dict) -> dict:
        response = self._client.post(url, data=data)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise DatasheetFetchError("令牌响应无法识别")
        return body

    def post_json(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        response = self._client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise DatasheetFetchError("查询响应无法识别")
        return body

    def get_json(self, url: str, headers: dict | None = None) -> dict:
        response = self._client.get(url, headers=headers)
        response.raise_for_status()
        ctype = response.headers.get("content-type", "")
        if "json" in ctype.lower():
            body = response.json()
            if isinstance(body, dict):
                return body
            if isinstance(body, list):
                return {"data": body}
            return {"content": str(body)}
        return {"content": response.text}

    def get_bytes(self, url: str) -> HttpFetched:
        if not public_url(url):
            return HttpFetched(status=0, content=b"", url=url)
        chunks: list[bytes] = []
        total = 0
        with self._client.stream("GET", url) as response:
            final = str(response.url)
            if not public_url(final):
                return HttpFetched(status=0, content=b"", url=final)
            if response.status_code != 200:
                return HttpFetched(status=response.status_code, content=b"", url=final)
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    return HttpFetched(status=0, content=b"", url=final)
                chunks.append(chunk)
        return HttpFetched(
            status=200,
            content=b"".join(chunks),
            content_type="",
            url=final,
        )


def _safe_one(mpn, refresh, http, verifier, store, allow_network, credentials) -> DatasheetMatch:
    try:
        return _resolve_one(mpn, refresh, http, verifier, store, allow_network, credentials)
    except Exception:
        parsed = normalize_mpn(mpn)
        return _fail(parsed.raw or mpn, parsed.base, "missing", "查找没有完成")


def _resolve_one(mpn, refresh, http, verifier, store, allow_network, credentials) -> DatasheetMatch:
    parsed = normalize_mpn(mpn)
    if not parsed.cleaned:
        return _fail(str(mpn or "").strip() or "未识别", "", "missing", "没有识别出料号")
    if not refresh:
        hit = store.get(parsed)
        if hit is not None:
            return hit
    if not allow_network:
        return _fail(parsed.raw, parsed.base, "blocked", "尚未允许外发。打开模型设置里的「允许外发」后才会联网查找手册。")
    last_note = "没有找到可下载的 Datasheet"
    checked = 0
    attempts = 0
    for tier, url in _candidate_urls(parsed, http, credentials):
        if checked >= 3 or attempts >= 6:
            break
        attempts += 1
        pdf = _download_pdf(http, url)
        if pdf is None:
            last_note = "没有找到可下载的 Datasheet"
            continue
        checked += 1
        text = first_pages_text(pdf, pages=2)
        if not page_mentions(text, parsed):
            last_note = "首页文字里没有这个料号，已丢弃"
            continue
        verdict = _checked(verifier, text, parsed.cleaned)
        if verdict is None:
            last_note = "首页验真没有完成，没有保存这份文件"
            continue
        problem = verdict_problem(verdict, parsed)
        if problem:
            last_note = problem
            continue
        note = _success_note(tier, verdict.confidence)
        return store.put(
            parsed,
            pdf,
            tier=tier,
            confidence=_unit_confidence(verdict.confidence),
            document_type=verdict.document_type,
            matched_mpn=verdict.matched_mpn,
            note=note,
            preview_text=text[:800],
        )
    kept = store.get(parsed) if refresh else None
    if kept is not None:
        kept.note = "重新搜索没有找到更合适的文件，仍使用上次保存的手册。"
        kept.cached = True
        return kept
    status = "rejected" if last_note != "没有找到可下载的 Datasheet" else "missing"
    return _fail(parsed.raw, parsed.base, status, last_note)


def _candidate_urls(parsed: NormalizedMpn, http, credentials: DatasheetCredentials):
    seen: set[str] = set()
    if credentials.nexar_client_id and credentials.nexar_client_secret:
        for part in parsed.search_order:
            for url in _nexar_urls(http, credentials, part):
                if url not in seen:
                    seen.add(url)
                    yield "nexar", url
    for part in parsed.search_order:
        for url in _jina_urls(http, credentials, part):
            if url not in seen:
                seen.add(url)
                yield "jina", url
    template = _VENDOR_URL.get(parsed.vendor)
    if template and parsed.base:
        url = template.format(part=parsed.base.lower())
        if url not in seen:
            yield parsed.vendor, url


def _nexar_urls(http, credentials: DatasheetCredentials, part: str) -> list[str]:
    token = _nexar_token(http, credentials)
    if not token:
        return []
    try:
        body = http.post_json(
            _NEXAR_GRAPHQL,
            {"query": _NEXAR_QUERY, "variables": {"q": part}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    except Exception:
        return []
    results = body.get("data", {}).get("supSearch", {}).get("results", []) if isinstance(body, dict) else []
    found: list[str] = []
    if not isinstance(results, list):
        return []
    for row in results:
        if not isinstance(row, dict):
            continue
        sheets = row.get("item", {}).get("datasheets", [])
        if not isinstance(sheets, list):
            continue
        for sheet in sheets:
            if not isinstance(sheet, dict):
                continue
            url = str(sheet.get("url") or "").strip()
            if url and public_url(url) and url not in found:
                found.append(url)
            if len(found) >= 2:
                return found
    return found


def _nexar_token(http, credentials: DatasheetCredentials) -> str:
    import time

    now = time.time()
    with _token_lock:
        if _token_box["access_token"] and _token_box["expires_at"] > now + 30:
            return str(_token_box["access_token"])
    try:
        body = http.post_form(
            _NEXAR_TOKEN,
            {
                "grant_type": "client_credentials",
                "client_id": credentials.nexar_client_id,
                "client_secret": credentials.nexar_client_secret,
                "scope": "supply.read",
            },
        )
    except Exception:
        return ""
    token = str(body.get("access_token") or "").strip()
    if not token:
        return ""
    try:
        expires = int(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires = 3600
    with _token_lock:
        _token_box["access_token"] = token
        _token_box["expires_at"] = now + max(60, expires)
    return token


def _jina_urls(http, credentials: DatasheetCredentials, part: str) -> list[str]:
    headers = {"Accept": "application/json", "X-With-Generated-Alt": "false"}
    if credentials.jina_api_key:
        headers["Authorization"] = f"Bearer {credentials.jina_api_key}"
    target = "https://s.jina.ai/" + quote(f"{part} datasheet filetype:pdf", safe="")
    try:
        body = http.get_json(target, headers=headers)
    except Exception:
        return []
    return _rank_pdf_urls(_collect_text(body), part)[:2]


def _collect_text(body) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        return "\n".join(_collect_text(item) for item in body)
    if not isinstance(body, dict):
        return str(body)
    chunks = [str(body.get("content") or ""), str(body.get("url") or "")]
    data = body.get("data")
    if isinstance(data, list):
        chunks.extend(_collect_text(item) for item in data)
    elif data is not None:
        chunks.append(_collect_text(data))
    return "\n".join(chunks)


def _rank_pdf_urls(text: str, part: str) -> list[str]:
    found: list[str] = []
    for pattern in (_PDF_URL, _TI_URL):
        for url in pattern.findall(text):
            cleaned = url.rstrip(".,);")
            if cleaned not in found and public_url(cleaned):
                found.append(cleaned)
    needle = part.lower()

    def score(url: str) -> tuple:
        low = url.lower()
        compact = re.sub(r"[^a-z0-9]", "", low)
        has_part = 0 if needle in compact or needle in low else 1
        known = 0 if any(domain in low for domain in _PREFERRED) else 1
        if has_part and known:
            return (2, 1, len(url))
        return (has_part, known, len(url))

    ranked = [url for url in found if score(url)[0] < 2]
    ranked.sort(key=score)
    return ranked


def _download_pdf(http, url: str) -> bytes | None:
    if http is None or not public_url(url):
        return None
    try:
        fetched = http.get_bytes(url)
    except Exception:
        return None
    return _as_pdf(fetched.content if fetched.status == 200 else b"")


def _as_pdf(data: bytes) -> bytes | None:
    if not data or len(data) > MAX_PDF_BYTES:
        return None
    if not data.lstrip().startswith(b"%PDF"):
        return None
    return data


def _checked(verifier: Verifier | None, text: str, mpn: str) -> DatasheetVerdict | None:
    if verifier is None:
        return None
    try:
        return verifier(text, mpn)
    except Exception:
        return None


def _compatible(matched: str, parsed: NormalizedMpn) -> bool:
    tokens = [token for token in (parsed.cleaned, parsed.base, *parsed.needles) if token]
    if len(matched) < 6:
        return False
    if matched in tokens:
        return True
    if len(matched) < 8:
        return False
    return any(token.startswith(matched) or matched.startswith(token) for token in tokens)


def _unit_confidence(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1 and number <= 100:
        number = number / 100
    if number < 0:
        return 0
    if number > 1:
        return 1
    return number


def _success_note(tier: str, confidence) -> str:
    if tier == "nexar":
        return "来自 Nexar 直链，首页验真通过。建议再看一眼封面型号。"
    percent = int(round(_unit_confidence(confidence) * 100))
    return f"来自搜索或厂商链接，匹配度 {percent}%。请预览前两页，确认不是勘误表或别的型号。"


def _presentation(tier: str, confidence, cached: bool) -> tuple[str, str, str]:
    if tier == "nexar":
        label = "已匹配 Datasheet（本地缓存，官方 API）" if cached else "已自动匹配 Datasheet（官方 API）"
        return "ok", label, "official_api"
    if tier == "manual":
        label = "已手动补充（本地缓存）" if cached else "已手动补充"
        return "warn", label, "manual"
    percent = int(round(_unit_confidence(confidence) * 100))
    where = "本地缓存，" if cached else ""
    return "warn", f"已自动抓取（{where}匹配度 {percent}%）", "fetched"


def _fail(mpn: str, base: str, status: str, note: str) -> DatasheetMatch:
    if status == "blocked":
        return DatasheetMatch(mpn=mpn, base_mpn=base, status="blocked", level="warn", label="尚未允许外发，无法联网查找", note=note)
    return DatasheetMatch(mpn=mpn, base_mpn=base, status=status, level="bad", label="抓取失败，请手动补充 PDF", note=note)


def _unique_mpns(mpns: list[str]) -> list[str]:
    found: list[str] = []
    for item in mpns:
        text = " ".join(str(item or "").split())
        if text and text not in found:
            found.append(text)
        if len(found) >= 12:
            break
    return found


def _network_allowed() -> bool:
    try:
        from app.llm.provider import load_llm_config

        return bool(load_llm_config().get("outbound_allowed"))
    except Exception:
        return False


def _safe_error(exc: BaseException, api_key: str) -> str:
    text = str(exc).strip() or "验真没有完成"
    if api_key:
        text = text.replace(api_key, "***")
    return text[:300]


def reset_token_for_tests() -> None:
    with _token_lock:
        _token_box["access_token"] = ""
        _token_box["expires_at"] = 0.0
