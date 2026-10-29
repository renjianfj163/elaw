"""合成资料上的设计约束审查。数字和型号都是测试夹具，不是 Laser C8750 的实测结论。"""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.main import app
from app.schematic.engine.model import Circuit
from app.schematic.knowledge import review_changes, review_with_knowledge
from app.schematic.knowledge.extract import extract_text
from app.schematic.knowledge.model import KnowledgeError
from app.schematic.knowledge.store import KnowledgeStore

FIXTURES = Path(__file__).parent / "fixtures" / "design_knowledge"
QUOTE = "GPIO12 absolute maximum voltage is 1.8 V."


def pin(number, net, **kwargs):
    body = {"number": str(number), "net": net}
    body.update(kwargs)
    return body


def part(refdes, kind, pins, **kwargs):
    body = {"refdes": refdes, "kind": kind, "fitted": True, "pins": pins}
    body.update(kwargs)
    return refdes, body


def board(parts, **kwargs):
    data = {"name": "synthetic", "source": "netlist", "connectivity_complete": True, "components": dict(parts)}
    data.update(kwargs)
    return Circuit.model_validate(data)


def _store(tmp_path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path / "knowledge")


def _load(store: KnowledgeStore, name: str, **kwargs):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    document = store.import_text(title=kwargs.pop("title", name), text=text, filename=name, **kwargs)
    for item in store.query(document_id=document.document_id):
        store.verify(item.constraint_id)
    return document


def _gpio_circuit(*extra, mpn="SYN-SOM-180", abs_max_v=None):
    gpio = pin(12, "GPIO", name="GPIO12", pin_type="gpio", direction="input", voltage_domain="V1V8")
    if abs_max_v is not None:
        gpio["abs_max_v"] = abs_max_v
    return board(
        [
            part("U1", "ic", [gpio], role="som", mpn=mpn, page=4, manufacturer="Synthetic", package="MODULE"),
            part("R1", "resistor", [pin(1, "GPIO"), pin(2, "MID")], value="10k", page=4),
            part("R0", "jumper", [pin(1, "MID"), pin(2, "V12")], value="0", page=4),
            *extra,
        ],
        power_domains=[
            {"name": "12V", "net": "V12", "nominal_v": 12},
            {"name": "V1V8", "net": "V1V8", "nominal_v": 1.8},
        ],
    )


def _review(circuit, store, rules):
    return review_with_knowledge(circuit, store, only=rules)


def _statuses(report, rule_id):
    return [item.status for item in report.findings if item.rule_id == rule_id]


def test_text_import_records_source_and_rejects_unsupported_quotes(tmp_path):
    store = _store(tmp_path)
    document = _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0", project_id="synth", product_id="board")
    assert document.parse_status == "parsed"
    assert document.revision == "1.0"
    assert document.sha256
    saved = store.query(mpn="SYN-SOM-180", pin="12", category="electrical", verified_only=True, current_only=True)
    assert len(saved) == 1
    item = saved[0]
    assert item.source_quote == QUOTE
    assert item.source_page == 2
    assert item.source_section == "3.2 GPIO bank"
    assert item.origin == "extracted_candidate"
    assert item.verification_status == "verified"
    assert item.project_id == "synth"
    rejected = extract_text(
        ":::constraint\ncategory: electrical\ntitle: 缺页\nconstraint_type: hard_limit\nparameter: abs_max_v\nmax: 1.8\nunit: V\nquote: not in the prose\n:::"
    )
    assert rejected.constraints == []
    assert rejected.rejected


def test_blank_pdf_is_not_treated_as_parsed(tmp_path):
    store = _store(tmp_path)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    payload = io.BytesIO()
    writer.write(payload)
    document = store.import_pdf(title="空白扫描页（合成）", payload=payload.getvalue(), kind="datasheet", revision="0")
    assert document.parse_status == "not_parsed"
    assert store.query(document_id=document.document_id) == []
    assert 1 in document.unparsed_pages


