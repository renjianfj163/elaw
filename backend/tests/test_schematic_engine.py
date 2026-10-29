"""合成网表上的确定性审查。不把 Laser C8750 的原报告当成答案。"""

import pytest

from app.schematic.engine.catalog import catalog_path, load_catalog
from app.schematic.engine.dc import CircuitView
from app.schematic.engine.model import Circuit
from app.schematic.engine.runner import CHECKERS, review_circuit
from app.schematic.engine.values import parse_capacitance, parse_resistance


def pin(number, net, **kwargs):
    body = {"number": str(number), "net": net}
    body.update(kwargs)
    return body


def part(refdes, kind, pins, **kwargs):
    body = {"refdes": refdes, "kind": kind, "fitted": True, "pins": pins}
    body.update(kwargs)
    return refdes, body


def board(parts, **kwargs):
    data = {"source": "netlist", "connectivity_complete": True, "components": dict(parts)}
    data.update(kwargs)
    return Circuit.model_validate(data)


def only(circuit, rule_id):
    report = review_circuit(circuit, only=[rule_id])
    return report, [item for item in report.findings if item.rule_id == rule_id]


def test_parsers_do_not_guess_units():
    assert parse_resistance("4k7") == 4700
    assert parse_resistance("10k") == 10000
    assert parse_resistance("0R") == 0
    assert parse_resistance("33") == 33
    assert parse_capacitance("22pF") == pytest.approx(22e-12)
    assert parse_capacitance("10uF") == pytest.approx(10e-6)
    assert parse_capacitance("100nF") == pytest.approx(100e-9)
    assert parse_capacitance("22") is None


def test_bias_solver_pullup_divider_and_jumper():
    circuit = board(
        [
            part("R1", "resistor", [pin(1, "GPIO"), pin(2, "MID")], value="10k"),
            part("R0", "jumper", [pin(1, "MID"), pin(2, "V12")], value="0"),
            part("RT", "resistor", [pin(1, "DIV"), pin(2, "V12")], value="30k"),
            part("RB", "resistor", [pin(1, "DIV"), pin(2, "GND")], value="10k"),
        ],
        power_domains=[{"name": "12V", "net": "V12", "nominal_v": 12}],
    )
    view = CircuitView(circuit)
    pulled = view.bias("GPIO")
    assert round(pulled.voltage, 6) == 12
    assert round(pulled.rth, 3) == 10000
    divided = view.bias("DIV")
    assert round(divided.voltage, 6) == 3
    assert round(divided.rth, 3) == 7500


def test_twelve_volt_pullup_is_confirmed_from_the_path():
    report, items = only(_gpio_12v(), "PWR-001")
    assert report.verdict == "findings"
    assert len(items) == 1
    item = items[0]
    assert item.status == "confirmed"
    assert item.severity == "critical"
    assert item.confidence == "high"
    assert {"R1", "R0"} <= set(item.affected_components)
    assert item.source_pages == [4]


def test_divider_to_safe_voltage_is_not_flagged():
    circuit = _mcu(
        part("RT", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="30k"),
        part("RB", "resistor", [pin(1, "GPIO"), pin(2, "GND")], value="10k"),
    )
    assert only(circuit, "PWR-001")[1] == []


def test_missing_abs_max_is_not_called_damage():
    circuit = _mcu(
        part("R1", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="10k"),
        abs_max_v=None,
        voltage_domain="V1V8",
    )
    items = only(circuit, "PWR-001")[1]
    assert items[0].status == "needs_review"
    assert items[0].severity == "high"
    assert "不能认定" in items[0].description


def test_net_name_alone_does_not_create_a_rail():
    circuit = board(
        [part("U1", "ic", [pin(1, "EN_12V", pin_type="gpio", abs_max_v=3.6)], role="mcu", mpn="MCU")]
    )
    assert only(circuit, "PWR-001")[1] == []


def test_unknown_rail_voltage_is_not_assessable():
    circuit = _mcu(part("R1", "resistor", [pin(1, "GPIO"), pin(2, "VUNK")], value="10k"))
    circuit.nets["VUNK"].is_power = True
    items = only(circuit, "PWR-001")[1]
    assert items[0].status == "not_assessable"


def test_unfitted_resistor_removes_the_path():
    circuit = _mcu(part("R1", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="10k", fitted=False))
    assert only(circuit, "PWR-001")[1] == []


