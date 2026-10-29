import json
from pathlib import Path

import pytest

from app.db import repo_root
from app.llm.provider import MemoryCache
from app.schematic.crop import crop_datasheet
from app.schematic.gemini_client import GeminiCallError, _response_schema, gemini_transport
from app.schematic.pipeline import (
    DATASHEET_ONLY_NOTE,
    DATASHEET_UNUSED,
    NO_RULES,
    PLAN_FAILED,
    PRODUCT_ONLY_NOTE,
    RULES_ONLY_NOTE,
    review_planned,
)
from app.schematic.planner import CircuitAnalysisPlan
from app.schematic.review import MAX_BASIS_CHARS, ProposalBatch, SchematicInputError

PRODUCT = "3.2 接口要求：需要一路 I2C 接口。"
CFG = {
    "model": "gemini-2.5-pro",
    "planner_model": "gemini-2.5-flash",
    "prompt_version": "schematic-2",
    "retries": 0,
    "cache": True,
    "max_output_tokens": 8192,
}


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "board.pdf"
    path.write_bytes(b"%PDF-1.4 schematic")
    return path


def _datasheet() -> str:
    return (
        "1 封装尺寸\n"
        + ("封装填充。\n" * 12000)
        + "\n2 引脚说明\nVDD 为电源引脚。SDA 为 I2C 数据。\n\n"
        "3 Typical Application\n输出引脚配置 10uF 陶瓷电容。\n\n"
        "4 寄存器配置\n寄存器 0x01 控制备用通道。\n\n"
        "5 TPS7A4700 专用说明\n仅在识别到该型号时保留这句专用说明。\n"
    )


def _plan(**overrides) -> dict:
    body = {
        "identified_modules": ["LDO降压", "I2C接口"],
        "active_components": ["U2(TPS7A4700)"],
        "required_rule_tags": ["power_supply"],
    }
    body.update(overrides)
    return body


def _item(**overrides) -> dict:
    body = {
        "target_refdes": "U2",
        "check_type": "presence",
        "requirement_source": "电源规则",
        "findings": "图纸上的 U2 输入和输出侧都有电容。",
        "conclusion": "符合",
        "status": "draft",
        "quote": "LDO 必须有输入和输出去耦电容",
    }
    body.update(overrides)
    return body


def _transport(plans: list, reviews: list):
    seen: list[dict] = []

    def send(body: dict) -> str:
        seen.append(body)
        if body.get("schema") == "plan":
            if not plans:
                raise RuntimeError("没有更多规划回复")
            payload = plans.pop(0)
            if isinstance(payload, str):
                return payload
            return json.dumps(payload, ensure_ascii=False)
        if not reviews:
            return json.dumps({"items": []}, ensure_ascii=False)
        return json.dumps({"items": [reviews.pop(0)]}, ensure_ascii=False)

    send.seen = seen
    return send


def test_short_excerpt_without_drop_sections_passes_through():
    text = "1. 推荐工作电压上限为 3.6V。\n2. 每个电源脚需要去耦电容。"
    assert crop_datasheet(text, ["U1"]) == text


def test_one_datasheet_cannot_crowd_out_the_next():
    first = "【stm32.pdf】\n" + "\n\n".join(f"Pin description {index}\n" + ("X" * 2000) for index in range(8))
    second = "【som.pdf】\nPin description\nKEEP_SOM_UNIQUE 上拉到 1.8V。\n"
    cropped = crop_datasheet(f"{first}\n\n{second}", ["U1", "U2"], limit=6000)
    assert "KEEP_SOM_UNIQUE" in cropped


GENERAL_TAGS = ["power_supply", "high_speed_digital", "component_derating", "dft_hygiene"]


