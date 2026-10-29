"""第二阶段：总线完整性、复位默认状态、条件通路和工作模式。"""

from app.schematic.engine.checks_mode import signal_reach
from app.schematic.engine.dc import CircuitView
from tests.test_schematic_engine import board, only, part, pin


def role(refdes, number, text, interface, side, **extra):
    body = {
        "parameter": "pin_role",
        "refdes": refdes,
        "pin": str(number),
        "value_text": text,
        "interface": interface,
        "side": side,
        "verified": True,
        "constraint_type": "hard_limit",
        "constraint_id": f"{refdes}-{number}-{text}-{side}",
    }
    body.update(extra)
    return body


def profile(refdes, name, interface):
    return {
        "parameter": "interface_profile",
        "refdes": refdes,
        "value_text": name,
        "interface": interface,
        "verified": True,
        "constraint_type": "hard_limit",
        "constraint_id": f"{refdes}-{name}",
    }


def _pair(interface, roles, nets):
    constraints = [profile("U1", interface if interface != "sdio" else "sdio4", "sdio" if interface == "sdio" else interface)]
    if interface == "sdio":
        constraints[0]["value_text"] = "sdio4"
    pins_h = []
    pins_d = []
    for index, name in enumerate(roles, start=1):
        net = nets.get(name, name.upper())
        pins_h.append(pin(index, net, pin_type="signal", direction="bidirectional"))
        pins_d.append(pin(index, net, pin_type="signal", direction="bidirectional"))
        constraints.append(role("U1", index, name, interface if interface != "sdio" else "sdio", "host"))
        constraints.append(role("U3", index, name, interface if interface != "sdio" else "sdio", "device"))
    return pins_h, pins_d, constraints


def test_sdio_mapping_and_missing_lane():
    roles = ["clk", "cmd", "d0", "d1", "d2", "d3"]
    nets = {name: name.upper() for name in roles}
    host, device, constraints = _pair("sdio", roles, nets)
    ok = board(
        [part("U1", "ic", host, mpn="MCU"), part("U3", "ic", device, mpn="WIFI")],
        constraints=constraints,
    )
    assert only(ok, "CONN-007")[1] == []
    assert only(ok, "CONN-007")[0].runs[0].decision == "confirmed_ok"
    host[4] = pin(5, None, pin_type="signal")
    broken = board(
        [part("U1", "ic", host, mpn="MCU"), part("U3", "ic", device, mpn="WIFI")],
        constraints=constraints,
    )
    assert only(broken, "CONN-007")[1][0].decision == "confirmed_error"
    incomplete = board(
        [part("U1", "ic", host, mpn="MCU"), part("U3", "ic", device, mpn="WIFI")],
        constraints=constraints,
        connectivity_complete=False,
    )
    assert only(incomplete, "CONN-007")[1][0].decision == "insufficient_netlist"
    from_pdf = board(
        [part("U1", "ic", host, mpn="MCU"), part("U3", "ic", device, mpn="WIFI")],
        constraints=constraints,
        source="pdf",
    )
    pdf_item = only(from_pdf, "CONN-007")[1][0]
    assert pdf_item.status == "probable"
    assert pdf_item.decision == "needs_review"


def test_sdio_without_device_roles_is_not_a_wiring_error():
    constraints = [profile("U1", "sdio4", "sdio"), role("U1", 1, "clk", "sdio", "host")]
    circuit = board([part("U1", "ic", [pin(1, "CLK")], mpn="MCU")], constraints=constraints)
    items = only(circuit, "CONN-007")[1]
    assert items[0].decision == "missing_pin_role"
    assert all(item.decision != "confirmed_error" for item in items)