def test_unknown_assembly_downgrades_confirmation():
    circuit = _mcu(part("R1", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="10k", fitted=None))
    assert only(circuit, "PWR-001")[1][0].status == "probable"


def test_pdf_connection_is_not_confirmed():
    circuit = _gpio_12v()
    circuit.source = "pdf"
    item = only(circuit, "PWR-001")[1][0]
    assert item.status == "probable"
    assert any("网表" in note for note in item.assumptions)


def test_en_pullup_is_separate_from_a_name():
    circuit = board(
        [
            part("U1", "ic", [pin("EN", "EN", pin_type="en", name="EN", abs_max_v=5.5)], role="regulator", mpn="LDO"),
            part("R1", "resistor", [pin(1, "EN"), pin(2, "V12")], value="100k"),
        ],
        power_domains=[{"name": "12V", "net": "V12", "nominal_v": 12}],
    )
    assert only(circuit, "PWR-002")[1][0].status == "confirmed"
    assert only(circuit, "PWR-001")[1] == []


def test_sink_current_uses_the_engine():
    circuit = _mcu(
        part("R1", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="1k"),
        iol_max_a=0.001,
        vol_v=0.4,
    )
    item = only(circuit, "PWR-003")[1][0]
    assert item.status == "confirmed"
    assert item.calculation.value > 0.001


def test_sink_without_limit_is_not_a_pass():
    circuit = _mcu(part("R1", "resistor", [pin(1, "GPIO"), pin(2, "V3V3")], value="10k"), iol_max_a=None)
    report, items = only(circuit, "PWR-003")
    assert report.verdict == "not_assessable"
    assert items[0].status == "not_assessable"


def test_backpower_without_ioff_is_not_confirmed_damage():
    circuit = _mcu(part("R1", "resistor", [pin(1, "GPIO"), pin(2, "V12")], value="10k"), supply_net="V1V8")
    item = only(circuit, "PWR-004")[1][0]
    assert item.status == "needs_review"


def test_en_without_bias_needs_review():
    circuit = board(
        [
            part(
                "U1",
                "ic",
                [pin(1, "EN", pin_type="en", name="EN"), pin(2, "EN", pin_type="gpio", direction="output")],
                role="mcu",
                mpn="MCU",
            )
        ]
    )
    assert only(circuit, "PWR-005")[1][0].status == "needs_review"


def test_resistive_bypass_is_not_declared_dead_or_fine():
    item = only(_shifter(part("R33", "resistor", [pin(1, "A"), pin(2, "B")], value="33")), "LS-001")[1][0]
    assert item.status == "probable"
    assert "仍然有效" in item.description
    assert "已经失效" in item.description


def test_zero_ohm_bypass_is_confirmed():
    item = only(_shifter(part("R0", "jumper", [pin(1, "A"), pin(2, "B")], value="0")), "LS-001")[1][0]
    assert item.status == "confirmed"
    assert item.severity == "critical"


def test_separate_pullups_are_not_a_bypass():
    circuit = _shifter(
        part("RA", "resistor", [pin(1, "A"), pin(2, "V3V3")], value="10k"),
        part("RB", "resistor", [pin(1, "B"), pin(2, "V3V3")], value="10k"),
    )
    assert only(circuit, "LS-001")[1] == []


def test_sdio_cross_uses_signal_identity_not_channel_number():
    items = only(_sdio("H0", "D1", "H1", "D0"), "IF-001")[1]
    assert items and {item.status for item in items} == {"confirmed"}
    assert only(_sdio("H1", "D1", "H0", "D0"), "IF-001")[1] == []


def test_uart_direction():
    assert any(item.status == "confirmed" for item in only(_uart("TX", "RX", "RX", "TX"), "IF-003")[1])
    assert only(_uart("TX", "RX", "TX", "RX"), "IF-003")[1] == []


def test_series_resistor_is_not_a_uart_bypass():
    circuit = _uart("TX", "RX", "RXB", "TXB")
    data = circuit.model_dump()
    data["components"]["U2"]["channels"] = [{"a_pin": "A1", "b_pin": "B1"}, {"a_pin": "A2", "b_pin": "B2"}]
    data["components"]["U2"]["pins"].extend(
        [pin("A1", "TXA"), pin("B1", "RXB"), pin("A2", "RX"), pin("B2", "TXB")]
    )
    data["components"]["Rser"] = {
        "refdes": "Rser",
        "kind": "resistor",
        "fitted": True,
        "value": "33",
        "pins": [pin(1, "TX"), pin(2, "TXA")],
    }
    assert only(Circuit.model_validate(data), "IF-004")[1] == []


