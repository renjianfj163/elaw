"""第一阶段连接检查。数字和型号只用于夹具。"""

from app.schematic.engine.model import Circuit
from app.schematic.engine.paths import dc_span
from app.schematic.engine.runner import review_circuit
from tests.test_schematic_engine import board, only, part, pin


def _power_pin(net, **pin_kwargs):
    body = {"number": "3", "net": net, "pin_type": "power", "name": "VDD"}
    body.update(pin_kwargs)
    return body


def _rail(**kwargs):
    data = {"name": "3V3", "net": "V3V3", "nominal_v": 3.3, "source": "J1"}
    data.update(kwargs)
    return data


def test_series_capacitor_is_not_a_dc_supply():
    circuit = board(
        [
            part("U1", "ic", [_power_pin("VDD")], mpn="MCU"),
            part("C1", "capacitor", [pin(1, "VDD"), pin(2, "V3V3")], value="100nF"),
        ],
        power_domains=[_rail()],
    )
    span = dc_span(circuit, "VDD")
    assert "V3V3" not in span.nets
    assert span.ac == [("C1", "V3V3")]
    item = only(circuit, "CONN-001")[1][0]
    assert item.decision == "confirmed_error"
    assert item.status == "confirmed"
    assert "C1" in item.traced_path or any("C1" in step for step in item.actual_roles)
    assert item.source_fact_ids


def test_unknown_part_is_not_treated_as_a_short_or_an_open_fault():
    circuit = board(
        [
            part("U1", "ic", [_power_pin("VDD")], mpn="MCU"),
            part("X1", "unknown", [pin(1, "VDD"), pin(2, "V3V3")]),
        ],
        power_domains=[_rail()],
    )
    assert "V3V3" not in dc_span(circuit, "VDD").nets
    item = only(circuit, "CONN-001")[1][0]
    assert item.decision == "needs_review"
    assert item.status == "needs_review"


def test_ferrite_is_a_dc_path_and_level_shifter_is_not():
    via_bead = board(
        [
            part("U1", "ic", [_power_pin("VDD")], mpn="MCU"),
            part("FB1", "ferrite", [pin(1, "VDD"), pin(2, "V3V3")]),
        ],
        power_domains=[_rail()],
    )
    assert "V3V3" in dc_span(via_bead, "VDD").nets
    assert only(via_bead, "CONN-001")[1] == []
    assert only(via_bead, "CONN-001")[0].runs[0].decision == "confirmed_ok"
    shifted = board(
        [
            part("U1", "ic", [_power_pin("VDD")], mpn="MCU"),
            part(
                "U2",
                "ic",
                [pin("A1", "VDD"), pin("B1", "V3V3")],
                role="level_shifter",
                channels=[{"a_pin": "A1", "b_pin": "B1"}],
            ),
        ],
        power_domains=[_rail()],
    )
    assert "V3V3" not in dc_span(shifted, "VDD").nets
    assert only(shifted, "CONN-001")[1][0].decision == "confirmed_error"


def test_common_mode_channels_do_not_cross():
    circuit = board(
        [
            part(
                "L1",
                "inductor",
                [pin("1", "A"), pin("2", "B"), pin("3", "C"), pin("4", "D")],
                channels=[{"a_pin": "1", "b_pin": "2"}, {"a_pin": "3", "b_pin": "4"}],
            )
        ]
    )
    assert dc_span(circuit, "A").nets == ["A", "B"] or set(dc_span(circuit, "A").nets) == {"A", "B"}
    assert "C" not in dc_span(circuit, "A").nets


def test_open_power_pin_and_incomplete_netlist():
    open_pin = board([part("U1", "ic", [_power_pin(None)], mpn="MCU")], power_domains=[_rail()])
    item = only(open_pin, "CONN-001")[1][0]
    assert item.decision == "confirmed_error"
    assert item.affected_pins[0].startswith("U1.3")
    assert only(open_pin, "CONN-002")[0].runs[0].decision == ""
    incomplete = board(
        [part("U1", "ic", [_power_pin(None)], mpn="MCU")],
        power_domains=[_rail()],
        connectivity_complete=False,
    )
    held = only(incomplete, "CONN-001")[1][0]
    assert held.decision == "insufficient_netlist"
    assert held.status == "not_assessable"


