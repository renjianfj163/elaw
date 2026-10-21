"""国家法律法规数据库公开检索。不访问内网地址，不猜测状态码的含义。"""

from datetime import date
from urllib.parse import urlparse

from app.laws.extract import TextExtractError, extract_docx
from app.laws.titles import clean_title, titles_match

LIST_URL = "https://flk.npc.gov.cn/law-search/search/list"
BATCH_URL = "https://flk.npc.gov.cn/law-search/download/batch"
ALLOWED_FILE_HOSTS = {
    "flk.npc.gov.cn",
    "wb.flk.npc.gov.cn",
    "flkoss.obs-bj2.cucloud.cn",
}


def search_payload(name: str, page: int) -> dict:
    return {
        "searchRange": 1,
        "sxrq": [],
        "gbrq": [],
        "searchType": 2,
        "sxx": [],
        "gbrqYear": [],
        "flfgCodeId": [],
        "zdjgCodeId": [],
        "searchContent": name,
        "orderByParam": {"order": "-1", "sort": ""},
        "pageNum": page,
        "pageSize": 20,
    }


def public_file_url(item: dict) -> str | None:
    raw = (item or {}).get("url") or ""
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_FILE_HOSTS:
        return None
    return raw


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    day = str(value).strip()[:10]
    try:
        year, month, day_no = (int(part) for part in day.split("-"))
        return date(year, month, day_no)
    except (TypeError, ValueError):
        return None


def pick_exact(seed_name: str, rows: list[dict]) -> tuple[dict | None, list[str]]:
    exact = []
    near = []
    for row in rows:
        title = clean_title(row.get("title") or "")
        if titles_match(seed_name, title):
            exact.append(row)
        elif seed_name and clean_title(seed_name) in title:
            near.append(title)
    exact.sort(key=lambda row: row.get("gbrq") or "", reverse=True)
    return (exact[0] if exact else None), near


def collect_flk(seed_name: str, http, pages: int = 3) -> dict:
    rows: list[dict] = []
    error = ""
    try:
        for page in range(1, pages + 1):
            payload = http.post_json(LIST_URL, search_payload(seed_name, page))
            batch = payload.get("rows") or []
            rows.extend(batch)
            total = int(payload.get("total") or 0)
            if not batch or page * 20 >= total:
                break
    except Exception as exc:
        error = str(exc)
        rows = []
    chosen, near = pick_exact(seed_name, rows)
    if chosen is None:
        return {
            "seed_name": seed_name,
            "source": "flk",
            "outcome": "未找到",
            "fulltext": "不适用",
            "raw_text": "",
            "raw_status": None,
            "publish_date": None,
            "effective_date": None,
            "version_label": None,
            "superseded_note": None,
            "detail": {"near_misses": near[:8], "error": error},
        }
    text = ""
    fulltext = "需人工上传"
    file_error = ""
    try:
        listed = http.post_json(BATCH_URL, [{"bbbs": chosen.get("bbbs"), "format": "docx"}])
        files = listed.get("data") or []
        url = public_file_url(files[0]) if files else None
        if url:
            blob = http.get_bytes(url)
            text = extract_docx(blob).strip()
            fulltext = "已获取" if text else "需人工上传"
        else:
            file_error = "公开文件地址不在允许的主机上，已忽略内网或未知地址"
    except (TextExtractError, Exception) as exc:
        file_error = str(exc)
        text = ""
        fulltext = "需人工上传"
    note = None
    exact_count = sum(1 for row in rows if titles_match(seed_name, row.get("title") or ""))
    if exact_count > 1:
        note = "检索到多条标题精确匹配的记录，暂以公布日最新的一条待确认，请人工核对替代关系。"
    return {
        "seed_name": seed_name,
        "source": "flk",
        "outcome": "找到",
        "fulltext": fulltext,
        "raw_text": text,
        "raw_status": None if chosen.get("sxx") is None else str(chosen.get("sxx")),
        "publish_date": parse_day(chosen.get("gbrq")),
        "effective_date": parse_day(chosen.get("sxrq")),
        "version_label": clean_title(chosen.get("title") or seed_name),
        "superseded_note": note,
        "issuer": chosen.get("zdjgName"),
        "detail": {
            "near_misses": near[:8],
            "bbbs": chosen.get("bbbs"),
            "file_error": file_error,
            "raw_status_note": "状态码是接口原值，程序不解释为有效或废止。",
        },
    }
