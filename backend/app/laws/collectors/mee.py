"""生态环境部公开网页检索。解析不到就未找到，不猜测正文。"""

import re
from urllib.parse import urljoin

from app.laws.titles import titles_match

SEARCH_URL = "https://www.mee.gov.cn/was5/web/search"
HOST = "www.mee.gov.cn"
_LINK = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def exact_links(html: str, name: str) -> list[str]:
    found = []
    for href, inner in _LINK.findall(html or ""):
        text = _TAG.sub("", inner)
        if not titles_match(name, text):
            continue
        if href.startswith("http"):
            if HOST not in href:
                continue
            found.append(href.split("#")[0])
        elif href.startswith("/"):
            found.append(urljoin("https://www.mee.gov.cn", href))
    return list(dict.fromkeys(found))


def collect_mee(seed_name: str, http) -> dict:
    error = ""
    links: list[str] = []
    try:
        html = http.get_text(SEARCH_URL, {"searchword": seed_name, "perpage": "10"})
        links = exact_links(html, seed_name)
    except Exception as exc:
        error = str(exc)
    if not links:
        return {
            "seed_name": seed_name,
            "source": "mee",
            "outcome": "未找到",
            "fulltext": "不适用",
            "raw_text": "",
            "raw_status": None,
            "publish_date": None,
            "effective_date": None,
            "version_label": None,
            "superseded_note": None,
            "detail": {"error": error, "note": "未在公开检索结果中看到精确标题，请人工上传。"},
        }
    return {
        "seed_name": seed_name,
        "source": "mee",
        "outcome": "找到",
        "fulltext": "需人工上传",
        "raw_text": "",
        "raw_status": None,
        "publish_date": None,
        "effective_date": None,
        "version_label": seed_name,
        "superseded_note": None,
        "detail": {
            "links": links[:5],
            "note": "只记录了公开页面上的标题链接，正文请下载后人工上传并核对。",
        },
    }
