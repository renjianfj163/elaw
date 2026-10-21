import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.laws.catalog_decision import compile_when, decide, diff_types
from app.laws.catalog_store import confirm_release, decide_confirmed, remember_snapshot, save_draft
from app.main import app
from app.models import Base, CatalogRelease


def _hospital_row(book_limit: int, form_limit: int) -> dict:
    return {
        "category": "示例医院",
        "book_text": "床位达到校对人填写的报告书门槛",
        "form_text": "床位低于该门槛",
        "register_text": "本示例不设登记表",
        "book_when": {"mode": "when", "all": [{"var": "bed_count", "op": ">=", "value": book_limit}]},
        "form_when": {"mode": "when", "all": [{"var": "bed_count", "op": "<", "value": form_limit}]},
        "register_when": {"mode": "never"},
    }


def test_catalog_rules_do_not_guess_missing_numbers():
    with pytest.raises(ValueError):
        compile_when({"mode": "when", "all": []})
    row = {
        "id": "r1",
        "category": "示例医院",
        "book_rule": compile_when(_hospital_row(500, 500)["book_when"]),
        "form_rule": compile_when(_hospital_row(500, 500)["form_when"]),
        "register_rule": compile_when({"mode": "never"}),
    }
    high = decide([row], "示例医院", {"bed_count": 800})
    low = decide([row], "示例医院", {"bed_count": 100})
    missing = decide([row], "示例医院", {})
    assert high["eia_type"] == "报告书"
    assert low["eia_type"] == "报告表"
    assert missing["determination"] == "无法判定"
    assert "bed_count" in missing["missing"]


def test_unconfirmed_catalog_cannot_decide_and_update_is_diffed():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db: Session = make_session_factory(engine)()
    name = "示例分类名录"
    draft = save_draft(db, name=name, rows=[_hospital_row(500, 500)])
    db.commit()
    blocked = decide_confirmed(db, name=name, category="示例医院", params={"bed_count": 800})
    assert blocked["determination"] == "无法判定"
    remember_snapshot(db, name=name, label="八百床示例", category="示例医院", params={"bed_count": 800})
    db.commit()
    first = confirm_release(db, release_id=draft.id, by="校对人", note="第一次")
    db.commit()
    assert first["changes"] == []
    allowed = decide_confirmed(db, name=name, category="示例医院", params={"bed_count": 800})
    assert allowed["eia_type"] == "报告书"
    second = save_draft(db, name=name, rows=[_hospital_row(1000, 1000)])
    db.commit()
    changed = confirm_release(db, release_id=second.id, by="校对人", note="门槛已按新文本改过")
    db.commit()
    assert changed["changes"][0]["before_type"] == "报告书"
    assert changed["changes"][0]["after_type"] == "报告表"
    locked = db.get(CatalogRelease, draft.id)
    locked.note = "试图改已确认版本"
    with pytest.raises(PermissionError):
        db.commit()
    db.rollback()


def test_catalog_api_rejects_partial_confirmation():
    client = TestClient(app)
    saved = client.post(
        "/api/catalog/draft",
        json={
            "name": "接口示例名录",
            "rows": [
                {
                    "category": "示例医院",
                    "book_text": "原文",
                    "form_text": "原文",
                    "register_text": "原文",
                    "book_when": {"mode": "always"},
                    "form_when": None,
                    "register_when": {"mode": "never"},
                }
            ],
        },
    )
    assert saved.status_code == 200
    confirmed = client.post(f"/api/catalog/{saved.json()['release_id']}/confirm", json={"by": "校对人"})
    assert confirmed.status_code == 400
    decision = client.post(
        "/api/catalog/decide",
        json={"name": "接口示例名录", "category": "示例医院", "params": {"bed_count": 10}},
    )
    assert decision.json()["determination"] == "无法判定"


def test_diff_without_database():
    old = [{"id": "a", "category": "示例医院", "book_rule": {">=": [{"var": "bed_count"}, 500]}, "form_rule": {"==": [True, False]}, "register_rule": {"==": [True, False]}}]
    new = [{"id": "b", "category": "示例医院", "book_rule": {">=": [{"var": "bed_count"}, 1000]}, "form_rule": {"<": [{"var": "bed_count"}, 1000]}, "register_rule": {"==": [True, False]}}]
    changes = diff_types(old, new, [{"label": "八百床", "category": "示例医院", "params": {"bed_count": 800}}])
    assert changes[0]["before_type"] == "报告书"
    assert changes[0]["after_type"] == "报告表"