def test_pdf_page_keeps_only_text_that_is_actually_there(tmp_path):
    store = _store(tmp_path)
    prose = "GPIO12 absolute maximum voltage is 1.8 V."
    block = "\n".join(
        [
            ":::constraint",
            "category: electrical",
            "title: GPIO12 absolute maximum",
            "constraint_type: hard_limit",
            "parameter: abs_max_v",
            "max: 1.8",
            "unit: V",
            "mpn: SYN-SOM-180",
            "pin: 12",
            "source_page: 1",
            "source_section: 3.2",
            f"quote: {prose}",
            ":::",
        ]
    )
    payload = _pdf_lines([prose, *block.splitlines(), ""])
    document = store.import_pdf(title="SYN-SOM PDF（合成）", payload=payload, kind="som_guide", revision="1.0", filename="som.pdf")
    assert document.parse_status == "parsed"
    item = store.query(document_id=document.document_id)[0]
    assert item.source_page == 1
    assert item.verification_status == "unverified"
    store.verify(item.constraint_id)
    assert store.constraints[item.constraint_id].verification_status == "verified"


def test_gpio_constraint_changes_the_review(tmp_path):
    store = _store(tmp_path)
    document = _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    circuit = _gpio_circuit()
    report = _review(circuit, store, ["PWR-001"])
    confirmed = [item for item in report.findings if item.rule_id == "PWR-001" and item.status == "confirmed"]
    assert len(confirmed) == 1
    item = confirmed[0]
    assert item.severity == "critical"
    assert "U1" in item.affected_components
    assert "R1" in item.affected_components
    assert "GPIO" in item.affected_nets
    assert "V12" in item.affected_nets
    assert item.source_pages == [4]
    assert item.calculation is not None
    assert "Vmax" in item.calculation.inputs
    assert "Vbias" in item.calculation.inputs
    assert item.calculation.evaluator_trace
    assert item.calculation.value == pytest.approx(0.0012)
    evidence = item.constraint_evidence[0]
    assert evidence.document_title == "SYN-SOM-180 硬件设计指南（合成）"
    assert evidence.document_revision == "1.0"
    assert evidence.source_page == 2
    assert evidence.source_section == "3.2 GPIO bank"
    assert evidence.source_quote == QUOTE
    assert "U1.12" in item.description
    assert "1.8" in item.description

    constraint_id = store.query(document_id=document.document_id, pin="12")[0].constraint_id
    store.withdraw(constraint_id)
    withdrawn = _review(circuit, store, ["PWR-001"])
    assert "confirmed" not in _statuses(withdrawn, "PWR-001")
    assert withdrawn.findings
    assert withdrawn.findings[0].status in {"needs_review", "not_assessable"}
    assert withdrawn.findings[0].constraint_evidence == []

    store.restore(constraint_id)
    restored = _review(circuit, store, ["PWR-001"])
    assert _statuses(restored, "PWR-001") == ["confirmed"]
    assert restored.findings[0].constraint_evidence[0].source_quote == QUOTE


def test_safe_divider_is_not_the_same_fault(tmp_path):
    store = _store(tmp_path)
    _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    circuit = board(
        [
            part(
                "U1",
                "ic",
                [pin(12, "GPIO", name="GPIO12", pin_type="gpio", direction="input", voltage_domain="V1V8")],
                role="som",
                mpn="SYN-SOM-180",
                page=4,
            ),
            part("RT", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="100k"),
            part("RB", "resistor", [pin(1, "GPIO"), pin(2, "GND")], value="10k"),
        ],
        power_domains=[{"name": "12V", "net": "V12", "nominal_v": 12}, {"name": "V1V8", "net": "V1V8", "nominal_v": 1.8}],
    )
    report = _review(circuit, store, ["PWR-001"])
    assert "confirmed" not in _statuses(report, "PWR-001")
    assert report.findings == []