def test_boot_oe_dependency_is_not_a_proven_failure():
    circuit = board(
        [
            part(
                "U1",
                "ic",
                [pin(1, "BOOT", pin_type="boot"), pin(2, "OE", pin_type="gpio", direction="output")],
                role="mcu",
                mpn="MCU",
            ),
            part(
                "U2",
                "ic",
                [pin("A1", "BOOT"), pin("B1", "BOOT_EXT"), pin("OE", "OE", pin_type="oe", name="OE")],
                role="level_shifter",
                mpn="TXS",
                channels=[{"a_pin": "A1", "b_pin": "B1"}],
            ),
        ]
    )
    item = only(circuit, "RST-004")[1][0]
    assert item.status == "needs_review"
    assert item.severity == "high"
    assert "不能证明启动失败" in item.description


def test_feedback_uses_net_positions_and_refuses_missing_vref():
    item = only(_regulator("100k", "10k", 0.6), "REG-001")[1][0]
    assert item.status == "confirmed"
    assert item.calculation.value == 6.6
    report, missing = only(_regulator("10k", "10k", None), "REG-001")
    assert report.verdict == "not_assessable"
    assert missing[0].calculation.value is None
    assert only(_regulator("100k", "10k", 0.6, verified=False), "REG-001")[1][0].status == "not_assessable"


def test_output_capacitance_needs_a_verified_limit_and_derating():
    assert only(_regulator("10k", "10k", 0.6, cap="1uF", cout_min=10), "REG-003")[1][0].status == "confirmed"
    assert only(_regulator("10k", "10k", 0.6, cap="22uF", cout_min=None), "REG-003")[0].verdict == "not_assessable"
    assert only(_regulator("10k", "10k", 0.6, cap="22uF", cout_min=10, bias=None), "REG-003")[1][0].status == "needs_review"
    assert only(_regulator("10k", "10k", 0.6, cap="22uF", cout_min=10, bias=0.8, tolerance=10), "REG-003")[1] == []


def test_crystal_load_keeps_the_approximation_visible():
    item = only(_crystal("22pF", "22pF", 2, 8, 10), "ANA-001")[1][0]
    assert item.status == "confirmed"
    assert "Cstray" in item.calculation.formula
    partial = only(_crystal("22pF", "22pF", None, 12, 14), "ANA-001")[1][0]
    assert partial.status == "needs_review"
    assert "c_stray" in partial.calculation.missing
    mismatch = _crystal("22pF", "22pF", 2, 12, 14)
    mismatch.components["C1"].comment = "12pF"
    note = only(mismatch, "ANA-002")[1][0]
    assert note.status == "confirmed"
    assert note.severity == "low"


def test_two_usb_phys_respect_assembly_state():
    assert only(_usb(True, True), "USB-001")[1][0].status == "confirmed"
    assert only(_usb(True, False), "USB-001")[1] == []
    assert only(_usb(None, None), "USB-001")[1][0].status == "needs_review"


def test_empty_circuit_is_not_a_pass():
    report = review_circuit(Circuit())
    assert report.verdict == "no_applicable_rules"
    assert report.findings == []
    assert "RST-006" in report.unimplemented
    assert "IF-005" not in report.unimplemented


def test_catalog_matches_the_checkers():
    version, rules = load_catalog()
    assert version == "1"
    assert len(rules) == 67
    assert set(CHECKERS) <= {item.rule_id for item in rules}
    text = catalog_path().read_text(encoding="utf-8")
    assert "TXB0108" not in text
    assert "TLV755" not in text


def test_push_pull_contention_and_open_drain_do_not_mix():
    shared = board(
        [
            part("U1", "ic", [pin(1, "BUS", direction="output", output_kind="push_pull")], mpn="A"),
            part("U2", "ic", [pin(1, "BUS", direction="output", output_kind="push_pull")], mpn="B"),
        ]
    )
    assert only(shared, "IO-004")[1][0].status == "confirmed"
    drains = board(
        [
            part("U1", "ic", [pin(1, "BUS", direction="output", output_kind="open_drain")], mpn="A"),
            part("U2", "ic", [pin(1, "BUS", direction="output", output_kind="open_drain")], mpn="B"),
            part("R1", "resistor", [pin(1, "BUS"), pin(2, "V3V3")], value="4.7k"),
        ],
        power_domains=[{"name": "V3V3", "net": "V3V3", "nominal_v": 3.3}],
    )
    assert only(drains, "IO-004")[1] == []
    assert only(drains, "IO-003")[1] == []


