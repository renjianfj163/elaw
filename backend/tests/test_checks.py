# 检查项：草稿不参与评估；数值走规则引擎；定性两遍并走缓存。

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.checks.cache import DbCache
from app.checks.evaluate import evaluate_checks
from app.checks.items import CheckItemView, activate_item, propose_draft, save_draft, view_from_row
from app.citation.check import BasisClauseView
from app.db import make_engine, make_session_factory
from app.hashing import new_id, sha256_text, utc_now
from app.llm.provider import MemoryCache
from app.models import Base, BasisClause, BasisDocument, LlmCache, ReviewLog
from app.parse.cli import parse_report
from app.parse.convert import ConvertError, find_soffice
from app.parse.docx_walk import parse_docx

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOC = REPO_ROOT / "data" / "samples" / "20241114-changgeng-radiotherapy.doc"
SAMPLE_DOCX = REPO_ROOT / "data" / "samples" / "m2"
AS_OF = date(2026, 10, 21)
CFG = {
    "model": "deepseek-v4-flash",
    "thinking": "enabled",
    "reasoning_effort": "high",
    "max_tokens": 8192,
    "response_format": "json_object",
    "retries": 2,
    "cache": True,
    "prompt_version": "test",
}


def test_draft_rejects_quote_and_number_that_are_not_in_the_clause():
    clause = _clause("职业工作人员的年剂量不超过3mSv。")
    calls = {"n": 0}

    def bad_quote(_body):
        calls["n"] += 1
        return json.dumps(_proposal(quote="可以超过9mSv", value=3))

    missed = propose_draft(clause, transport=bad_quote, cache=MemoryCache(), config=CFG)
    assert missed.item is None
    assert calls["n"] == 3
    assert "子串" in (missed.rejected_reason or "")

    def bad_number(_body):
        return json.dumps(_proposal(quote="不超过3mSv", value=99))

    invented = propose_draft(clause, transport=bad_number, cache=MemoryCache(), config=CFG)
    assert invented.item is None
    assert "数字" in (invented.rejected_reason or "")

    def then_good(_body):
        calls["good"] = calls.get("good", 0) + 1
        if calls["good"] < 3:
            return json.dumps(_proposal(quote="并不存在", value=3))
        return json.dumps(_proposal(quote="不超过3mSv", value=3))

    drafted = propose_draft(clause, transport=then_good, cache=MemoryCache(), config=CFG)
    assert drafted.item is not None
    assert drafted.item.status == "draft"
    assert drafted.item.quote in clause.text
    assert calls["good"] == 3


def test_draft_items_are_not_evaluated_until_a_person_activates_them():
    clause = _clause("应设置控制区和监督区的管理制度。", path="zone")
    draft = _item(clause, item_code="DRAFT-ZONE", status="draft", report_topics=["控制区", "监督区"])
    parsed = _parsed([{"text": "已设置控制区和监督区的管理制度。", "section": "10.1.2"}])
    skipped = evaluate_checks(
        parsed,
        items=[draft],
        clauses=[clause],
        as_of=AS_OF,
        transport=_boom,
        cache=MemoryCache(),
        config=CFG,
    )
    assert "DRAFT-ZONE" not in {row.item_code for row in skipped.findings}

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = make_session_factory(engine)()
    _store_clause(db, clause)
    row = save_draft(db, draft, by="审核人", note="先存草稿")
    db.commit()
    activated = activate_item(db, row.id, by="审核人", note="同意启用")
    db.commit()
    actions = list(
        db.scalars(select(ReviewLog.action).where(ReviewLog.target_id == row.id).order_by(ReviewLog.at, ReviewLog.id))
    )
    assert actions == ["draft", "activate"]
    assert activated.status == "active"
    assert view_from_row(activated).status == "active"