def test_rule_numbers_live_only_in_the_data_file():
    packs = (repo_root() / "data" / "schematic_rules" / "packs.json").read_text(encoding="utf-8")
    assert "2.2k-4.7k" in packs
    assert "I_sat" in packs
    assert "NRND" in packs
    for path in (repo_root() / "backend" / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "2.2k" not in source
        assert "I_sat" not in source
        assert "NRND" not in source


def test_response_schema_uses_the_plan_model_only_for_the_planner():
    assert _response_schema("plan") is CircuitAnalysisPlan
    assert _response_schema("proposal") is ProposalBatch
    assert _response_schema("") is ProposalBatch


def test_review_schema_is_accepted_by_the_gemini_developer_api():
    from google.genai import _transformers

    class DeveloperApi:
        vertexai = False

    schema = _transformers.t_schema(DeveloperApi(), ProposalBatch)
    dumped = schema.model_dump()
    blob = json.dumps(dumped)
    assert "additionalProperties" not in blob
    item_fields = dumped["properties"]["items"]["items"]["properties"]
    assert "requirement" not in item_fields
    assert "compare_op" in item_fields


def test_transport_rejects_temperature_without_calling_the_api(tmp_path):
    send = gemini_transport(_pdf(tmp_path))
    with pytest.raises(GeminiCallError, match="temperature"):
        send({"temperature": 0})
    send.close()
    with pytest.raises(GeminiCallError, match="已结束"):
        send({"model": "gemini-2.5-pro", "system": "", "user": "看图", "max_output_tokens": 8})


def test_gemini_transport_sends_the_proxy_base_without_temperature(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443/v1beta\n"
        "  model: gemini-2.5-pro\n"
        "  outbound_allowed: true\n"
        "  thinking: enabled\n"
        "  prompt_version: t\n",
        encoding="utf-8",
    )
    seen = {}

    class Files:
        def upload(self, file):
            class Uploaded:
                name = "files/1"
                state = "ACTIVE"

            return Uploaded()

        def delete(self, name):
            seen["deleted"] = name

    class Models:
        def generate_content(self, **kwargs):
            seen["generate"] = kwargs

            class Response:
                text = '{"items":[]}'

            return Response()

    class Client:
        def __init__(self, *, api_key=None, http_options=None):
            seen["api_key"] = api_key
            seen["base_url"] = None if http_options is None else http_options.base_url
            self.files = Files()
            self.models = Models()

    import google.genai as genai

    monkeypatch.setattr(genai, "Client", Client)
    send = gemini_transport(_pdf(tmp_path))
    text = send({"model": "gemini-2.5-pro", "system": "s", "user": "看图", "max_output_tokens": 8, "schema": "proposal"})
    assert text == '{"items":[]}'
    assert seen["api_key"] == "test-key"
    assert seen["base_url"] == "http://108.160.140.173:8443"
    assert seen["generate"]["model"] == "gemini-2.5-pro"
    assert seen["generate"]["config"].temperature is None
    send.close()
    assert seen["deleted"] == "files/1"


def test_gemini_transport_uploads_every_schematic(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    (tmp_path / "llm.yaml").write_text(
        "llm:\n"
        "  provider: gemini\n"
        "  base_url: http://108.160.140.173:8443\n"
        "  model: gemini-2.5-pro\n"
        "  outbound_allowed: true\n"
        "  thinking: enabled\n"
        "  prompt_version: t\n",
        encoding="utf-8",
    )
    seen = {"uploads": [], "deleted": []}

    class Files:
        def upload(self, file):
            seen["uploads"].append(file)

            class Uploaded:
                name = f"files/{len(seen['uploads'])}"
                state = "ACTIVE"

            return Uploaded()

        def delete(self, name):
            seen["deleted"].append(name)

    class Models:
        def generate_content(self, **kwargs):
            seen["contents"] = kwargs["contents"]

            class Response:
                text = '{"items":[]}'

            return Response()

    class Client:
        def __init__(self, *, api_key=None, http_options=None):
            self.files = Files()
            self.models = Models()

    import google.genai as genai

    monkeypatch.setattr(genai, "Client", Client)
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"%PDF-a")
    second.write_bytes(b"%PDF-b")
    send = gemini_transport([first, second])
    text = send({"model": "gemini-2.5-pro", "system": "s", "user": "看图", "max_output_tokens": 8, "schema": "proposal"})
    assert text == '{"items":[]}'
    assert len(seen["uploads"]) == 2
    assert len(seen["contents"]) == 3
    assert seen["contents"][-1] == "看图"
    send.close()
    assert seen["deleted"] == ["files/1", "files/2"]


def test_schematic_transport_asks_for_gemini_when_deepseek_is_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("ELAW_CONFIG_DIR", str(tmp_path))
    (tmp_path / "llm.yaml").write_text(
        "llm:\n  provider: deepseek\n  base_url: https://api.deepseek.com\n  model: deepseek-v4-flash\n  outbound_allowed: true\n",
        encoding="utf-8",
    )
    send = gemini_transport(_pdf(tmp_path))
    with pytest.raises(GeminiCallError, match="选择 Gemini"):
        send({"model": "deepseek-v4-flash", "system": "", "user": "看图", "max_output_tokens": 8})


def test_pipeline_crops_datasheet_and_mounts_only_the_selected_rules(tmp_path):
    transport = _transport([_plan()], [_item()])
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.plan_note == ""
    assert result.rule_tags == GENERAL_TAGS
    assert result.identified_modules == ["LDO降压", "I2C接口"]
    assert result.items[0].status == "draft"
    assert result.items[0].conclusion == "符合"
    plan_call, review_call = transport.seen[0], transport.seen[1]
    assert plan_call["schema"] == "plan"
    assert plan_call["model"] == "gemini-2.5-flash"
    assert "temperature" not in plan_call
    assert "2.2k" not in plan_call["user"]
    assert "LDO、电源去耦" in plan_call["user"]
    assert review_call["model"] == "gemini-2.5-pro"
    assert "schema" not in review_call
    assert "temperature" not in review_call
    assert "不适用" in review_call["user"]
    user = review_call["user"]
    assert "VDD 为电源引脚" in user
    assert "10uF" in user
    assert "仅在识别到该型号时保留这句专用说明" in user
    assert "LDO 必须有输入和输出去耦电容" in user
    assert "未使用的输入引脚不得悬空" in user
    assert "I_sat" in user
    assert "测试点" in user
    assert "射频走线上必须有 DC Block" not in user
    assert "封装填充" not in user
    assert "备用通道" not in user
    assert len(user) < MAX_BASIS_CHARS


def test_failed_plan_keeps_the_conservative_sections_and_is_cached(tmp_path):
    calls = {"n": 0}

    def transport(body: dict) -> str:
        calls["n"] += 1
        if body.get("schema") == "plan":
            return "不是 JSON"
        assert "专用说明" not in body["user"]
        assert "封装填充" not in body["user"]
        assert "LDO 必须有输入和输出去耦电容" not in body["user"]
        assert "未使用的输入引脚不得悬空" not in body["user"]
        assert "VDD 为电源引脚" in body["user"]
        return json.dumps({"items": [_item(quote="VDD 为电源引脚")]}, ensure_ascii=False)

    cache = MemoryCache()
    pdf = _pdf(tmp_path)
    config = {**CFG, "retries": 2}
    first = review_planned(pdf, PRODUCT, _datasheet(), transport=transport, cache=cache, config=config)
    second = review_planned(pdf, PRODUCT, _datasheet(), transport=transport, cache=cache, config=config)
    assert first.plan_note == PLAN_FAILED
    assert first.rule_tags == []
    assert first.items[0].quote == "VDD 为电源引脚"
    assert second.result_hash == first.result_hash
    assert calls["n"] == 4


def test_unknown_tags_are_dropped_and_known_tags_still_mount(tmp_path):
    transport = _transport(
        [_plan(required_rule_tags=["clock_routing", "power_supply", "power_supply"])],
        [_item()],
    )
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.plan_note == ""
    assert result.rule_tags == GENERAL_TAGS
    assert "LDO 必须有输入和输出去耦电容" in transport.seen[1]["user"]
    assert "射频走线上必须有 DC Block" not in transport.seen[1]["user"]


def test_plan_without_tags_still_mounts_general_rules(tmp_path):
    transport = _transport([_plan(required_rule_tags=[])], [_item(quote="VDD 为电源引脚")])
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.plan_note == ""
    assert result.rule_tags == GENERAL_TAGS
    assert result.active_components == ["U2(TPS7A4700)"]
    user = transport.seen[1]["user"]
    assert "专用说明" in user
    assert "LDO 必须有输入和输出去耦电容" in user
    assert "射频走线上必须有 DC Block" not in user


def test_empty_selection_does_not_invent_optional_rules(tmp_path):
    rules = tmp_path / "packs.json"
    rules.write_text(
        json.dumps(
            {
                "packs": [
                    {
                        "tag": "power_supply",
                        "title": "电源",
                        "hint": "LDO、电源去耦",
                        "text": "LDO 必须有输入和输出去耦电容。",
                        "apply": "tagged",
                    },
                    {
                        "tag": "rf_bias",
                        "title": "射频",
                        "hint": "射频",
                        "text": "射频走线上必须有 DC Block 隔直电容。",
                        "apply": "tagged",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    transport = _transport([_plan(required_rule_tags=[])], [_item(quote="VDD 为电源引脚")])
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
        rules_path=rules,
    )
    assert result.plan_note == NO_RULES
    assert result.rule_tags == []
    assert "LDO 必须有输入和输出去耦电容" not in transport.seen[1]["user"]
    assert "DC Block" not in transport.seen[1]["user"]


def test_rf_rules_mount_only_when_selected(tmp_path):
    transport = _transport([_plan(required_rule_tags=["rf_bias"])], [_item()])
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.rule_tags == [*GENERAL_TAGS, "rf_bias"]
    user = transport.seen[1]["user"]
    assert "射频走线上必须有 DC Block" in user
    assert "π 型或 T 型匹配网络" in user


def test_same_plan_is_reused_from_cache(tmp_path):
    calls = {"n": 0}

    def transport(body: dict) -> str:
        calls["n"] += 1
        if body.get("schema") == "plan":
            return json.dumps(_plan(), ensure_ascii=False)
        return json.dumps({"items": [_item()]}, ensure_ascii=False)

    cache = MemoryCache()
    pdf = _pdf(tmp_path)
    hashes = []
    for _ in range(3):
        result = review_planned(pdf, PRODUCT, _datasheet(), transport=transport, cache=cache, config=CFG)
        hashes.append(result.result_hash)
    assert calls["n"] == 3
    assert hashes[0] == hashes[1] == hashes[2]


def test_datasheet_without_relevant_sections_stops_before_review(tmp_path):
    seen: list[dict] = []

    def transport(body: dict) -> str:
        seen.append(body)
        return json.dumps(_plan(active_components=[], required_rule_tags=[]), ensure_ascii=False)

    try:
        review_planned(
            _pdf(tmp_path),
            PRODUCT,
            "订购代号。" * 12000,
            transport=transport,
            cache=MemoryCache(),
            config=CFG,
        )
    except SchematicInputError as exc:
        assert "引脚" in str(exc)
    else:
        raise AssertionError("没有相关章节时应停止")
    assert len(seen) == 1
    assert seen[0]["schema"] == "plan"


def test_rules_only_review_uses_the_rule_pack_and_says_so(tmp_path):
    transport = _transport([_plan()], [_item()])
    result = review_planned(
        _pdf(tmp_path),
        "",
        "",
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.basis_note == RULES_ONLY_NOTE
    assert result.rule_tags == GENERAL_TAGS
    assert result.items[0].conclusion == "符合"
    plan_call, review_call = transport.seen
    assert "本次没有产品定义" in plan_call["user"]
    assert "结合产品定义" not in plan_call["user"]
    assert "temperature" not in plan_call
    assert "temperature" not in review_call
    assert "没有产品定义和 Datasheet" in review_call["user"]
    assert "LDO 必须有输入和输出去耦电容" in review_call["user"]
    assert "3.2 接口要求" not in review_call["user"]
    assert "射频走线上必须有 DC Block" not in review_call["user"]


def test_failed_plan_without_datasheet_falls_back_to_general_rules(tmp_path):
    transport = _transport(["不是 JSON"], [_item()])
    result = review_planned(
        _pdf(tmp_path),
        "",
        "   ",
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.plan_note == PLAN_FAILED
    assert result.basis_note == RULES_ONLY_NOTE
    assert result.rule_tags == GENERAL_TAGS
    assert "LDO 必须有输入和输出去耦电容" in transport.seen[1]["user"]


def test_product_alone_still_reviews(tmp_path):
    transport = _transport([_plan(required_rule_tags=[])], [_item()])
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        "",
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.basis_note == PRODUCT_ONLY_NOTE
    assert result.rule_tags == GENERAL_TAGS
    user = transport.seen[1]["user"]
    assert "需要一路 I2C 接口" in user
    assert "未提供 Datasheet" in user
    assert "LDO 必须有输入和输出去耦电容" in user


def test_rule_quote_does_not_count_as_datasheet_evidence(tmp_path):
    payload = {
        "items": [
            _item(),
            _item(quote="VDD 为电源引脚", findings="图纸上的 U2 电源脚名为 VDD。"),
        ]
    }

    def transport(body: dict) -> str:
        transport.seen.append(body)
        if body.get("schema") == "plan":
            return json.dumps(_plan(), ensure_ascii=False)
        return json.dumps(payload, ensure_ascii=False)

    transport.seen = []
    result = review_planned(
        _pdf(tmp_path),
        PRODUCT,
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    by_quote = {item.quote: item.requirement_source for item in result.items}
    assert by_quote["LDO 必须有输入和输出去耦电容"] == "审查规则"
    assert by_quote["VDD 为电源引脚"] == "Datasheet"
    assert result.basis_note == ""
    user = transport.seen[1]["user"]
    assert "【Datasheet 依据】" in user
    assert "【审查规则】" in user
    assert "不要用审查规则的句子代替手册原文" in user


def test_datasheet_left_unused_is_stated_on_the_draft(tmp_path):
    transport = _transport([_plan()], [_item()])
    result = review_planned(
        _pdf(tmp_path),
        "",
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.items[0].requirement_source == "审查规则"
    assert result.basis_note == DATASHEET_UNUSED


def test_datasheet_alone_still_crops_and_reviews(tmp_path):
    transport = _transport([_plan()], [_item(quote="VDD 为电源引脚")])
    result = review_planned(
        _pdf(tmp_path),
        "",
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert result.basis_note == DATASHEET_ONLY_NOTE
    user = transport.seen[1]["user"]
    assert "VDD 为电源引脚" in user
    assert "未提供产品定义" in user
    assert "3.2 接口要求" not in user
    assert "封装填充" not in user


def test_part_missing_from_the_datasheet_is_named_without_another_call(tmp_path):
    transport = _transport(
        [_plan(active_components=["U2(TPS7A4700)", "U9(LM5146)"])],
        [_item(quote="VDD 为电源引脚", findings="图纸上的 U2 电源脚名为 VDD。")],
    )
    result = review_planned(
        _pdf(tmp_path),
        "",
        _datasheet(),
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    assert len(transport.seen) == 2
    assert "LM5146" in result.coverage_note
    assert "没有出现在已提供的手册文本里" in result.coverage_note


def test_uncovered_part_gets_its_own_datasheet_pass(tmp_path):
    sheet = (
        "【a.pdf】\n推荐工作\nTPS7A4700 的 VDD 为电源引脚。\n\n"
        "【b.pdf】\n推荐工作\nRTL8127AT 的 DVDDIO 范围为 3.14V 到 3.46V。\n"
    )

    def transport(body: dict) -> str:
        transport.seen.append(body)
        if body.get("schema") == "plan":
            return json.dumps(
                _plan(active_components=["U2(TPS7A4700)", "U9(RTL8127AT)"]),
                ensure_ascii=False,
            )
        if "只为这些位号" in body["user"]:
            return json.dumps(
                {
                    "items": [
                        _item(
                            target_refdes="U9",
                            check_type="numeric",
                            requirement_source="Datasheet",
                            findings="图纸上的 U9 DVDDIO 为 3.3V。",
                            quote="3.14V 到 3.46V",
                            observed={"value": 3.3, "unit": "V"},
                            compare_op="between",
                            limit_value=3.14,
                            limit_high=3.46,
                            limit_unit="V",
                            param_name="dvddio",
                        )
                    ]
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "items": [
                    _item(
                        target_refdes="U2",
                        requirement_source="Datasheet",
                        findings="图纸上的 U2 电源脚名为 VDD。",
                        quote="VDD 为电源引脚",
                    )
                ]
            },
            ensure_ascii=False,
        )

    transport.seen = []
    result = review_planned(
        _pdf(tmp_path),
        "",
        sheet,
        transport=transport,
        cache=MemoryCache(),
        config=CFG,
    )
    by_ref = {item.target_refdes: item for item in result.items}
    assert by_ref["U2"].requirement_source == "Datasheet"
    assert by_ref["U9"].conclusion == "符合"
    assert by_ref["U9"].method == "rule-engine"
    assert any("只为这些位号" in body["user"] and "U9" in body["user"] for body in transport.seen)
    assert "分段补审" in result.coverage_note


def test_rules_only_without_packs_stops_before_review(tmp_path):
    rules = tmp_path / "packs.json"
    rules.write_text(json.dumps({"packs": []}), encoding="utf-8")
    seen: list[dict] = []

    def transport(body: dict) -> str:
        seen.append(body)
        return json.dumps(_plan(), ensure_ascii=False)

    try:
        review_planned(
            _pdf(tmp_path),
            "",
            "",
            transport=transport,
            cache=MemoryCache(),
            config=CFG,
            rules_path=rules,
        )
    except SchematicInputError as exc:
        assert "没有可引用的依据" in str(exc)
    else:
        raise AssertionError("没有依据时应停止")
    assert len(seen) == 1
    assert seen[0]["schema"] == "plan"
