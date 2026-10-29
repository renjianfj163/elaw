import json
from pathlib import Path

from sqlalchemy import select

from app.db import make_engine, make_session_factory
from app.llm.provider import MemoryCache
from app.models import Base, ReviewLog
from app.schematic.discovery import DiscoveryBatch, discover_missing_rules
from app.schematic.gemini_client import _response_schema

CFG = {
    "model": "gemini-2.5-pro",
    "prompt_version": "schematic-2",
    "retries": 0,
    "cache": True,
    "max_output_tokens": 8192,
}
KEPT = "开关电源的续流二极管方向必须与输出极性一致。"


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "board.pdf"
    path.write_bytes(b"%PDF-1.4 schematic")
    return path


def _packs(tmp_path: Path) -> Path:
    path = tmp_path / "packs.json"
    path.write_text(
        json.dumps(
            {
                "packs": [
                    {
                        "tag": "power_supply",
                        "title": "电源",
                        "hint": "LDO 和去耦",
                        "text": "LDO 必须有输入和输出去耦电容。",
                        "apply": "always",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _proposal(**overrides) -> dict:
    body = {
        "suggested_tag": "power_supply",
        "draft_rule_text": KEPT,
        "trigger_context": "图上有降压电路",
        "engineering_rationale": "减小开关环路",
    }
    body.update(overrides)
    return body


def _session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def test_discovery_schema_is_separate_from_review():
    assert _response_schema("discover") is DiscoveryBatch


def test_numeric_unknown_and_duplicate_drafts_stay_out_of_the_pool(tmp_path):
    pending = tmp_path / "pending.json"

    def transport(body: dict) -> str:
        assert body["schema"] == "discover"
        assert "temperature" not in body
        assert "2.2k" not in body["system"]
        return json.dumps(
            {
                "new_rules": [
                    _proposal(suggested_tag="power_basic"),
                    _proposal(),
                    _proposal(draft_rule_text="续流二极管按 2 倍余量选取。"),
                    _proposal(draft_rule_text="LDO 必须有输入和输出去耦电容。"),
                ]
            },
            ensure_ascii=False,
        )

    packs = _packs(tmp_path)
    db = _session()
    outcome = discover_missing_rules(
        _pdf(tmp_path),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
        rules_path=packs,
        pending_path=pending,
        db=db,
    )
    assert [item.text for item in outcome.rules] == [KEPT]
    assert outcome.rules[0].status == "accepted"
    assert "已写入规则库" in outcome.note
    assert "没有写入规则库" in outcome.note
    saved = json.loads(pending.read_text(encoding="utf-8"))
    assert saved["rules"][0]["status"] == "accepted"
    stored = json.loads(packs.read_text(encoding="utf-8"))
    assert stored["packs"][0]["text"].endswith(KEPT)
    assert "2 倍" not in stored["packs"][0]["text"]
    log = db.scalar(select(ReviewLog).where(ReviewLog.target_id == outcome.rules[0].id))
    assert log is not None
    assert log.action == "verify"
    assert log.by == "系统"
    assert log.target_type == "schematic_rule"


def test_same_draft_is_not_saved_twice(tmp_path):
    pending = tmp_path / "pending.json"
    packs = _packs(tmp_path)
    calls = {"n": 0}

    def transport(body: dict) -> str:
        calls["n"] += 1
        return json.dumps({"new_rules": [_proposal()]}, ensure_ascii=False)

    cache = MemoryCache()
    pdf = _pdf(tmp_path)
    db = _session()
    first = discover_missing_rules(
        pdf, transport=transport, cache=cache, config=CFG, rules_path=packs, pending_path=pending, db=db
    )
    second = discover_missing_rules(
        pdf, transport=transport, cache=cache, config=CFG, rules_path=packs, pending_path=pending, db=db
    )
    assert len(first.rules) == 1
    assert second.rules == []
    assert len(json.loads(pending.read_text(encoding="utf-8"))["rules"]) == 1
    assert KEPT in packs.read_text(encoding="utf-8")
    assert packs.read_text(encoding="utf-8").count(KEPT) == 1


def test_failed_discovery_returns_a_note(tmp_path):
    def transport(body: dict) -> str:
        return "不是 JSON"

    outcome = discover_missing_rules(
        _pdf(tmp_path),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
        rules_path=_packs(tmp_path),
        pending_path=tmp_path / "pending.json",
    )
    assert outcome.rules == []
    assert outcome.note == "规则建议没有生成"