def test_pdf_connection_does_not_become_a_confirmed_error():
    circuit = board([part("U1", "ic", [_power_pin(None)], mpn="MCU")], power_domains=[_rail()])
    circuit.source = "pdf"
    item = only(circuit, "CONN-001")[1][0]
    assert item.status == "probable"
    assert item.decision == "needs_review"


def test_supply_source_is_needs_review_until_a_verified_source_misses():
    unnamed = board(
        [part("U1", "ic", [_power_pin("V3V3")], mpn="MCU")],
        power_domains=[{"name": "3V3", "net": "V3V3", "nominal_v": 3.3}],
    )
    assert only(unnamed, "CONN-002")[1][0].decision == "needs_review"
    named = board(
        [
            part("U1", "ic", [_power_pin("V3V3")], mpn="MCU"),
            part("U4", "ic", [pin("OUT", "V3V3", pin_type="vout")], role="regulator", mpn="LDO"),
        ],
        power_domains=[{"name": "3V3", "net": "V3V3", "nominal_v": 3.3}],
    )
    assert only(named, "CONN-002")[1] == []
    assert only(named, "CONN-002")[0].runs[0].decision == "confirmed_ok"
    missed = board(
        [part("U1", "ic", [_power_pin("V3V3")], mpn="MCU")],
        power_domains=[{"name": "3V3", "net": "V3V3", "nominal_v": 3.3, "source": "J1"}],
        constraints=[
            {
                "parameter": "supply_source",
                "refdes": "U1",
                "pin": "3",
                "value_text": "U9",
                "verified": True,
                "constraint_id": "src-u9",
                "constraint_type": "hard_limit",
            }
        ],
    )
    item = only(missed, "CONN-002")[1][0]
    assert item.decision == "confirmed_error"
    assert "src-u9" in item.source_fact_ids


def test_regulator_short_and_different_rails():
    shorted = board(
        [
            part(
                "U4",
                "ic",
                [pin("IN", "VIN", pin_type="vin"), pin("OUT", "VIN", pin_type="vout")],
                role="regulator",
                mpn="LDO",
            )
        ]
    )
    assert only(shorted, "CONN-003")[1][0].decision == "confirmed_error"
    coupled = board(
        [
            part(
                "U4",
                "ic",
                [pin("IN", "VIN", pin_type="vin"), pin("OUT", "VOUT", pin_type="vout")],
                role="regulator",
                mpn="LDO",
            ),
            part("C1", "capacitor", [pin(1, "VIN"), pin(2, "VOUT")], value="100nF"),
        ]
    )
    assert only(coupled, "CONN-003")[1] == []
    rails = board(
        [part("R0", "jumper", [pin(1, "V3V3"), pin(2, "V1V8")], value="0")],
        power_domains=[
            {"name": "3V3", "net": "V3V3", "nominal_v": 3.3},
            {"name": "1V8", "net": "V1V8", "nominal_v": 1.8},
        ],
    )
    item = only(rails, "CONN-003")[1][0]
    assert item.decision == "confirmed_error"
    assert "V3V3" in item.affected_nets and "V1V8" in item.affected_nets
    resistive = board(
        [part("R1", "resistor", [pin(1, "V3V3"), pin(2, "V1V8")], value="10k")],
        power_domains=[
            {"name": "3V3", "net": "V3V3", "nominal_v": 3.3},
            {"name": "1V8", "net": "V1V8", "nominal_v": 1.8},
        ],
    )
    assert only(resistive, "CONN-003")[1][0].decision == "needs_review"
    same = board(
        [part("R0", "jumper", [pin(1, "VA"), pin(2, "VB")], value="0")],
        power_domains=[
            {"name": "A", "net": "VA", "nominal_v": 3.3},
            {"name": "B", "net": "VB", "nominal_v": 3.3},
        ],
    )
    assert only(same, "CONN-003")[1] == []
    unknown_v = board(
        [part("R0", "jumper", [pin(1, "VA"), pin(2, "VB")], value="0")],
        power_domains=[{"name": "A", "net": "VA", "nominal_v": 3.3}, {"name": "B", "net": "VB"}],
    )
    assert only(unknown_v, "CONN-003")[1][0].decision == "needs_review"


