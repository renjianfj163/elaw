import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.llm.provider import MemoryCache
from app.main import app
from app.rules.evaluator import evaluate
from app.schematic.review import SchematicInputError, review_schematic

PRODUCT = "3.2 接口要求：需要一路 I2C 接口。"
DATASHEET = "推荐工作电压上限为 3.6V。每个电源脚需要去耦电容。"
CFG = {
    "model": "gemini-1.5-pro",
    "prompt_version": "schematic-1",
    "retries": 2,
    "cache": True,
    "max_output_tokens": 8192,
}


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "board.pdf"
    path.write_bytes(b"%PDF-1.4 schematic")
    return path


def _item(**overrides) -> dict:
    body = {
        "target_refdes": "U1",
        "check_type": "presence",
        "requirement_source": "产品规范 3.2",
        "findings": "图纸上的 U1 旁标有 I2C 网络。",
        "conclusion": "符合",
        "status": "draft",
        "quote": "需要一路 I2C 接口",
    }
    body.update(overrides)
    return body


def _transport(payloads: list[dict]):
    seen: list[dict] = []

    def send(body: dict) -> str:
        seen.append(body)
        if not payloads:
            raise RuntimeError("没有更多预设回复")
        return json.dumps(payloads.pop(0), ensure_ascii=False)

    send.seen = seen
    return send


def test_presence_item_stays_draft_and_request_has_no_temperature(tmp_path):
    transport = _transport([{"items": [_item()]}])
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert result.rejected == []
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status == "draft"
    assert item.conclusion == "符合"
    assert item.check_type == "presence"
    assert item.needs_review is True
    assert "temperature" not in transport.seen[0]


def test_quote_mismatch_and_missing_refdes_are_rejected(tmp_path):
    transport = _transport(
        [
            {
                "items": [
                    _item(quote="图纸上并没有的句子"),
                    _item(target_refdes="U1", findings="只写了 U10，没有单独的目标位号。"),
                ]
            }
        ]
    )
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert result.items == []
    reasons = {item.reason for item in result.rejected}
    assert "quote 不是依据原文的子串" in reasons
    assert "findings 没有写明图纸上的位号" in reasons


def test_numeric_conclusion_comes_from_the_rule_engine(tmp_path):
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        check_type="numeric",
                        requirement_source="Datasheet 推荐工作电压",
                        findings="U1 的 VDD 读数为 5V。",
                        conclusion="符合",
                        quote="推荐工作电压上限为 3.6V",
                        observed={"value": 5, "unit": "V"},
                        compare_op="<=",
                        limit_value=3.6,
                        limit_unit="V",
                        param_name="vdd",
                    )
                ]
            }
        ]
    )
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert result.rejected == []
    item = result.items[0]
    assert item.method == "rule-engine"
    assert item.conclusion == "不符合"
    assert item.status == "draft"
    assert item.trace


def test_invented_limit_is_rejected(tmp_path):
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        check_type="numeric",
                        findings="U1 的 VDD 读数为 3.3V。",
                        quote="推荐工作电压上限为 3.6V",
                        observed={"value": 3.3, "unit": "V"},
                        requirement={"<=": [{"var": "vdd"}, {"value": 9.9, "unit": "V"}]},
                        required_params=["vdd"],
                    )
                ]
            }
        ]
    )
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert result.items == []
    assert result.rejected[0].reason == "requirement 中的数字不在依据原文里"


def test_qualitative_disagreement_is_undecided_and_marked_dual_review(tmp_path):
    transport = _transport(
        [
            {"items": [_item(check_type="qualitative", conclusion="符合", findings="U1 的 I2C 脚已连接。")]},
            {"items": [_item(check_type="qualitative", conclusion="不符合", findings="另一遍认为 U1 未连接。")]},
        ]
    )
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert len(transport.seen) == 2
    assert "先前结论" in transport.seen[1]["system"]
    assert "符合" not in transport.seen[1]["user"]
    item = result.items[0]
    assert item.conclusion == "无法判定"
    assert item.dual_review == "同源双审"
    assert item.needs_review is True
    assert item.reason == "两遍结论不一致"


def test_same_input_reuses_cache_and_keeps_result_hash(tmp_path):
    calls = {"n": 0}

    def transport(body: dict) -> str:
        calls["n"] += 1
        return json.dumps({"items": [_item()]}, ensure_ascii=False)

    cache = MemoryCache()
    pdf = _pdf(tmp_path)
    hashes = []
    for _ in range(3):
        result = review_schematic(pdf, PRODUCT, DATASHEET, transport=transport, cache=cache, config=CFG)
        hashes.append(result.result_hash)
    assert calls["n"] == 1
    assert hashes[0] == hashes[1] == hashes[2]


def test_model_failure_is_undecided_without_items(tmp_path):
    def transport(body: dict) -> str:
        return "不是 JSON"

    result = review_schematic(
        _pdf(tmp_path),
        PRODUCT,
        DATASHEET,
        transport=transport,
        cache=MemoryCache(),
        config={**CFG, "retries": 2},
    )
    assert result.items == []
    assert result.reason == "模型输出未通过校验"


def test_long_datasheet_is_refused_before_the_model(tmp_path):
    try:
        review_schematic(_pdf(tmp_path), PRODUCT, "A" * 48001, transport=lambda body: "[]", cache=MemoryCache(), config=CFG)
    except SchematicInputError as exc:
        assert "不要送入整本" in str(exc)
    else:
        raise AssertionError("过长依据应被拒绝")


def test_capacitance_units_go_through_the_rule_engine():
    result = evaluate(
        {"==": [{"var": "c"}, {"value": 100, "unit": "nF"}]},
        {"c": {"value": 0.1, "unit": "µF"}},
    )
    assert result["result"] == "true"


def test_review_route_returns_drafts(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path)

    def fake(path, product, datasheet, **kwargs):
        from app.schematic.pipeline import PlannedReview
        from app.schematic.review import SchematicDraftItem

        return PlannedReview(
            items=[
                SchematicDraftItem(
                    target_refdes="U1",
                    check_type="presence",
                    requirement_source="产品规范 3.2",
                    findings="图纸上的 U1 旁标有 I2C。",
                    conclusion="符合",
                    quote="需要一路 I2C 接口",
                    method="model",
                )
            ],
            result_hash="abc",
        )

    monkeypatch.setattr("app.schematic.api.review_planned", fake)
    client = TestClient(app)
    response = client.post(
        "/api/schematic/review",
        files={"schematic": ("board.pdf", pdf.read_bytes(), "application/pdf")},
        data={"product_spec_text": PRODUCT, "datasheet_text": DATASHEET},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["status"] == "draft"
    assert body["items"][0]["target_refdes"] == "U1"
    assert "不构成设计签核" in body["disclaimer"]