def test_numeric_item_uses_the_engine_and_missing_number_is_undecided():
    clause = _clause("职业工作人员的年剂量不超过3mSv。")
    item = _item(
        clause,
        item_code="DOSE",
        kind="numeric",
        report_topics=["剂量"],
        required_params=["reported_dose"],
        requirement={"<=": [{"var": "reported_dose"}, {"value": 3, "unit": "mSv"}]},
        quote="不超过3mSv",
    )
    present = _parsed([{"text": "剂量为1mSv。", "section": "7.3"}])
    met = evaluate_checks(present, items=[item], clauses=[clause], as_of=AS_OF, transport=_boom, cache=MemoryCache(), config=CFG)
    hit = _one(met, "DOSE")
    assert hit.verdict == "符合"
    assert hit.trace[-1]["op"] == "<="
    assert present.full_text[hit.char_start : hit.char_end] == hit.evidence_span

    over = _parsed([{"text": "剂量为9mSv。", "section": "7.3"}])
    exceeded = evaluate_checks(over, items=[item], clauses=[clause], as_of=AS_OF, transport=_boom, cache=MemoryCache(), config=CFG)
    assert _one(exceeded, "DOSE").verdict == "不符合"

    removed = _parsed([{"text": "剂量未给出。", "section": "7.3"}])
    missing = evaluate_checks(removed, items=[item], clauses=[clause], as_of=AS_OF, transport=_boom, cache=MemoryCache(), config=CFG)
    undecided = _one(missing, "DOSE")
    assert undecided.verdict == "无法判定"
    assert "reported_dose" in undecided.missing

    ambiguous = _parsed([{"text": "剂量为1mSv，另有9mSv。", "section": "7.3"}])
    many = evaluate_checks(ambiguous, items=[item], clauses=[clause], as_of=AS_OF, transport=_boom, cache=MemoryCache(), config=CFG)
    assert _one(many, "DOSE").verdict == "无法判定"


def test_outside_standard_and_figure_are_not_sent_to_the_model():
    clause = _clause("应设置联锁装置。", path="interlock")
    item = _item(clause, item_code="LOCK", report_topics=["联锁"], quote="应设置联锁装置")
    sentence = "控制区边界外30cm处的辐射剂量率应不大于2.5μSv/h（HJ1198-2021）。"
    parsed = _parsed(
        [
            {"text": sentence, "section": "7.3.3"},
            {"text": "联锁逻辑见图", "section": "8.1", "has_image": True},
            {
                "text": "",
                "type": "image",
                "section": "8.1",
                "not_parsed": True,
                "has_image": True,
                "media_file": "word/media/lock.wmf",
            },
        ]
    )
    result = evaluate_checks(
        parsed,
        items=[item],
        clauses=[clause],
        as_of=AS_OF,
        transport=_boom,
        cache=MemoryCache(),
        config=CFG,
    )
    outside = next(row for row in result.findings if "HJ1198" in row.evidence_span and "μSv/h" in row.evidence_span)
    assert outside.verdict == "超出依据范围"
    assert outside.kind == "out_of_scope"
    assert parsed.full_text[outside.char_start : outside.char_end] == outside.evidence_span
    figure = _one(result, "LOCK")
    assert figure.verdict == "无法判定"
    assert "图片" in figure.reason
    assert any(row.kind == "unparsed" and row.verdict == "无法判定" and row.section_path == "8.1" for row in result.findings)


def test_qualitative_disagreement_is_dual_review_and_cache_keeps_the_hash():
    clause = _clause("应设置控制区和监督区的管理制度。", path="zone")
    item = _item(
        clause,
        item_code="ZONE",
        kind="qualitative",
        report_topics=["控制区", "监督区"],
        quote="控制区和监督区的管理制度",
    )
    parsed = _parsed([{"text": "已设置控制区和监督区的管理制度。", "section": "10.1.2"}])
    span = "控制区和监督区的管理制度"
    prompts = []

    def transport(body):
        assert "temperature" not in body
        assert body["thinking"] == {"type": "enabled"}
        prompt = body["messages"][-1]["content"]
        prompts.append(prompt)
        if "换一个顺序" in prompt:
            assert "FIRST-PASS-TOKEN" not in prompt
            verdict = "不符合"
        else:
            verdict = "符合"
        return json.dumps({"verdict": verdict, "evidence_span": span, "reason": "FIRST-PASS-TOKEN"})

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = make_session_factory(engine)()
    cache = DbCache(db)
    runs = [
        evaluate_checks(parsed, items=[item], clauses=[clause], as_of=AS_OF, transport=transport, cache=cache, config=CFG)
        for _ in range(3)
    ]
    assert len(prompts) == 2
    assert prompts[0] != prompts[1]
    assert runs[0].result_hash == runs[1].result_hash == runs[2].result_hash
    finding = _one(runs[0], "ZONE")
    assert finding.verdict == "无法判定"
    assert finding.dual_review == "同源双审"
    assert finding.needs_review is True
    assert finding.review_note == "需人工复核"
    assert finding.reason == "两遍结论不一致"
    assert len(list(db.scalars(select(LlmCache)))) == 2

    def agree(_body):
        return json.dumps({"verdict": "符合", "evidence_span": span, "reason": ""})

    agreed = evaluate_checks(
        parsed,
        items=[item],
        clauses=[clause],
        as_of=AS_OF,
        transport=agree,
        cache=MemoryCache(),
        config=CFG,
    )
    same = _one(agreed, "ZONE")
    assert same.verdict == "符合"
    assert same.dual_review == "同源双审"
    assert same.needs_review is True
    assert same.review_note == "需人工复核"
    assert parsed.full_text[same.char_start : same.char_end] == span