def test_spi_chip_select_target_and_i2c_pullup():
    constraints = [
        profile("U1", "spi", "spi"),
        role("U1", 1, "cs", "spi", "host"),
        role("U3", 1, "cs", "spi", "device"),
        role("U9", 1, "cs", "spi", "device"),
        {
            "parameter": "cs_target",
            "refdes": "U1",
            "value_text": "U9",
            "verified": True,
            "constraint_type": "hard_limit",
            "constraint_id": "cs-u9",
        },
    ]
    wrong = board(
        [
            part("U1", "ic", [pin(1, "CS0")], mpn="MCU"),
            part("U3", "ic", [pin(1, "CS0")], mpn="FLASH"),
            part("U9", "ic", [pin(1, "CS1")], mpn="EEPROM"),
        ],
        constraints=constraints,
    )
    assert any(item.decision == "confirmed_error" for item in only(wrong, "CONN-007")[1])
    i2c = [
        profile("U1", "i2c", "i2c"),
        role("U1", 1, "sda", "i2c", "host"),
        role("U1", 2, "scl", "i2c", "host"),
        role("U3", 1, "sda", "i2c", "device"),
        role("U3", 2, "scl", "i2c", "device"),
        {
            "parameter": "require_pullup",
            "refdes": "U1",
            "value_text": "required",
            "verified": True,
            "constraint_type": "hard_limit",
            "constraint_id": "i2c-pull",
        },
        {
            "parameter": "pullup_domain",
            "refdes": "U1",
            "value_text": "V3V3",
            "verified": True,
            "constraint_type": "hard_limit",
            "constraint_id": "i2c-rail",
        },
    ]
    open_bus = board(
        [
            part("U1", "ic", [pin(1, "SDA"), pin(2, "SCL")], mpn="MCU"),
            part("U3", "ic", [pin(1, "SDA"), pin(2, "SCL")], mpn="DEV"),
        ],
        constraints=i2c,
        power_domains=[{"name": "3V3", "net": "V3V3", "nominal_v": 3.3}, {"name": "1V8", "net": "V1V8", "nominal_v": 1.8}],
    )
    assert any(item.decision == "confirmed_error" for item in only(open_bus, "CONN-007")[1])
    pulled = board(
        [
            part("U1", "ic", [pin(1, "SDA"), pin(2, "SCL")], mpn="MCU"),
            part("U3", "ic", [pin(1, "SDA"), pin(2, "SCL")], mpn="DEV"),
            part("R1", "resistor", [pin(1, "SDA"), pin(2, "V1V8")], value="4.7k"),
            part("R2", "resistor", [pin(1, "SCL"), pin(2, "V1V8")], value="4.7k"),
        ],
        constraints=i2c,
        power_domains=[{"name": "3V3", "net": "V3V3", "nominal_v": 3.3}, {"name": "1V8", "net": "V1V8", "nominal_v": 1.8}],
    )
    assert any(item.decision == "confirmed_error" and "上拉电源" in item.title for item in only(pulled, "CONN-007")[1])


def test_uart_flow_control_and_pcie_polarity():
    roles = ["tx", "rx", "cts", "rts"]
    host = []
    device = []
    constraints = [profile("U1", "uart_hwfc", "uart")]
    cross = {"tx": "RXD", "rx": "TXD", "cts": "RTS", "rts": "CTS"}
    for index, name in enumerate(roles, start=1):
        host.append(pin(index, {"tx": "TXD", "rx": "RXD", "cts": "CTS", "rts": "RTS"}[name]))
        device.append(pin(index, cross[name]))
        constraints.append(role("U1", index, name, "uart", "host"))
        constraints.append(role("U3", index, name, "uart", "device"))
    ok = board(
        [part("U1", "ic", host, mpn="MCU"), part("U3", "ic", device, mpn="MOD")],
        constraints=constraints,
    )
    assert only(ok, "CONN-007")[1] == []
    device[2] = pin(3, "CTS")
    swapped = board(
        [part("U1", "ic", host, mpn="MCU"), part("U3", "ic", device, mpn="MOD")],
        constraints=constraints,
    )
    assert any(item.decision == "confirmed_error" for item in only(swapped, "CONN-007")[1])
    pcie_roles = ["tx_p", "tx_n", "rx_p", "rx_n", "refclk_p", "refclk_n", "perst"]
    host_nets = {name: name.upper() for name in pcie_roles}
    device_nets = {
        "tx_p": "RX_P",
        "tx_n": "RX_N",
        "rx_p": "TX_P",
        "rx_n": "TX_N",
        "refclk_p": "REFCLK_P",
        "refclk_n": "REFCLK_N",
        "perst": "PERST",
    }
    pins_h, pins_d, body = [], [], [profile("U1", "pcie", "pcie")]
    for index, name in enumerate(pcie_roles, start=1):
        pins_h.append(pin(index, host_nets[name]))
        pins_d.append(pin(index, device_nets[name]))
        body.append(role("U1", index, name, "pcie", "host"))
        body.append(role("U3", index, name, "pcie", "device"))
    linked = board([part("U1", "ic", pins_h, mpn="RC"), part("U3", "ic", pins_d, mpn="EP")], constraints=body)
    assert only(linked, "CONN-007")[1] == []
    pins_d[0] = pin(1, "TX_P")
    pins_d[2] = pin(3, "RX_P")
    straight = board([part("U1", "ic", pins_h, mpn="RC"), part("U3", "ic", pins_d, mpn="EP")], constraints=body)
    assert any(item.decision == "confirmed_error" for item in only(straight, "CONN-007")[1])
    pins_d[0] = pin(1, "RX_N")
    pins_d[1] = pin(2, "RX_P")
    pins_d[2] = pin(3, "TX_P")
    polarity = board([part("U1", "ic", pins_h, mpn="RC"), part("U3", "ic", pins_d, mpn="EP")], constraints=body)
    assert any(item.decision == "needs_review" for item in only(polarity, "CONN-007")[1])


