import io
import zipfile
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.hashing import result_hash
from app.laws.catalog import catalog_csv, parse_catalog
from app.laws.collect import run_collection
from app.laws.collectors.flk import pick_exact, public_file_url
from app.laws.collectors.mee import exact_links
from app.laws.collectors.std import metadata_visible
from app.laws.seeds import load_seeds
from app.laws.splitter import split_legal, split_standard
from app.laws.titles import titles_match
from app.llm.provider import MemoryCache, build_request, generate_json
from app.main import app
from app.models import Base, LawVersion
from app.rules.evaluator import RuleEngineRefusal, evaluate, judge
from app.scope import judge_scope
from app.verify.citation import evidence_matches, explain_rejection, quote_matches, version_in_force


def test_titles_do_not_swallow_longer_names():
    assert titles_match("环境保护法", "中华人民共和国环境保护法")
    assert not titles_match("环境保护法", "中华人民共和国海洋环境保护法")


def test_pick_exact_keeps_near_misses():
    rows = [
        {"title": "中华人民共和国<em>环境保护法</em>", "gbrq": "2014-04-24"},
        {"title": "中华人民共和国海洋环境保护法", "gbrq": "2023-10-24"},
    ]
    chosen, near = pick_exact("环境保护法", rows)
    assert "环境保护法" in chosen["title"]
    assert "海洋" not in chosen["title"]
    assert near


def test_public_file_url_rejects_private_host():
    assert public_file_url({"url": "http://172.16.220.27/a.docx"}) is None
    assert public_file_url({"urlIn": "http://10.0.0.1/a.docx", "url": "http://10.0.0.1/a.docx"}) is None
    allowed = "https://flkoss.obs-bj2.cucloud.cn/prod/a.docx"
    assert public_file_url({"url": allowed, "urlIn": "http://172.16.220.27/a.docx"}) == allowed


def test_mee_and_std_parsers():
    html = '<a href="/xxgk/a.html">建设项目环境影响评价分类管理名录</a>'
    assert exact_links(html, "建设项目环境影响评价分类管理名录")
    assert not exact_links("<p>建设项目环境影响评价分类管理名录</p>", "建设项目环境影响评价分类管理名录")
    assert metadata_visible("标准号 GB 18871-2002 出现在检索页", "GB 18871-2002")
    assert not metadata_visible("没有标准号", "GB 18871-2002")


def test_seed_list_is_not_marked_in_force():
    seeds = load_seeds()
    names = {item["name"] for item in seeds}
    assert "中华人民共和国生态环境法典" in names
    assert any("GB 18871-2002" in item["name"] for item in seeds)
    assert all("status" not in item for item in seeds)


def test_split_legal_and_standard():
    text = "\n".join(
        [
            "第一章 总则",
            "第一条 为了测试切分，写下一句完整的话。",
            "",
            "本条第二款单独成段。",
            "（一）第一项示例内容。",
            "（二）第二项示例内容。",
            "第二条 另一条的正文。",
        ]
    )
    pieces = split_legal(text)
    paths = [item["path"] for item in pieces]
    assert "第一章/第一条/第一款" in paths
    assert "第一章/第一条/第二款/第一项" in paths
    assert "第一章/第二条/第一款" in paths
    standard = split_standard("4.1 总要求\n应当设置处理设施。\n4.2.1 细节条款\n数值另行规定。")
    assert [item["path"] for item in standard] == ["4.1", "4.2.1"]


def test_catalog_needs_human_review():
    rows = parse_catalog("项目类别\t报告书\t报告表\t登记表\n示例医院\t床位较多\t其他\t—\n")
    assert rows[0]["needs_human_review"] is True
    assert "示例医院" in catalog_csv(rows)


def test_three_valued_logic_and_censored_numbers():
    unknown = evaluate(
        {"and": [{"==": [{"var": "has_inpatient"}, True]}, {"==": [{"var": "discharge_to"}, "municipal_sewer"]}]},
        {"has_inpatient": True},
    )
    assert unknown["result"] == "unknown"
    assert "discharge_to" in unknown["missing"]

    either = evaluate(
        {"or": [{"==": [{"var": "has_boiler"}, True]}, {"==": [{"var": "has_generator"}, True]}]},
        {"has_boiler": True},
    )
    assert either["result"] == "true"

    item = {
        "kind": "numeric",
        "status": "active",
        "applies_when": {"==": [{"var": "discharge_to"}, "municipal_sewer"]},
        "required_params": ["COD_mg_per_L"],
        "requirement": {"<=": [{"var": "COD_mg_per_L"}, {"value": 250, "unit": "mg/L"}]},
    }
    low = judge(item, {"discharge_to": "municipal_sewer", "COD_mg_per_L": {"cmp": "<", "value": 10, "unit": "mg/L"}})
    assert low["verdict"] == "符合"
    unclear = judge(
        item,
        {"discharge_to": "municipal_sewer", "COD_mg_per_L": {"cmp": "<", "value": 300, "unit": "mg/L"}},
    )
    assert unclear["verdict"] == "无法判定"
    missing = judge(item, {"discharge_to": "municipal_sewer"})
    assert missing["verdict"] == "无法判定"
    skipped = judge(item, {"discharge_to": "river", "COD_mg_per_L": 10})
    assert skipped["verdict"] == "不适用"
    converted = judge(
        item,
        {
            "discharge_to": "municipal_sewer",
            "COD_mg_per_L": {"value": 0.2, "unit": "g/L", "fact_id": "f1"},
        },
    )
    assert converted["verdict"] == "符合"
    with pytest.raises(RuleEngineRefusal):
        judge({"kind": "qualitative", "status": "active", "requirement": {"==": [1, 1]}}, {})
    with pytest.raises(RuleEngineRefusal):
        judge({"kind": "numeric", "status": "draft", "requirement": {"==": [1, 1]}}, {})


