import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.llm.provider import MemoryCache
from app.main import app
from app.rules.evaluator import evaluate
from app.schematic.discovery import DiscoveryOutcome
from app.schematic.review import MAX_BASIS_CHARS, MAX_SCHEMATIC_FILES, SchematicInputError, review_schematic

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


def test_datasheet_finding_can_name_the_part_instead_of_the_refdes(tmp_path):
    quote = "3.14V 到 3.46V"
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        target_refdes="U1103",
                        check_type="numeric",
                        requirement_source="Datasheet",
                        findings="RTL8127AT 的 DVDDIO 在图纸上为 3.3V。",
                        quote=quote,
                        observed={"value": 3.3, "unit": "V"},
                        compare_op="between",
                        limit_value=3.14,
                        limit_high=3.46,
                        limit_unit="V",
                        param_name="dvddio",
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        "",
        f"RTL8127AT 的 DVDDIO 范围为 {quote}。",
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.rejected == []
    assert result.items[0].conclusion == "符合"
    assert result.items[0].target_refdes == "U1103"


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
    assert {item.admission for item in result.items} == {"needs_evidence"}
    assert {item.conclusion for item in result.items} == {"无法判定"}
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
    assert item.admission == "accepted"
    assert item.status == "draft"
    assert item.evidence[0].source_type == "datasheet"
    assert item.evidence[0].verification_status == "verified"
    assert item.evidence[0].quote == "推荐工作电压上限为 3.6V"
    assert item.evidence[0].pdf_page is None
    assert item.trace


def test_datasheet_label_requires_the_quote_to_come_from_the_datasheet(tmp_path):
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        requirement_source="Datasheet",
                        findings="图纸上的 U1 旁标有 I2C 网络。",
                        quote="需要一路 I2C 接口",
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        PRODUCT,
        DATASHEET,
        rules_text="LDO 必须有输入和输出去耦电容。",
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.items[0].admission == "needs_evidence"
    assert result.items[0].conclusion == "无法判定"
    assert result.items[0].requirement_source == "待核实"
    assert all(row.source_type != "datasheet" for row in result.items[0].evidence)
    assert result.rejected[0].reason == "quote 不在 Datasheet 原文里"


def test_unicode_minus_in_a_datasheet_quote_still_compares(tmp_path):
    quote = "−0.3 4.0 V"
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        check_type="numeric",
                        requirement_source="Datasheet",
                        findings="图纸上的 U1 供电为 3.3V。",
                        quote=quote,
                        observed={"value": 3.3, "unit": "V"},
                        compare_op="between",
                        limit_value=-0.3,
                        limit_high=4.0,
                        limit_unit="V",
                        param_name="vdd",
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        "",
        f"绝对最大额定值 {quote} 范围内。",
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.rejected == []
    assert result.items[0].conclusion == "符合"
    assert result.items[0].requirement_source == "Datasheet"


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
                        compare_op="<=",
                        limit_value=9.9,
                        limit_unit="V",
                        param_name="vdd",
                    )
                ]
            }
        ]
    )
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert result.items[0].admission == "needs_evidence"
    assert result.items[0].conclusion == "无法判定"
    assert result.items[0].calculation is not None
    assert result.items[0].calculation.validation_status == "not_verified"
    assert result.items[0].calculation.calculated_value is None
    assert result.rejected[0].reason == "这个限值没有出现在原文里，也没有给出可重算的输入。"


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


def test_derived_limit_is_recomputed_without_appearing_in_the_quote(tmp_path):
    quote = "12 V 线路上，工作电压不得超过额定耐压的 70%。"
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        check_type="numeric",
                        requirement_source="审查规则",
                        findings="图纸上的 U1 旁电容额定耐压为 25V。",
                        conclusion="无法判定",
                        quote=quote,
                        observed={"value": 25, "unit": "V"},
                        compare_op=">=",
                        limit_value=17.14,
                        limit_unit="V",
                        param_name="rated_voltage",
                        calc_op="percent_divide",
                        calc_inputs=[
                            {"name": "left", "value": 12, "unit": "V"},
                            {"name": "right", "value": 70, "unit": "%"},
                        ],
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        "",
        "",
        rules_text=quote,
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    item = result.items[0]
    assert item.admission == "accepted"
    assert item.conclusion == "符合"
    assert item.calculation is not None
    assert item.calculation.validation_status == "verified"
    assert item.calculation.calculated_value is not None
    assert abs(item.calculation.calculated_value - (12 / 0.7)) < 1e-9
    assert "17.14" not in quote
    kinds = {row.source_type for row in item.evidence}
    assert kinds == {"general_rule", "derived_calculation"}