def test_missing_rating_or_mpn_is_not_a_pass(tmp_path):
    store = _store(tmp_path)
    document = _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    circuit = _gpio_circuit(mpn="")
    report = _review(circuit, store, ["PWR-001"])
    assert "confirmed" not in _statuses(report, "PWR-001")
    assert report.findings[0].status in {"needs_review", "not_assessable"}

    unverified = KnowledgeStore(tmp_path / "unverified")
    text = (FIXTURES / "som_guide.txt").read_text(encoding="utf-8")
    unverified.import_text(title="未复核指南", text=text, kind="som_guide", revision="1.0")
    pending = _review(_gpio_circuit(), unverified, ["PWR-001"])
    assert pending.findings[0].status == "needs_review"
    assert "尚未复核" in pending.findings[0].description
    assert "confirmed" not in _statuses(pending, "PWR-001")

    constraint_id = store.query(document_id=document.document_id, pin="12")[0].constraint_id
    store.withdraw(constraint_id)
    removed = _review(_gpio_circuit(), store, ["PWR-001"])
    assert removed.verdict != "clear"
    assert "confirmed" not in _statuses(removed, "PWR-001")


def test_ambiguous_mpn_is_not_auto_selected(tmp_path):
    store = _store(tmp_path)
    for suffix, limit in (("A", "1.8"), ("B", "3.3")):
        prose = f"GPIO12 absolute maximum voltage is {limit} V."
        text = f"{prose}\n" + _block(
            {
                "category": "electrical",
                "title": f"订货号 {suffix}",
                "constraint_type": "hard_limit",
                "parameter": "abs_max_v",
                "max": limit,
                "unit": "V",
                "mpn": f"SYN-SOM-180{suffix}",
                "pin": "12",
                "source_page": "1",
                "source_section": suffix,
                "quote": prose,
            }
        )
        document = store.import_text(title=f"订货号 {suffix}（合成）", text=text, kind="datasheet", revision="1")
        for item in store.query(document_id=document.document_id):
            store.verify(item.constraint_id)
    report = _review(_gpio_circuit(mpn="SYN-SOM-180"), store, ["PWR-001"])
    assert "confirmed" not in _statuses(report, "PWR-001")
    assert "多个可能匹配" in report.findings[0].description