def test_citation_and_version_window():
    article = "医疗机构排放的水污染物应当符合本标准。"
    assert quote_matches("应当符合本标准", article)
    assert quote_matches("应当 符合\n本标准", article)
    assert not quote_matches("可以超过本标准", article)
    assert not quote_matches("   ", article)
    text = "床位数为800张。"
    assert evidence_matches(text, "800张", text.index("800张"), text.index("800张") + 4)
    assert not evidence_matches(text, "500张", 0, 3)
    day = date(2026, 10, 21)
    assert version_in_force(
        effective_date=date(2026, 8, 15),
        repeal_date=None,
        status="有效",
        reference_date=day,
        verified_at=datetime(2026, 10, 1),
    )
    assert not version_in_force(
        effective_date=date(2026, 8, 15),
        repeal_date=None,
        status="待确认",
        reference_date=day,
        verified_at=None,
    )
    reason = explain_rejection(
        quote="并不存在的句子",
        article_text=article,
        evidence=[],
        in_force=True,
    )
    assert reason


def test_scope_defaults_to_out():
    assert judge_scope("消防") == "OUT_OF_SCOPE"
    assert judge_scope("医疗废物") == "IN_SCOPE"
    assert judge_scope("随便问问") == "OUT_OF_SCOPE"


def test_result_hash_is_stable():
    first = result_hash(rule_pack_hash="p", params={"a": 1}, article_sha="s", output={"verdict": "无法判定"})
    second = result_hash(rule_pack_hash="p", params={"a": 1}, article_sha="s", output={"verdict": "无法判定"})
    assert first == second


def test_llm_cache_replaces_temperature():
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    seen = []

    def transport(body):
        seen.append(body)
        if len(seen) == 1:
            return "不是 json"
        return '{"verdict": "无法判定"}'

    cfg = {
        "model": "deepseek-v4-flash",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "max_tokens": 8192,
        "response_format": "json_object",
        "retries": 2,
        "cache": True,
        "prompt_version": "test",
    }
    body = build_request(cfg, "请输出 JSON")
    assert "temperature" not in body
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    cache = MemoryCache()
    first = generate_json(Out, "请输出 JSON", transport=transport, cache=cache, config=cfg)
    second = generate_json(Out, "请输出 JSON", transport=transport, cache=cache, config=cfg)
    assert first["output"]["verdict"] == "无法判定"
    assert second["cache_hit"] is True
    assert len(seen) == 2


def _docx(text: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", xml)
    return buffer.getvalue()


class FakeHttp:
    def __init__(self) -> None:
        self.downloads: list[str] = []

    def post_json(self, url, payload):
        if url.endswith("/list"):
            return {
                "total": 2,
                "rows": [
                    {
                        "title": "中华人民共和国环境保护法",
                        "gbrq": "2014-04-24",
                        "sxrq": "2015-01-01",
                        "sxx": 1,
                        "bbbs": "abc",
                        "zdjgName": "全国人民代表大会常务委员会",
                    },
                    {"title": "中华人民共和国海洋环境保护法", "gbrq": "2023-10-24", "sxx": 1, "bbbs": "other"},
                ],
            }
        if url.endswith("/batch"):
            return {
                "data": [
                    {
                        "urlIn": "http://172.16.220.27:38080/secret.docx",
                        "url": "https://flkoss.obs-bj2.cucloud.cn/prod/a.docx",
                    }
                ]
            }
        raise AssertionError(url)

    def get_bytes(self, url):
        self.downloads.append(url)
        return _docx("第一条 为了保护和改善环境，制定本法。")

    def get_text(self, url, params=None):
        return ""


def test_collection_stores_unverified_version():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db: Session = make_session_factory(engine)()
    http = FakeHttp()
    saved = run_collection(db, http, names=["中华人民共和国环境保护法"], pause_seconds=0)
    assert saved[0]["outcome"] == "找到"
    assert saved[0]["raw_status"] == "1"
    assert http.downloads and http.downloads[0].startswith("https://")
    version = db.get(LawVersion, saved[0]["version_id"])
    assert version.status == "待确认"
    assert version.verified_at is None
    version.status = "有效"
    version.verified_by = "测试"
    version.verified_at = datetime(2026, 10, 21)
    db.commit()
    version.raw_text = "被改写"
    with pytest.raises(PermissionError):
        db.commit()
    db.rollback()


def test_upload_and_scope_api():
    client = TestClient(app)
    scope = client.get("/api/scope", params={"topic": "用地"})
    assert scope.json()["result"] == "OUT_OF_SCOPE"
    response = client.post(
        "/api/laws/upload",
        data={"name": "示例文本", "level": "法律", "confirm": "false", "by": ""},
        files={"file": ("demo.txt", "第一条 这是用来测试上传的正文。".encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "待确认"
    listed = client.get("/api/reports")
    match = next(item for item in listed.json()["items"] if item["seed_name"] == "示例文本")
    assert match["version_status"] == "待确认"
    assert match["verified_by"] is None
    health = client.get("/api/health")
    assert "不构成法律意见" in health.json()["disclaimer"]
