"""国家标准只查公开检索页上有没有这个标准号。不下载全文。"""

import re

SEARCH_URL = "https://openstd.samr.gov.cn/bzgk/std/std_list"


def metadata_visible(html: str, query: str) -> bool:
    compact = re.sub(r"\s+", "", html or "")
    needle = re.sub(r"\s+", "", query or "")
    return bool(needle) and needle in compact


def collect_std(seed_name: str, query: str, http) -> dict:
    error = ""
    visible = False
    try:
        html = http.get_text(SEARCH_URL, {"p.p1": "0", "p.p2": query})
        visible = metadata_visible(html, query)
    except Exception as exc:
        error = str(exc)
    if not visible:
        return {
            "seed_name": seed_name,
            "source": "std",
            "outcome": "未找到",
            "fulltext": "需人工上传",
            "raw_text": "",
            "raw_status": None,
            "publish_date": None,
            "effective_date": None,
            "version_label": None,
            "superseded_note": None,
            "detail": {
                "query": query,
                "error": error,
                "note": "公开检索页没有看到该标准号，或页面暂时打不开。全文请人工上传。",
            },
        }
    return {
        "seed_name": seed_name,
        "source": "std",
        "outcome": "找到",
        "fulltext": "需人工上传",
        "raw_text": "",
        "raw_status": None,
        "publish_date": None,
        "effective_date": None,
        "version_label": query,
        "superseded_note": None,
        "detail": {
            "query": query,
            "note": "只确认检索页出现了标准号。全文未下载，请人工上传并核对年份与修改单。",
        },
    }
