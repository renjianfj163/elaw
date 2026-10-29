"""在线试用。密钥只留在服务器环境变量里，不进入页面，也不接受访客改写。"""

from __future__ import annotations

import os
import threading
import time

NOTICE = (
    "这是在线试用。上传的原理图和资料会发到这台服务器，并送去 Gemini。"
    "请不要上传保密图纸。审查结果是草稿，需要人工复核，不构成设计签核。"
    "试用密钥留在服务器上，页面里看不到。"
)

TRIAL_FILES = 8
TRIAL_PDF_BYTES = 15 * 1024 * 1024
TRIAL_TOTAL_BYTES = 40 * 1024 * 1024
_WINDOW_SECONDS = 60 * 60

_PATH_LIMITS = {
    "/api/schematic/review": ("ELAW_TRIAL_REVIEW_LIMIT", 4),
    "/api/schematic/extract": ("ELAW_TRIAL_EXTRACT_LIMIT", 40),
    "/api/schematic/datasheets/resolve": ("ELAW_TRIAL_RESOLVE_LIMIT", 20),
    "/api/schematic/datasheets/upload": ("ELAW_TRIAL_UPLOAD_LIMIT", 10),
    "/api/schematic/knowledge/import": ("ELAW_TRIAL_IMPORT_LIMIT", 10),
    "/api/schematic/knowledge/review": ("ELAW_TRIAL_KNOWLEDGE_LIMIT", 10),
    "/api/llm/ping": ("ELAW_TRIAL_PING_LIMIT", 8),
}

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}
_review_slots = threading.BoundedSemaphore(2)


def enabled() -> bool:
    return os.environ.get("ELAW_PUBLIC", "") == "1"


def trial_caps() -> tuple[int, int, int] | None:
    if not enabled():
        return None
    return TRIAL_FILES, TRIAL_PDF_BYTES, TRIAL_TOTAL_BYTES


def limits_payload() -> dict:
    return {
        "max_files": TRIAL_FILES,
        "max_pdf_mb": TRIAL_PDF_BYTES // (1024 * 1024),
        "max_total_mb": TRIAL_TOTAL_BYTES // (1024 * 1024),
    }


def oversized_detail(size: int) -> str:
    caps = trial_caps()
    if caps is None or size <= caps[1]:
        return ""
    return f"在线试用的单个文件不超过 {caps[1] // (1024 * 1024)}MB"


def reset_for_tests() -> None:
    global _review_slots
    with _lock:
        _hits.clear()
        _review_slots = threading.BoundedSemaphore(2)


def client_ip(request) -> str:
    if os.environ.get("ELAW_TRUST_PROXY", "") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:80] or "unknown"
    client = request.client
    host = client.host if client is not None else ""
    return (host or "unknown")[:80]


def _cap(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 1:
        return 1
    return min(value, 1000)


def allow(bucket: str, limit: int, window: float = _WINDOW_SECONDS) -> bool:
    now = time.monotonic()
    with _lock:
        recent = [stamp for stamp in _hits.get(bucket, []) if now - stamp < window]
        if len(recent) >= limit:
            _hits[bucket] = recent
            return False
        recent.append(now)
        _hits[bucket] = recent
        return True


def rate_detail(request) -> str:
    if not enabled() or request.method != "POST":
        return ""
    rule = _PATH_LIMITS.get(request.url.path)
    if rule is None:
        return ""
    env_name, default = rule
    if allow(f"{client_ip(request)}:{request.url.path}", _cap(env_name, default)):
        return ""
    return "这次试用的次数够多了，请过一会儿再来。"


def take_review_slot() -> bool:
    return _review_slots.acquire(blocking=False)


def release_review_slot() -> None:
    _review_slots.release()