def test_ground_names_alone_do_not_fail_and_policy_does():
    names = board(
        [
            part("U1", "ic", [pin(1, "AGND", pin_type="ground"), pin(2, "DGND", pin_type="ground")], mpn="MCU"),
        ]
    )
    assert only(names, "CONN-004")[1] == []
    separate = board(
        [
            part("U1", "ic", [pin(1, "AGND"), pin(2, "DGND")], mpn="MCU"),
            part("R0", "jumper", [pin(1, "AGND"), pin(2, "DGND")], value="0"),
        ],
        constraints=[
            {
                "parameter": "ground_policy",
                "refdes": "U1",
                "value_text": "separate:AGND,DGND",
                "verified": True,
                "constraint_id": "gnd-sep",
                "constraint_type": "hard_limit",
            }
        ],
    )
    item = only(separate, "CONN-004")[1][0]
    assert item.decision == "confirmed_error"
    assert "gnd-sep" in item.source_fact_ids
    joined = board(
        [part("U1", "ic", [pin(1, "AGND"), pin(2, "DGND")], mpn="MCU")],
        constraints=[
            {
                "parameter": "ground_policy",
                "refdes": "U1",
                "value_text": "join:AGND,DGND",
                "verified": True,
                "constraint_id": "gnd-join",
                "constraint_type": "hard_limit",
            }
        ],
    )
    assert only(joined, "CONN-004")[1][0].decision == "confirmed_error"
    wrong_rail = board(
        [part("U1", "ic", [_power_pin("V1V8")], mpn="MCU")],
        power_domains=[_rail(), {"name": "1V8", "net": "V1V8", "nominal_v": 1.8, "source": "U2"}],
        constraints=[
            {
                "parameter": "supply_net",
                "refdes": "U1",
                "pin": "3",
                "value_text": "V3V3",
                "verified": True,
                "constraint_id": "rail-3v3",
                "constraint_type": "hard_limit",
            }
        ],
    )
    missed = only(wrong_rail, "CONN-004")[1][0]
    assert missed.decision == "confirmed_error"
    assert missed.expected_roles == ["V3V3"]
    assert "rail-3v3" in missed.evidence_sources or "rail-3v3" in missed.source_fact_ids


def test_required_and_open_pins_follow_verification():
    missing = board(
        [part("U1", "ic", [pin(1, "GPIO", direction="input", pin_type="signal")], mpn="MCU")],
        constraints=[
            {
                "parameter": "pin_tie",
                "refdes": "U1",
                "pin": "9",
                "value_text": "required",
                "verified": True,
                "constraint_id": "pin-9",
                "constraint_type": "hard_limit",
            }
        ],
    )
    item = only(missing, "CONN-005")[1][0]
    assert item.decision == "confirmed_error"
    assert item.actual_roles == ["missing_pin"]
    tied = board(
        [part("U1", "ic", [pin(8, "NC_NET")], mpn="MCU")],
        constraints=[
            {
                "parameter": "pin_tie",
                "refdes": "U1",
                "pin": "8",
                "value_text": "nc",
                "verified": True,
                "constraint_id": "pin-8",
                "constraint_type": "hard_limit",
            }
        ],
    )
    assert only(tied, "CONN-005")[1][0].decision == "confirmed_error"
    unverified = board(
        [part("U1", "ic", [pin(8, "NC_NET")], mpn="MCU")],
        constraints=[
            {
                "parameter": "pin_tie",
                "refdes": "U1",
                "pin": "8",
                "value_text": "nc",
                "verified": False,
                "constraint_id": "pin-8",
            }
        ],
    )
    held = only(unverified, "CONN-005")[1][0]
    assert held.decision == "missing_pin_role"
    assert held.status == "not_assessable"
    conflict = board(
        [part("U1", "ic", [pin(8, "NC_NET")], mpn="MCU")],
        constraints=[
            {
                "parameter": "pin_tie",
                "refdes": "U1",
                "pin": "8",
                "value_text": "nc",
                "verified": True,
                "constraint_id": "a",
                "constraint_type": "hard_limit",
            },
            {
                "parameter": "pin_tie",
                "refdes": "U1",
                "pin": "8",
                "value_text": "required",
                "verified": True,
                "constraint_id": "b",
                "constraint_type": "hard_limit",
            },
        ],
    )
    assert only(conflict, "CONN-005")[1][0].decision == "conflicting_constraints"