def test_board_voltages_can_finish_a_percent_derating(tmp_path):
    quote = "陶瓷电容的工作电压不得超过额定耐压的 70%。图上缺额定耐压或工作电压时，结论为无法判定。"
    shared = {
        "target_refdes": "C501",
        "check_type": "numeric",
        "requirement_source": "审查规则",
        "findings": "图纸上的陶瓷去耦电容 C501（100nF）额定耐压为 50V，工作在 12V 总线上。",
        "conclusion": "无法判定",
        "quote": quote,
        "compare_op": ">=",
        "limit_value": 70,
        "limit_unit": "%",
        "param_name": "rated_voltage",
        "calc_op": "percent_divide",
        "calc_inputs": [
            {"name": "left", "value": 12, "unit": "V", "origin": "drawing"},
            {"name": "right", "value": 70, "unit": "%"},
        ],
    }

    def run(rated: float):
        transport = _transport([{"items": [_item(observed={"value": rated, "unit": "V"}, **shared)]}])
        result = review_schematic(
            _pdf(tmp_path),
            "",
            "",
            rules_text=quote,
            transport=transport,
            cache=MemoryCache(),
            config=CFG,
        )
        return result.items[0]

    passed = run(50)
    assert passed.admission == "accepted"
    assert passed.conclusion == "符合"
    assert abs(passed.calculation.calculated_value - (12 / 0.7)) < 1e-9
    assert "12" not in quote

    tight = run(16)
    assert tight.conclusion == "不符合"
    assert tight.admission == "accepted"


def test_percent_limit_still_has_to_be_in_the_rule_text(tmp_path):
    quote = "陶瓷电容的工作电压不得超过额定耐压。图上缺额定耐压或工作电压时，结论为无法判定。"
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        target_refdes="C501",
                        check_type="numeric",
                        requirement_source="审查规则",
                        findings="图纸上的陶瓷去耦电容 C501 额定耐压为 50V，工作在 12V 总线上。",
                        conclusion="无法判定",
                        quote=quote,
                        observed={"value": 50, "unit": "V"},
                        compare_op=">=",
                        limit_unit="%",
                        param_name="rated_voltage",
                        calc_op="percent_divide",
                        calc_inputs=[
                            {"name": "left", "value": 12, "unit": "V", "origin": "drawing"},
                            {"name": "right", "value": 70, "unit": "%", "origin": "drawing"},
                        ],
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        "",
        "",
        rules_text=quote,
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    item = result.items[0]
    assert item.conclusion == "无法判定"
    assert item.admission == "needs_evidence"
    assert item.calculation.validation_status == "not_verified"


def test_untraced_derating_stays_visible_and_unverified(tmp_path):
    quote = "陶瓷电容应计入直流偏置引起的容值衰减。其工作电压不得超过额定耐压的 70%，宜按不超过 50% 使用。"
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        target_refdes="C503",
                        check_type="numeric",
                        requirement_source="审查规则",
                        findings="图纸 Page 4 中 VDD_12V_SYS 输入端的去耦电容 C503（22uF）额定耐压为 25V。",
                        conclusion="符合",
                        quote=quote,
                        observed={"value": 25, "unit": "V"},
                        compare_op=">=",
                        limit_value=17.14,
                        limit_unit="V",
                        param_name="rated_voltage",
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        "",
        "VDD 为电源引脚。",
        rules_text=quote,
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    item = result.items[0]
    assert item.target_refdes == "C503"
    assert item.admission == "needs_evidence"
    assert item.conclusion == "无法判定"
    assert item.calculation is not None
    assert item.calculation.validation_status == "not_verified"
    assert item.calculation.calculated_value is None
    assert "17.14" in item.calculation.assumptions
    assert item.evidence[0].source_type == "general_rule"
    assert all(row.source_type != "datasheet" for row in item.evidence)