def test_bad_evidence_is_rejected_instead_of_accepted():
    clause = _clause("应设置控制区和监督区的管理制度。", path="zone")
    item = _item(clause, item_code="ZONE", report_topics=["控制区"], quote="控制区和监督区")
    parsed = _parsed([{"text": "已设置控制区和监督区的管理制度。", "section": "10.1.2"}])

    def transport(_body):
        return json.dumps({"verdict": "有", "evidence_span": "报告里并没有这句话", "reason": ""})

    result = evaluate_checks(
        parsed,
        items=[item],
        clauses=[clause],
        as_of=AS_OF,
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    finding = _one(result, "ZONE")
    assert finding.verdict == "无法判定"
    assert finding.evidence_span == ""
    assert any(row.reason == "evidence_span 与报告原文对不上" for row in result.rejected)


def test_review_api_activates_only_a_verified_clause():
    from app.main import SessionFactory, app

    clause = _clause("应设置控制区和监督区的管理制度。", path="zone-api")
    db = SessionFactory()
    _store_clause(db, clause)
    db.commit()
    db.close()
    client = TestClient(app)
    created = client.post(
        "/api/check-items/draft",
        json={
            "doc_id": clause.doc_id,
            "clause_path": clause.path,
            "item_code": "ZONE-API",
            "kind": "presence",
            "report_topics": ["控制区"],
            "quote": "控制区和监督区",
            "by": "审核人",
        },
    )
    assert created.status_code == 200
    item_id = created.json()["id"]
    assert created.json()["status"] == "draft"
    refused = client.post(
        "/api/check-items/draft",
        json={
            "doc_id": clause.doc_id,
            "clause_path": clause.path,
            "item_code": "ZONE-BAD",
            "kind": "presence",
            "report_topics": ["控制区"],
            "quote": "条款里没有这句",
            "by": "审核人",
        },
    )
    assert refused.status_code == 400
    activated = client.post(f"/api/check-items/{item_id}/activate", json={"by": "审核人", "note": "启用"})
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    listed = client.get("/api/check-items", params={"status": "active"})
    assert any(row["item_code"] == "ZONE-API" for row in listed.json()["items"])


def test_sample_cases_for_scope_presence_and_unparsed_images():
    parsed = _load_sample()
    if parsed is None:
        pytest.skip("样例报告或 LibreOffice 不可用")
    clause = _clause("应将辐射工作场所分为控制区和监督区。", path="zone")
    active = _item(
        clause,
        item_code="ZONE-ACTIVE",
        report_topics=["控制区", "监督区"],
        quote="分为控制区和监督区",
    )
    draft = _item(clause, id="draft-zone", item_code="ZONE-DRAFT", status="draft", report_topics=["控制区", "监督区"])
    prompts = []

    def transport(body):
        assert "temperature" not in body
        content = body["messages"][-1]["content"]
        prompts.append(content)
        assert "word/media" not in content
        part = content.split("报告片段如下：", 1)[-1]
        left = part.find("控制区")
        right = part.find("监督区")
        if left < 0 or right < 0:
            return json.dumps({"verdict": "无法判定", "evidence_span": "", "reason": "片段里没有分区"})
        start, end = min(left, right), max(left, right) + len("监督区")
        span = part[start:end]
        if "---" in span or len(span) > 160:
            return json.dumps({"verdict": "无法判定", "evidence_span": "", "reason": "片段不连续"})
        return json.dumps({"verdict": "有", "evidence_span": span, "reason": ""})

    cache = MemoryCache()
    runs = [
        evaluate_checks(
            parsed,
            items=[active, draft],
            clauses=[clause],
            as_of=AS_OF,
            transport=transport,
            cache=cache,
            config=CFG,
        )
        for _ in range(3)
    ]
    assert runs[0].result_hash == runs[1].result_hash == runs[2].result_hash
    assert len(prompts) == 1
    assert "ZONE-DRAFT" not in {row.item_code for row in runs[0].findings}
    dose = next(row for row in runs[0].findings if "HJ1198" in row.evidence_span and "Sv/h" in row.evidence_span)
    assert dose.verdict == "超出依据范围"
    assert not any(row.verdict == "符合" and "HJ1198" in row.evidence_span for row in runs[0].findings)
    zone = _one(runs[0], "ZONE-ACTIVE")
    assert zone.verdict == "有"
    assert "控制区" in zone.evidence_span and "监督区" in zone.evidence_span
    assert parsed.full_text[zone.char_start : zone.char_end] == zone.evidence_span
    assert zone.quote in clause.text
    images = [block for block in parsed.blocks if block.type == "image"]
    assert images
    assert all(block.not_parsed for block in images)
    unparsed = [row for row in runs[0].findings if row.kind == "unparsed"]
    assert unparsed
    assert {row.verdict for row in unparsed} == {"无法判定"}


def _proposal(quote: str, value: int) -> dict:
    return {
        "item_code": "DOSE",
        "kind": "numeric",
        "report_topics": ["剂量"],
        "required_params": ["reported_dose"],
        "requirement": {"<=": [{"var": "reported_dose"}, {"value": value, "unit": "mSv"}]},
        "quote": quote,
    }


def _one(result, code: str):
    hits = [row for row in result.findings if row.item_code == code]
    assert len(hits) == 1
    return hits[0]


def _boom(_body):
    raise AssertionError("这项不应该调用模型")


def _clause(text: str, path: str = "dose") -> BasisClauseView:
    return BasisClauseView(
        doc_id="GB18871-2002",
        title="电离辐射防护与辐射源安全基本标准 GB18871-2002",
        path=path,
        text=text,
        text_sha256=sha256_text(text),
        effective_date=date(2003, 4, 1),
        verified=True,
    )


def _item(clause: BasisClauseView, **kwargs) -> CheckItemView:
    payload = {
        "id": kwargs.pop("id", "item-1"),
        "doc_id": clause.doc_id,
        "clause_path": clause.path,
        "clause_sha256": clause.text_sha256,
        "item_code": "ITEM",
        "kind": "presence",
        "report_topics": ["剂量"],
        "required_params": [],
        "requirement": {},
        "quote": clause.text,
        "status": "active",
    }
    payload.update(kwargs)
    return CheckItemView(**payload)


def _parsed(parts: list[dict]):
    blocks = []
    pieces: list[str] = []
    cursor = 0
    for part in parts:
        text = part.get("text") or ""
        if text:
            if pieces:
                pieces.append("\n")
                cursor += 1
            start = cursor
            pieces.append(text)
            cursor += len(text)
            end = cursor
        else:
            start = end = cursor
        blocks.append(
            SimpleNamespace(
                text=text,
                type=part.get("type", "para"),
                section_path=part.get("section", ""),
                char_start=start,
                char_end=end,
                has_image=part.get("has_image", False),
                not_parsed=part.get("not_parsed", False),
                media_file=part.get("media_file"),
            )
        )
    return SimpleNamespace(blocks=blocks, full_text="".join(pieces))


def _store_clause(db, clause: BasisClauseView) -> None:
    document = BasisDocument(
        id=new_id(),
        doc_id=clause.doc_id,
        title=clause.title,
        kind="国家标准",
        effective_date=clause.effective_date,
        source_note="测试夹具，不是标准全文",
        relative_path="basis/fixture.txt",
        file_sha256=sha256_text(clause.text),
        text_sha256=clause.text_sha256,
        clause_count=1,
        status="active",
        created_at=utc_now(),
    )
    db.add(document)
    db.add(
        BasisClause(
            id=new_id(),
            document_id=document.id,
            ordinal=1,
            path=clause.path,
            text=clause.text,
            text_sha256=clause.text_sha256,
            verified=True,
            verified_by="审核人",
            verified_at=utc_now(),
        )
    )
    db.flush()


def _load_sample():
    if SAMPLE_DOCX.is_dir():
        found = next(SAMPLE_DOCX.glob("*.docx"), None)
        if found is not None:
            return parse_docx(found)
    if not SAMPLE_DOC.is_file():
        return None
    try:
        find_soffice()
    except ConvertError:
        return None
    return parse_report(SAMPLE_DOC)