def test_reset_boot_and_oe_defaults():
    held = board(
        [
            part("U1", "ic", [pin(1, "RST")], mpn="MCU"),
            part("R0", "jumper", [pin(1, "RST"), pin(2, "GND")], value="0"),
        ],
        constraints=[
            {
                "parameter": "pin_state",
                "refdes": "U1",
                "pin": "1",
                "value_text": "idle_high",
                "verified": True,
                "constraint_type": "hard_limit",
                "constraint_id": "rst-idle",
            }
        ],
    )
    assert only(held, "CONN-008")[1][0].decision == "confirmed_error"
    driven = board(
        [
            part("U1", "ic", [pin(1, "BOOT")], mpn="MCU"),
            part("U2", "ic", [pin(1, "BOOT", direction="output", output_kind="push_pull")], mpn="IO"),
        ],
        constraints=[
            {
                "parameter": "pin_state",
                "refdes": "U1",
                "pin": "1",
                "value_text": "not_strongly_driven",
                "verified": True,
                "constraint_type": "hard_limit",
                "constraint_id": "boot",
            }
        ],
    )
    assert only(driven, "CONN-008")[1][0].decision == "confirmed_error"
    unknown = board(
        [
            part("U1", "ic", [pin(1, "BOOT")], mpn="MCU"),
            part("U2", "ic", [pin(1, "BOOT", direction="output", output_kind="unknown")], mpn="IO"),
        ],
        constraints=driven.constraints,
    )
    assert only(unknown, "CONN-008")[1][0].decision == "needs_review"
    released = board(
        [
            part("U2", "ic", [pin("OE", "OE", pin_type="oe")], role="level_shifter", mpn="TXS"),
            part("Rpd", "resistor", [pin(1, "OE"), pin(2, "GND")], value="100k"),
        ],
        constraints=[
            {
                "parameter": "pin_state",
                "refdes": "U2",
                "pin": "OE",
                "value_text": "idle_low",
                "verified": True,
                "constraint_type": "hard_limit",
                "constraint_id": "oe-low",
            }
        ],
    )
    assert only(released, "CONN-008")[1] == []


def test_mux_needs_a_route_and_a_mode():
    bare = board([part("U2", "ic", [pin("A1", "A"), pin("B1", "B")], role="mux", mpn="MUX")])
    item = only(bare, "CONN-009")[1][0]
    assert item.decision == "needs_review"
    assert "拓扑无法确定" in item.title
    view = CircuitView(bare)
    assert "B" not in signal_reach(view, "A")
    pending = board(
        [part("U2", "ic", [pin("A1", "A"), pin("B1", "B")], role="mux", mpn="MUX")],
        constraints=[
            {
                "parameter": "switch_route",
                "refdes": "U2",
                "value_text": "A1:B1",
                "operating_mode": "S0",
                "verified": True,
                "constraint_type": "hard_limit",
                "constraint_id": "route",
            }
        ],
    )
    assert only(pending, "CONN-009")[1][0].decision == "needs_review"
    selected = board(
        [part("U2", "ic", [pin("A1", "A"), pin("B1", "B")], role="mux", mpn="MUX", mode="S0")],
        constraints=pending.constraints,
    )
    assert only(selected, "CONN-009")[1] == []
    assert only(selected, "CONN-009")[0].runs[0].decision == "confirmed_ok"
    assert "B" in signal_reach(CircuitView(selected), "A")
    clash = board(
        [part("U2", "ic", [pin("A1", "A"), pin("B1", "B"), pin("B2", "C")], role="analog_switch", mpn="SW", mode="S0")],
        constraints=[
            {
                "parameter": "switch_route",
                "refdes": "U2",
                "value_text": "A1:B1",
                "operating_mode": "S0",
                "verified": True,
                "constraint_type": "hard_limit",
                "constraint_id": "r1",
            },
            {
                "parameter": "switch_route",
                "refdes": "U2",
                "value_text": "A1:B2",
                "operating_mode": "S0",
                "verified": True,
                "constraint_type": "hard_limit",
                "constraint_id": "r2",
            },
        ],
    )
    assert only(clash, "CONN-009")[1][0].decision == "conflicting_constraints"


def test_operating_mode_blocks_a_direct_error():
    pins = [pin(1, "PAD", direction="input", pin_type="signal")]
    unknown = board(
        [part("U1", "ic", pins, mpn="MCU")],
        constraints=[
            role("U1", 1, "scl", "i2c", "host", operating_mode="i2c"),
            role("U1", 1, "gpio", "gpio", "host", operating_mode="gpio", constraint_id="gpio-role"),
        ],
    )
    item = only(unknown, "CONN-006")[1][0]
    assert item.decision == "needs_review"
    assert item.status != "confirmed"
    clash = board(
        [part("U1", "ic", pins, mpn="MCU", mode="i2c")],
        constraints=[
            role("U1", 1, "scl", "i2c", "host", operating_mode="i2c"),
            role("U1", 1, "sda", "i2c", "host", operating_mode="i2c", constraint_id="sda-role"),
        ],
    )
    assert only(clash, "CONN-006")[1][0].decision == "conflicting_constraints"