def test_undriven_input_pullup_and_capacitor():
    floating = board([part("U1", "ic", [pin(1, "RX", direction="input", pin_type="signal")], mpn="MCU")])
    assert only(floating, "CONN-006")[1][0].decision == "confirmed_error"
    pulled = board(
        [
            part("U1", "ic", [pin(1, "RX", direction="input", pin_type="signal")], mpn="MCU"),
            part("R1", "resistor", [pin(1, "RX"), pin(2, "V3V3")], value="10k"),
        ],
        power_domains=[_rail()],
    )
    assert only(pulled, "CONN-006")[1] == []
    driven = board(
        [
            part("U1", "ic", [pin(1, "RX", direction="input", pin_type="signal")], mpn="MCU"),
            part("R1", "resistor", [pin(1, "RX"), pin(2, "MID")], value="33"),
            part("U3", "ic", [pin(1, "MID", direction="output", output_kind="push_pull")], mpn="MOD"),
        ]
    )
    assert only(driven, "CONN-006")[1] == []
    coupled = board(
        [
            part("U1", "ic", [pin(1, "RX", direction="input", pin_type="signal")], mpn="MCU"),
            part("C1", "capacitor", [pin(1, "RX"), pin(2, "TX")], value="100nF"),
            part("U3", "ic", [pin(1, "TX", direction="output", output_kind="push_pull")], mpn="MOD"),
        ]
    )
    item = only(coupled, "CONN-006")[1][0]
    assert item.decision == "needs_review"
    assert any("C1" in step for step in item.actual_roles)
    incomplete = board(
        [part("U1", "ic", [pin(1, "RX", direction="input", pin_type="signal")], mpn="MCU")],
        connectivity_complete=False,
    )
    assert only(incomplete, "CONN-006")[1][0].decision == "insufficient_netlist"


def test_existing_push_pull_rule_still_confirms_and_open_drain_does_not():
    shared = board(
        [
            part("U1", "ic", [pin(1, "BUS", direction="output", output_kind="push_pull")], mpn="A"),
            part("U2", "ic", [pin(1, "BUS", direction="output", output_kind="push_pull")], mpn="B"),
        ]
    )
    assert only(shared, "IO-004")[1][0].status == "confirmed"
    assert only(shared, "IO-004")[1][0].decision == ""
    drains = board(
        [
            part("U1", "ic", [pin(1, "BUS", direction="output", output_kind="open_drain")], mpn="A"),
            part("U2", "ic", [pin(1, "BUS", direction="output", output_kind="open_drain")], mpn="B"),
        ]
    )
    assert only(drains, "IO-004")[1] == []


def test_healthy_board_is_unchanged():
    from tests.test_schematic_engine import _healthy

    report = review_circuit(_healthy())
    bad = [item for item in report.findings if item.status in {"confirmed", "probable"}]
    assert bad == []
    assert report.verdict == "clear"


def test_empty_circuit_still_has_no_applicable_rules():
    report = review_circuit(Circuit())
    assert report.verdict == "no_applicable_rules"
    assert "CONN-001" not in report.unimplemented