def test_same_quote_keeps_datasheet_and_rule_evidence(tmp_path):
    sentence = "每个电源脚需要去耦电容。"
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        requirement_source="审查规则",
                        findings="图纸上的 U1 电源脚旁有电容。",
                        quote=sentence,
                    )
                ]
            }
        ]
    )
    result = review_schematic(
        _pdf(tmp_path),
        "",
        sentence,
        rules_text=sentence,
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    kinds = [row.source_type for row in result.items[0].evidence]
    assert kinds == ["datasheet", "general_rule"]
    assert result.items[0].requirement_source == "Datasheet"


def test_part_number_outside_the_quote_is_not_attached(tmp_path):
    transport = _transport(
        [
            {
                "items": [
                    _item(
                        requirement_source="Datasheet",
                        findings="图纸上的 U1 电源为 3.3V。",
                        quote="推荐工作电压上限为 3.6V",
                        part_number="SYN-OTHER-999",
                    )
                ]
            }
        ]
    )
    result = review_schematic(_pdf(tmp_path), PRODUCT, DATASHEET, transport=transport, cache=MemoryCache(), config=CFG)
    assert result.items[0].admission == "needs_evidence"
    assert result.rejected[0].reason == "声称的型号没有出现在这条原文里"
    assert all(not row.part_number for row in result.items[0].evidence)


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
        review_schematic(
            _pdf(tmp_path),
            PRODUCT,
            "A" * (MAX_BASIS_CHARS + 1),
            transport=lambda body: "[]",
            cache=MemoryCache(),
            config=CFG,
        )
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
    monkeypatch.setattr("app.schematic.api.discover_missing_rules", lambda *args, **kwargs: DiscoveryOutcome())
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
    assert body["pending_rules"] == []


def test_review_route_accepts_many_schematics(tmp_path, monkeypatch):
    seen = {}

    def fake(path, product, datasheet, **kwargs):
        from app.schematic.pipeline import PlannedReview

        seen["count"] = len(path)
        seen["product"] = product
        return PlannedReview(result_hash="abc")

    monkeypatch.setattr("app.schematic.api.review_planned", fake)
    monkeypatch.setattr("app.schematic.api.discover_missing_rules", lambda *args, **kwargs: DiscoveryOutcome())
    client = TestClient(app)
    response = client.post(
        "/api/schematic/review",
        files=[
            ("schematic", ("power.pdf", b"%PDF-1.4 power", "application/pdf")),
            ("schematic", ("io.pdf", b"%PDF-1.4 io", "application/pdf")),
        ],
        data={"product_spec_text": PRODUCT, "datasheet_text": ""},
    )
    assert response.status_code == 200
    assert seen["count"] == 2
    assert seen["product"] == PRODUCT
    assert response.json()["filename"] == "power.pdf、io.pdf"


def test_review_route_rejects_too_many_schematics():
    client = TestClient(app)
    files = [
        ("schematic", (f"{index}.pdf", b"%PDF-1.4", "application/pdf"))
        for index in range(MAX_SCHEMATIC_FILES + 1)
    ]
    response = client.post("/api/schematic/review", files=files)
    assert response.status_code == 400
    assert "120" in response.json()["detail"]


def test_review_route_allows_a_schematic_without_materials(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path)

    def fake(path, product, datasheet, **kwargs):
        from app.schematic.pipeline import RULES_ONLY_NOTE, PlannedReview

        assert product == ""
        assert datasheet == ""
        return PlannedReview(result_hash="abc", basis_note=RULES_ONLY_NOTE)

    monkeypatch.setattr("app.schematic.api.review_planned", fake)
    monkeypatch.setattr("app.schematic.api.discover_missing_rules", lambda *args, **kwargs: DiscoveryOutcome())
    client = TestClient(app)
    response = client.post(
        "/api/schematic/review",
        files={"schematic": ("board.pdf", pdf.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200
    body = response.json()
    assert "未挂载产品定义与 Datasheet" in body["disclaimer"]
    assert body["basis_note"] == body["disclaimer"].replace("辅助预审，不构成设计签核。审查项为草稿，需人工复核。", "")


def test_review_without_any_basis_is_refused(tmp_path):
    try:
        review_schematic(_pdf(tmp_path), "  ", "", transport=lambda body: "[]", cache=MemoryCache(), config=CFG)
    except SchematicInputError as exc:
        assert "没有可引用的依据" in str(exc)
    else:
        raise AssertionError("没有依据时应停止")