def test_healthy_board_has_no_confirmed_or_probable_faults():
    report = review_circuit(_healthy())
    bad = [item for item in report.findings if item.status in {"confirmed", "probable"}]
    assert bad == []
    assert report.verdict == "clear"


def _gpio_12v():
    return _mcu(
        part("R1", "resistor", [pin(1, "GPIO"), pin(2, "MID")], value="10k"),
        part("R0", "jumper", [pin(1, "MID"), pin(2, "V12")], value="0"),
        abs_max_v=3.6,
        page=4,
        voltage_domain="V1V8",
    )


def _mcu(*extra, abs_max_v=3.6, voltage_domain="V3V3", iol_max_a=0.02, vol_v=None, page=None, supply_net=None):
    gpio = pin(1, "GPIO", pin_type="gpio", direction="output", abs_max_v=abs_max_v, voltage_domain=voltage_domain, iol_max_a=iol_max_a)
    if vol_v is not None:
        gpio["vol_v"] = vol_v
    return board(
        [part("U1", "ic", [gpio], role="mcu", mpn="MCU", page=page, supply_net=supply_net), *extra],
        power_domains=[
            {"name": "12V", "net": "V12", "nominal_v": 12},
            {"name": "V3V3", "net": "V3V3", "nominal_v": 3.3},
            {"name": "V1V8", "net": "V1V8", "nominal_v": 1.8},
        ],
    )


def _shifter(*extra):
    return board(
        [
            part("U2", "ic", [pin("A1", "A"), pin("B1", "B")], role="level_shifter", mpn="TXS", channels=[{"a_pin": "A1", "b_pin": "B1"}]),
            *extra,
        ],
        power_domains=[{"name": "V3V3", "net": "V3V3", "nominal_v": 3.3}],
    )


def _sdio(a0, b0, a1, b1):
    return board(
        [
            part("U1", "ic", [pin(1, "H0", pin_type="signal"), pin(2, "H1", pin_type="signal")], role="mcu", mpn="MCU"),
            part(
                "U2",
                "ic",
                [pin("A1", a0), pin("B1", b0), pin("A2", a1), pin("B2", b1)],
                role="level_shifter",
                mpn="TXS",
                channels=[{"a_pin": "A1", "b_pin": "B1"}, {"a_pin": "A2", "b_pin": "B2"}],
            ),
            part("U3", "ic", [pin(1, "D0", pin_type="signal"), pin(2, "D1", pin_type="signal")], role="module", mpn="WIFI"),
        ],
        signal_groups=[
            {
                "name": "SD0",
                "kind": "sdio",
                "endpoints": [
                    {"component": "U1", "pin": "1", "role": "d0", "side": "host"},
                    {"component": "U1", "pin": "2", "role": "d1", "side": "host"},
                    {"component": "U3", "pin": "1", "role": "d0", "side": "device"},
                    {"component": "U3", "pin": "2", "role": "d1", "side": "device"},
                ],
            }
        ],
    )


def _uart(tx, rx, device_rx, device_tx):
    return board(
        [
            part(
                "U1",
                "ic",
                [pin(1, tx, pin_type="signal", direction="output"), pin(2, rx, pin_type="signal", direction="input")],
                role="mcu",
                mpn="MCU",
            ),
            part(
                "U3",
                "ic",
                [pin(1, device_rx, pin_type="signal", direction="input"), pin(2, device_tx, pin_type="signal", direction="output")],
                role="module",
                mpn="MOD",
            ),
            part("U2", "ic", [], role="level_shifter", mpn="TXS", channels=[{"a_pin": "A1", "b_pin": "B1"}]),
        ],
        signal_groups=[
            {
                "name": "COM",
                "kind": "uart",
                "endpoints": [
                    {"component": "U1", "pin": "1", "role": "tx", "side": "host"},
                    {"component": "U1", "pin": "2", "role": "rx", "side": "host"},
                    {"component": "U3", "pin": "1", "role": "rx", "side": "device"},
                    {"component": "U3", "pin": "2", "role": "tx", "side": "device"},
                ],
            }
        ],
    )