def test_revision_marks_old_constraints_and_does_not_overwrite(tmp_path):
    store = _store(tmp_path)
    document = _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    before = _review(_gpio_circuit(), store, ["PWR-001"])
    assert _statuses(before, "PWR-001") == ["confirmed"]
    old_ids = [item.constraint_id for item in store.query(document_id=document.document_id)]
    affected = store.update_revision(document.document_id, revision="2.0", text="这一版正文不再包含原来的电压句子。\n")
    assert set(old_ids) <= set(affected)
    assert set(store.affected_by(document.document_id)) >= set(old_ids)
    with pytest.raises(KnowledgeError):
        store.restore(old_ids[0])
    revised = _review(_gpio_circuit(), store, ["PWR-001"])
    assert "confirmed" not in _statuses(revised, "PWR-001")

    fresh = KnowledgeStore(tmp_path / "later")
    first = _load(fresh, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    later = (FIXTURES / "som_later_limit.txt").read_text(encoding="utf-8")
    second = fresh.import_text(title="后上传的不同限值（合成）", text=later, kind="datasheet", revision="9")
    for item in fresh.query(document_id=second.document_id):
        fresh.verify(item.constraint_id)
    both = fresh.query(pin="12", current_only=True, verified_only=True)
    assert {item.parameters[0].max_value for item in both} == {1.8, 3.6}
    assert fresh.documents[first.document_id].revision == "1.0"
    conflicted = _review(_gpio_circuit(), fresh, ["PWR-001", "KN-001"])
    assert "confirmed" not in _statuses(conflicted, "PWR-001")
    kn = [item for item in conflicted.findings if item.rule_id == "KN-001"]
    assert kn and kn[0].status == "needs_review"
    assert "1.8" in kn[0].description and "3.6" in kn[0].description
    assert len(kn[0].constraint_evidence) == 2


def test_product_and_device_limits_stay_in_conflict(tmp_path):
    store = _store(tmp_path)
    _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    _load(store, "product_gpio.txt", title="产品接口要求（合成）", kind="product_spec", revision="A")
    report = _review(_gpio_circuit(), store, ["PWR-001", "KN-001"])
    assert "confirmed" not in _statuses(report, "PWR-001")
    kn = [item for item in report.findings if item.rule_id == "KN-001"][0]
    assert kn.status == "needs_review"
    assert "3.3" in kn.description
    assert QUOTE in kn.description
    assert "The GPIO12 interface shall be 3.3 V." in kn.description


def test_fixing_the_circuit_clears_the_previous_finding(tmp_path):
    store = _store(tmp_path)
    _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    before = _review(_gpio_circuit(), store, ["PWR-001"])
    fixed = board(
        [
            part(
                "U1",
                "ic",
                [pin(12, "GPIO", name="GPIO12", pin_type="gpio", direction="input", voltage_domain="V1V8")],
                role="som",
                mpn="SYN-SOM-180",
                page=4,
            ),
            part("RT", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="100k", page=4),
            part("RB", "resistor", [pin(1, "GPIO"), pin(2, "GND")], value="10k", page=4),
        ],
        power_domains=[{"name": "12V", "net": "V12", "nominal_v": 12}, {"name": "V1V8", "net": "V1V8", "nominal_v": 1.8}],
    )
    after = _review(fixed, store, ["PWR-001"])
    changes = review_changes(before, after)
    assert changes == [{"rule_id": "PWR-001", "from": "confirmed", "to": "cleared", "finding_id": before.findings[0].finding_id}]
    assert after.findings == []


def test_level_shifter_bypass_uses_the_datasheet(tmp_path):
    store = _store(tmp_path)
    document = _load(store, "shifter_datasheet.txt", title="SYN-TXS-2 数据手册（合成）", kind="datasheet", revision="B")
    circuit = board(
        [
            part(
                "U2",
                "ic",
                [pin("A1", "A"), pin("B1", "B")],
                role="level_shifter",
                mpn="SYN-TXS-2",
                page=3,
                channels=[{"a_pin": "A1", "b_pin": "B1"}],
            ),
            part("R0", "jumper", [pin(1, "A"), pin(2, "B")], value="0", page=3),
        ]
    )
    report = _review(circuit, store, ["LS-001"])
    item = report.findings[0]
    assert item.status == "confirmed"
    assert item.constraint_evidence[0].source_quote == "Do not place a resistive bypass across a channel."
    assert item.constraint_evidence[0].source_section == "7.4 Channel use"
    assert item.source_pages == [3]
    assert {"U2", "R0"} <= set(item.affected_components)
    store.withdraw(store.query(document_id=document.document_id)[0].constraint_id)
    without = _review(circuit, store, ["LS-001"])
    assert without.findings[0].status == "needs_review"
    assert "confirmed" not in _statuses(without, "LS-001")


def test_ldo_capacitance_uses_verified_limit_and_refuses_missing_derating(tmp_path):
    store = _store(tmp_path)
    _load(store, "ldo_datasheet.txt", title="SYN-LDO-33 数据手册（合成）", kind="datasheet", revision="C")
    low = _review(_ldo("1uF"), store, ["REG-003"])
    assert low.findings[0].status == "confirmed"
    assert low.findings[0].constraint_evidence[0].source_quote == "The minimum output capacitance is 10 uF."
    assert low.findings[0].constraint_evidence[0].source_page == 4
    assert low.findings[0].calculation is not None

    nominal = _review(_ldo("22uF"), store, ["REG-003"])
    assert nominal.findings[0].status == "needs_review"
    assert "降容" in nominal.findings[0].description
    assert "confirmed" not in _statuses(nominal, "REG-003")

    empty = KnowledgeStore(tmp_path / "no-ldo")
    missing = _review(_ldo("22uF"), empty, ["REG-003"])
    assert missing.findings[0].status == "not_assessable"
    assert missing.verdict != "clear"

    recommended = KnowledgeStore(tmp_path / "recommended")
    prose = "10 uF is a recommended example, not a requirement."
    text = prose + "\n" + _block(
        {
            "category": "power",
            "title": "推荐输出电容",
            "constraint_type": "recommended",
            "parameter": "cout_min",
            "value": "10",
            "unit": "uF",
            "mpn": "SYN-LDO-33",
            "source_page": "1",
            "source_section": "example",
            "quote": prose,
        }
    )
    document = recommended.import_text(title="推荐值（合成）", text=text, kind="datasheet", revision="1")
    recommended.verify(recommended.query(document_id=document.document_id)[0].constraint_id)
    advice = _review(_ldo("1uF"), recommended, ["REG-003"])
    assert "confirmed" not in _statuses(advice, "REG-003")
    assert "推荐" in advice.findings[0].description


def test_sdio_mapping_comes_from_pin_constraints(tmp_path):
    store = _store(tmp_path)
    _load(store, "som_guide.txt", title="SYN-SOM-180 硬件设计指南（合成）", kind="som_guide", revision="1.0")
    _load(store, "wifi_module.txt", title="SYN-WIFI-1 数据手册（合成）", kind="datasheet", revision="2")
    _load(store, "product_sdio.txt", title="产品接口要求（合成）", kind="product_spec", revision="A")
    crossed = _review(_sdio(b0="D1", b1="D0"), store, ["IF-001", "KN-001"])
    faults = [item for item in crossed.findings if item.rule_id == "IF-001" and item.status == "confirmed"]
    assert faults
    quotes = " ".join(item.source_quote for finding in faults for item in finding.constraint_evidence)
    assert "Host SDIO D0 is pin 1." in quotes
    assert "Device SDIO D0 is pin 1." in quotes
    straight = _review(_sdio(b0="D0", b1="D1"), store, ["IF-001", "KN-001"])
    assert [item for item in straight.findings if item.status == "confirmed"] == []

    unverified = KnowledgeStore(tmp_path / "roles")
    for name, title, kind in (
        ("som_guide.txt", "SYN-SOM-180 硬件设计指南（合成）", "som_guide"),
        ("wifi_module.txt", "SYN-WIFI-1 数据手册（合成）", "datasheet"),
        ("product_sdio.txt", "产品接口要求（合成）", "product_spec"),
    ):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        unverified.import_text(title=title, text=text, kind=kind, revision="1")
    product = [item for item in unverified.constraints.values() if item.parameters[0].name == "require_interface"][0]
    unverified.verify(product.constraint_id)
    unknown = _review(_sdio(b0="D1", b1="D0"), unverified, ["IF-001", "KN-001"])
    assert "confirmed" not in _statuses(unknown, "IF-001")
    assert any(item.rule_id == "KN-001" and item.status == "not_assessable" for item in unknown.findings)


def test_boot_path_cites_timing_and_does_not_invent_a_failure(tmp_path):
    store = _store(tmp_path)
    _load(store, "mcu_boot.txt", title="SYN-MCU-10 启动说明（合成）", kind="datasheet", revision="3")
    report = _review(_boot(), store, ["RST-004"])
    item = report.findings[0]
    assert item.status == "needs_review"
    assert item.status != "confirmed"
    assert item.constraint_evidence[0].source_quote == "The level-shifter OE is not defined during reset."
    assert item.constraint_evidence[0].source_page == 9
    assert item.constraint_evidence[0].source_section == "3.4 Boot"
    bare = _review(_boot(), KnowledgeStore(tmp_path / "bare"), ["RST-004"])
    assert bare.findings[0].status == "needs_review"
    assert "boot_timing" in bare.findings[0].required_missing_inputs
    assert bare.findings[0].constraint_evidence == []


def test_workbench_exposes_constraint_import_and_review():
    html = (Path(__file__).parents[1] / "app" / "static" / "workbench.html").read_text(encoding="utf-8")
    assert 'id="knowledgeForm"' in html
    assert 'id="knowledgeList"' in html
    assert 'data-verify=' in html
    assert 'id="netlistForm"' in html
    assert 'id="knowledgeResult"' in html
    assert "constraint_evidence" in html


def test_existing_engine_still_confirms_without_the_knowledge_store():
    circuit = _gpio_circuit(abs_max_v=3.6)
    from app.schematic.engine.runner import review_circuit

    report = review_circuit(circuit, only=["PWR-001"])
    assert [item.status for item in report.findings] == ["confirmed"]
    assert report.findings[0].constraint_evidence == []


def test_knowledge_review_api_uses_the_imported_constraint(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(app)
    text = (FIXTURES / "som_guide.txt").read_text(encoding="utf-8")
    imported = client.post(
        "/api/schematic/knowledge/import",
        data={"title": "SYN-SOM-180 硬件设计指南（合成）", "kind": "som_guide", "revision": "1.0", "text": text},
    )
    assert imported.status_code == 200
    document = imported.json()["document"]
    assert document["parse_status"] == "parsed"
    for item in imported.json()["constraints"]:
        if item["parameters"][0]["name"] != "abs_max_v":
            continue
        verified = client.post(f"/api/schematic/knowledge/constraints/{item['constraint_id']}/verify")
        assert verified.status_code == 200
    listed = client.get("/api/schematic/knowledge/constraints", params={"mpn": "SYN-SOM-180", "verified_only": True})
    assert "documents" in listed.json()
    assert any(item["parameters"][0]["name"] == "abs_max_v" for item in listed.json()["constraints"])
    reviewed = client.post("/api/schematic/knowledge/review", json=_gpio_circuit().model_dump())
    assert reviewed.status_code == 200
    body = reviewed.json()
    gpio = [item for item in body["findings"] if item["rule_id"] == "PWR-001" and item["status"] == "confirmed"]
    assert len(gpio) == 1
    assert gpio[0]["constraint_evidence"][0]["source_quote"] == QUOTE
    assert gpio[0]["source_pages"] == [4]


def _ldo(cap):
    return board(
        [
            part(
                "U1",
                "ic",
                [pin("FB", "FB", pin_type="feedback"), pin("OUT", "VOUT", pin_type="vout")],
                role="regulator",
                mpn="SYN-LDO-33",
                page=5,
            ),
            part("C1", "capacitor", [pin(1, "VOUT"), pin(2, "GND")], value=cap, page=5),
        ]
    )


def _sdio(b0, b1):
    return board(
        [
            part("U1", "ic", [pin(1, "H0", pin_type="signal"), pin(2, "H1", pin_type="signal")], role="som", mpn="SYN-SOM-180", page=2),
            part(
                "U2",
                "ic",
                [pin("A1", "H0"), pin("B1", b0), pin("A2", "H1"), pin("B2", b1)],
                role="level_shifter",
                mpn="SYN-TXS-2",
                page=2,
                channels=[{"a_pin": "A1", "b_pin": "B1"}, {"a_pin": "A2", "b_pin": "B2"}],
            ),
            part("U3", "ic", [pin(1, "D0", pin_type="signal"), pin(2, "D1", pin_type="signal")], role="module", mpn="SYN-WIFI-1", page=2),
        ]
    )


def _boot():
    return board(
        [
            part(
                "U1",
                "ic",
                [pin(1, "BOOT", pin_type="boot"), pin(2, "OE", pin_type="gpio", direction="output")],
                role="mcu",
                mpn="SYN-MCU-10",
                page=1,
            ),
            part(
                "U2",
                "ic",
                [pin("A1", "BOOT"), pin("B1", "BOOT_EXT"), pin("OE", "OE", pin_type="oe", name="OE")],
                role="level_shifter",
                mpn="SYN-TXS-2",
                page=1,
                channels=[{"a_pin": "A1", "b_pin": "B1"}],
            ),
        ]
    )


def _block(fields: dict[str, str]) -> str:
    lines = [":::constraint", *[f"{key}: {value}" for key, value in fields.items()], ":::"]
    return "\n".join(lines)


def _pdf_lines(lines: list[str]) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    commands = ["BT", "/F1 10 Tf", "72 720 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"({escaped}) Tj")
        commands.append("0 -14 Td")
    commands.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("latin-1"))
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    payload = io.BytesIO()
    writer.write(payload)
    return payload.getvalue()