def _regulator(rtop, rbot, vref, cap="10uF", cout_min=10, bias=1, tolerance=0, verified=True):
    constraints = []
    if vref is not None:
        constraints.append({"parameter": "vref", "refdes": "U1", "value": vref, "unit": "V", "verified": verified, "source": "datasheet p.12"})
    if cout_min is not None:
        constraints.append({"parameter": "cout_min", "refdes": "U1", "value": cout_min, "unit": "uF", "verified": True, "source": "datasheet p.20"})
    return board(
        [
            part(
                "U1",
                "ic",
                [pin("FB", "FB", pin_type="feedback"), pin("OUT", "VOUT", pin_type="vout")],
                role="regulator",
                mpn="LDO",
                parameters={"target_vout": 1.2, "vout_tolerance_pct": 1},
            ),
            part("Rtop", "resistor", [pin(1, "VOUT"), pin(2, "FB")], value=rtop),
            part("Rbot", "resistor", [pin(1, "FB"), pin(2, "GND")], value=rbot),
            part("C1", "capacitor", [pin(1, "VOUT"), pin(2, "GND")], value=cap, tolerance_pct=tolerance, parameters={} if bias is None else {"bias_factor": bias}),
        ],
        power_domains=[{"name": "VOUT", "net": "VOUT", "nominal_v": 1.2, "source": "U1"}],
        constraints=constraints,
    )


def _crystal(c1, c2, stray, cl, cl_max):
    return board(
        [
            part("Y1", "crystal", [pin(1, "X1"), pin(2, "X2")], role="crystal", mpn="XTAL", parameters={} if stray is None else {"c_stray_pf": stray}),
            part("C1", "capacitor", [pin(1, "X1"), pin(2, "GND")], value=c1),
            part("C2", "capacitor", [pin(1, "X2"), pin(2, "GND")], value=c2),
        ],
        constraints=[{"parameter": "cl", "refdes": "Y1", "value": cl, "min_value": cl, "max_value": cl_max, "unit": "pF", "verified": True, "source": "crystal datasheet"}],
    )


def _usb(fitted_a, fitted_b):
    return board(
        [
            part("U1", "ic", [pin(1, "DP", pin_type="dp"), pin(2, "DM", pin_type="dm")], role="usb_phy", mpn="PHY1", fitted=fitted_a),
            part("U2", "ic", [pin(1, "DP", pin_type="dp"), pin(2, "DM", pin_type="dm")], role="usb_phy", mpn="PHY2", fitted=fitted_b),
        ]
    )


def _healthy():
    return board(
        [
            part(
                "U1",
                "ic",
                [
                    pin(1, "GPIO", pin_type="gpio", direction="output", abs_max_v=3.6, voltage_domain="V3V3", iol_max_a=0.008),
                    pin(2, "H0", pin_type="signal", direction="bidirectional", abs_max_v=3.6, voltage_domain="V3V3", iol_max_a=0.008),
                    pin(3, "H1", pin_type="signal", direction="bidirectional", abs_max_v=3.6, voltage_domain="V3V3", iol_max_a=0.008),
                    pin(4, "UTX", pin_type="signal", direction="output", output_kind="push_pull", abs_max_v=3.6, voltage_domain="V3V3"),
                    pin(5, "URX", pin_type="signal", direction="input", abs_max_v=3.6, voltage_domain="V3V3"),
                ],
                role="mcu",
                mpn="STM32H723",
                supply_net="V3V3",
                ioff=True,
            ),
            part("Rpu", "resistor", [pin(1, "GPIO"), pin(2, "V3V3")], value="10k"),
            part(
                "U2",
                "ic",
                [
                    pin("A1", "H0"),
                    pin("B1", "D0"),
                    pin("A2", "H1"),
                    pin("B2", "D1"),
                    pin("VCCA", "V1V8", pin_type="vcca", name="VCCA"),
                    pin("VCCB", "V3V3", pin_type="vccb", name="VCCB"),
                    pin("OE", "V3V3", pin_type="oe", name="OE", abs_max_v=5.5, voltage_domain="V3V3"),
                ],
                role="level_shifter",
                mpn="TXS0102",
                channels=[{"a_pin": "A1", "b_pin": "B1"}, {"a_pin": "A2", "b_pin": "B2"}],
            ),
            part(
                "U3",
                "ic",
                [
                    pin(1, "D0", pin_type="signal", direction="bidirectional", abs_max_v=3.6, voltage_domain="V1V8", iol_max_a=0.008),
                    pin(2, "D1", pin_type="signal", direction="bidirectional", abs_max_v=3.6, voltage_domain="V1V8", iol_max_a=0.008),
                    pin(3, "UTX", pin_type="signal", direction="input"),
                    pin(4, "URX", pin_type="signal", direction="output", output_kind="push_pull"),
                ],
                role="module",
                mpn="FN6222",
            ),
            part(
                "U4",
                "ic",
                [pin("FB", "FB", pin_type="feedback"), pin("OUT", "VOUT", pin_type="vout"), pin("EN", "EN", pin_type="en", name="EN", abs_max_v=6)],
                role="regulator",
                mpn="TLV75533",
                parameters={"target_vout": 1.2, "vout_tolerance_pct": 1},
            ),
            part("Rpd", "resistor", [pin(1, "EN"), pin(2, "GND")], value="100k"),
            part("Rtop", "resistor", [pin(1, "VOUT"), pin(2, "FB")], value="10k"),
            part("Rbot", "resistor", [pin(1, "FB"), pin(2, "GND")], value="10k"),
            part("Cout", "capacitor", [pin(1, "VOUT"), pin(2, "GND")], value="22uF", tolerance_pct=10, parameters={"bias_factor": 0.8}),
            part("Y1", "crystal", [pin(1, "X1"), pin(2, "X2")], role="crystal", mpn="ABS07", parameters={"c_stray_pf": 2}),
            part("Cx1", "capacitor", [pin(1, "X1"), pin(2, "GND")], value="22pF"),
            part("Cx2", "capacitor", [pin(1, "X2"), pin(2, "GND")], value="22pF"),
            part("U5", "ic", [pin(1, "USB_DP", pin_type="dp"), pin(2, "USB_DM", pin_type="dm")], role="usb_phy", mpn="USBPHY"),
            part("J1", "connector", [], fitted=True),
            part("J2", "connector", [], fitted=False),
        ],
        power_domains=[
            {"name": "V3V3", "net": "V3V3", "nominal_v": 3.3},
            {"name": "V1V8", "net": "V1V8", "nominal_v": 1.8},
            {"name": "12V", "net": "V12", "nominal_v": 12},
            {"name": "VOUT", "net": "VOUT", "nominal_v": 1.2, "source": "U4"},
        ],
        signal_groups=[
            {
                "name": "SD0",
                "kind": "sdio",
                "endpoints": [
                    {"component": "U1", "pin": "2", "role": "d0", "side": "host"},
                    {"component": "U1", "pin": "3", "role": "d1", "side": "host"},
                    {"component": "U3", "pin": "1", "role": "d0", "side": "device"},
                    {"component": "U3", "pin": "2", "role": "d1", "side": "device"},
                ],
            },
            {
                "name": "COM",
                "kind": "uart",
                "endpoints": [
                    {"component": "U1", "pin": "4", "role": "tx", "side": "host"},
                    {"component": "U1", "pin": "5", "role": "rx", "side": "host"},
                    {"component": "U3", "pin": "3", "role": "rx", "side": "device"},
                    {"component": "U3", "pin": "4", "role": "tx", "side": "device"},
                ],
            },
        ],
        constraints=[
            {"parameter": "vcca", "refdes": "U2", "min_value": 1.65, "max_value": 5.5, "unit": "V", "verified": True, "source": "level shifter datasheet"},
            {"parameter": "vccb", "refdes": "U2", "min_value": 1.65, "max_value": 5.5, "unit": "V", "verified": True, "source": "level shifter datasheet"},
            {"parameter": "vref", "refdes": "U4", "value": 0.6, "unit": "V", "verified": True, "source": "ldo p.8"},
            {"parameter": "cout_min", "refdes": "U4", "value": 10, "unit": "uF", "verified": True, "source": "ldo p.20"},
            {"parameter": "cl", "refdes": "Y1", "value": 12, "min_value": 10, "max_value": 14, "unit": "pF", "verified": True, "source": "crystal datasheet"},
        ],
        assembly=[{"id": "debug", "refdes": ["J1", "J2"], "exclusive": True}],
    )
