"""总线完整性、复位启动状态，以及开关器件的条件通路。

已有的 SDIO/SPI/UART 同名或交叉映射仍由 IF 规则执行。
这里只补充必需信号、流控、PCIe、上拉电压域、复位默认状态和工作模式。
"""

from __future__ import annotations

from app.schematic.engine.calculators import compare
from app.schematic.engine.checks_connectivity import _decide, _ok, _undecided
from app.schematic.engine.checks_mode import (
    CONDITIONAL_ROLES,
    classify_pin_roles,
    classify_routes,
    conditional_bridge,
    signal_reach,
)
from app.schematic.engine.checks_mode import _facts, _route_pins
from app.schematic.engine.dc import CircuitView, is_ground, is_power, rail_voltage
from app.schematic.engine.model import Component
from app.schematic.engine.report import NOT_APPLICABLE, CheckResult, Rule, finish

_SAME = {
    "sdio1": ["clk", "cmd", "d0"],
    "sdio4": ["clk", "cmd", "d0", "d1", "d2", "d3"],
    "spi": ["mosi", "miso", "sclk", "cs"],
    "i2c": ["sda", "scl"],
}
_CROSS = {
    "uart": {"tx": "rx", "rx": "tx"},
    "uart_hwfc": {"tx": "rx", "rx": "tx", "cts": "rts", "rts": "cts"},
    "pcie": {
        "tx_p": "rx_p",
        "tx_n": "rx_n",
        "rx_p": "tx_p",
        "rx_n": "tx_n",
        "refclk_p": "refclk_p",
        "refclk_n": "refclk_n",
        "perst": "perst",
    },
}
_OWNED = {
    "sdio": {"d0", "d1", "d2", "d3"},
    "spi": {"mosi", "miso", "sclk", "cs"},
    "uart": {"tx", "rx"},
}
_DIFF = {"pcie": (("tx_p", "tx_n"), ("rx_p", "rx_n"))}


def check_conn_007(view: CircuitView, rule: Rule) -> CheckResult:
    profiles = _profiles(view)
    if not profiles:
        return NOT_APPLICABLE
    findings = []
    for comp, item in profiles:
        name = (item.value_text or "").strip().lower()
        if name not in _SAME and name not in _CROSS:
            findings.append(
                _undecided(view, rule, comp, None, "needs_review", "interface_profile", f"{comp.refdes} 的接口轮廓 {name or '空'} 无法执行。")
            )
            continue
        state, roles = _interface_roles(view, item.interface or _kind_of(name))
        if state == "mode_unknown":
            findings.append(
                _undecided(view, rule, comp, None, "needs_review", "operating_mode", f"{name} 的引脚角色取决于工作模式，当前模式未知。")
            )
            continue
        if state == "conflict":
            findings.append(
                _undecided(view, rule, comp, None, "conflicting_constraints", "pin_role", f"{name} 在同一工作模式下有冲突的引脚角色。")
            )
            continue
        missing = _missing_roles(name, roles)
        if missing:
            findings.append(
                _undecided(
                    view,
                    rule,
                    comp,
                    None,
                    "missing_pin_role",
                    "pin_role",
                    f"{name} 还缺少已复核角色：{', '.join(missing)}。没有这些角色时不能判断线有没有接对。",
                )
            )
        findings.extend(_map_profile(view, rule, name, roles))
        findings.extend(_pullups(view, rule, name, roles))
        if name == "spi":
            texts = sorted({(item.value_text or "").strip() for item in _facts(view, comp, "cs_target", None) if (item.value_text or "").strip()})
            if len(texts) > 1:
                findings.append(
                    _undecided(view, rule, comp, None, "conflicting_constraints", "cs_target", f"{comp.refdes} 的片选目标互相冲突：{', '.join(texts)}。")
                )
            elif texts:
                findings.extend(_cs_target(view, rule, roles, texts[0]))
    return finish(findings) if findings else _ok(view)


def check_conn_008(view: CircuitView, rule: Rule) -> CheckResult:
    states = _component_facts(view, "pin_state")
    polarities = _component_facts(view, "reset_polarity")
    if not states and not polarities:
        return NOT_APPLICABLE
    findings = []
    for comp, item in states:
        pin = _pin(comp, item.pin)
        mode = (item.operating_mode or "").strip()
        if mode and mode != (comp.mode or "").strip():
            if not (comp.mode or "").strip():
                findings.append(
                    _undecided(view, rule, comp, pin, "needs_review", "operating_mode", f"{comp.refdes}.{item.pin} 的默认状态取决于工作模式。", item.pin)
                )
            continue
        text = (item.value_text or "").strip().lower()
        if text not in {"idle_high", "idle_low", "not_strongly_driven"}:
            findings.append(_undecided(view, rule, comp, pin, "needs_review", "pin_state", f"{comp.refdes}.{item.pin} 的默认状态无法执行。", item.pin))
            continue
        findings.extend(_state_finding(view, rule, comp, pin, item, text))
    findings.extend(_polarity_clashes(view, rule, polarities))
    return finish(findings) if findings else _ok(view)


def check_conn_009(view: CircuitView, rule: Rule) -> CheckResult:
    devices = [
        comp
        for comp in view.circuit.components.values()
        if comp.fitted is not False and comp.role in CONDITIONAL_ROLES
    ]
    if not devices:
        return NOT_APPLICABLE
    findings = []
    for comp in devices:
        status, routes = classify_routes(comp, _facts(view, comp, "switch_route", None))
        if status == "conflict":
            findings.append(
                _undecided(view, rule, comp, None, "conflicting_constraints", "switch_route", f"{comp.refdes} 在同一工作模式下有多条开关通路。")
            )
        elif status == "mode_unknown":
            findings.append(
                _undecided(view, rule, comp, None, "needs_review", "operating_mode", f"{comp.refdes} 的导通通路取决于工作模式，当前模式未知。")
            )
        elif status == "unmodeled":
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    None,
                    "needs_review",
                    title=f"{comp.refdes} 的拓扑无法确定",
                    description=(
                        f"{comp.refdes} 是{ _role_name(comp.role) }，没有已复核的导通通路。"
                        "不能把它当成短路，也不能把它当成断开。"
                    ),
                    expected=["switch_route"],
                    actual=["unmodeled"],
                    missing=["switch_route"],
                )
            )
        elif status == "ok":
            ends = _route_pins(routes[0].value_text)
            if ends is None:
                continue
            left = view.net_of(comp.refdes, ends[0])
            right = view.net_of(comp.refdes, ends[1])
            if not left or not right:
                decision = "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist"
                findings.append(
                    _decide(
                        view,
                        rule,
                        comp,
                        None,
                        decision,
                        title=f"{comp.refdes} 的选中通路没有进入网表",
                        description=f"工作模式 {comp.mode or '不限'} 选择 {ends[0]}→{ends[1]}，其中一侧没有网络。",
                        expected=[routes[0].value_text],
                        actual=[left or "open", right or "open"],
                        facts=routes,
                    )
                )
    return finish(findings) if findings else _ok(view)


def _map_profile(view, rule, name: str, roles: list):
    kind = _kind_of(name)
    owned = _OWNED.get(kind, set())
    grouped = any(group.kind == kind for group in view.circuit.signal_groups)
    findings = []
    if name in _SAME:
        pairs = {role: role for role in _SAME[name]}
    else:
        pairs = _CROSS[name]
    hosts = [item for item in roles if item.side == "host" and (item.value_text or "").strip().lower() in pairs]
    for host in hosts:
        role = host.value_text.strip().lower()
        if grouped and role in owned:
            continue
        expected = pairs[role]
        devices = [
            item
            for item in roles
            if item.side == "device" and (item.value_text or "").strip().lower() == expected and _same_bus(host, item)
        ]
        if not devices:
            continue
        comp = view.circuit.components.get(host.refdes)
        if comp is None:
            continue
        pin = _pin(comp, host.pin)
        net = view.net_of(host.refdes, host.pin) if host.pin else None
        reached = signal_reach(view, net)
        got = []
        for device in devices:
            other = view.net_of(device.refdes, device.pin)
            if other and other in reached:
                got.append(device)
        if got:
            continue
        bridge = conditional_bridge(view, net, view.net_of(devices[0].refdes, devices[0].pin))
        if bridge is not None:
            findings.append(
                _undecided(
                    view,
                    rule,
                    comp,
                    pin,
                    "needs_review",
                    "switch_route",
                    f"{name} 的 {role} 经过 {bridge.refdes}，该器件的导通通路尚未确定。",
                    host.pin,
                )
            )
            continue
        swapped = _swapped(view, name, host, roles, reached)
        if swapped:
            findings.append(
                _undecided(
                    view,
                    rule,
                    comp,
                    pin,
                    "needs_review",
                    "polarity",
                    f"{name} 的 {role} 到达了对端相反极性。没有已复核的允许交换结论时，不直接判成接反。",
                    host.pin,
                )
            )
            continue
        wrong = _wrong_arrival(view, roles, host, expected, reached)
        decision = "needs_review" if wrong and not view.circuit.connectivity_complete else (
            "confirmed_error" if wrong or view.circuit.connectivity_complete else "insufficient_netlist"
        )
        if not net and not view.circuit.connectivity_complete:
            decision = "insufficient_netlist"
        findings.append(
            _decide(
                view,
                rule,
                comp,
                pin,
                decision,
                title=f"{name} 的 {role} 没有到达预期对端",
                description=(
                    f"{host.refdes}.{host.pin} 的 {role} 应当到达对端 {expected}。"
                    + (f"实际到达 {wrong}。" if wrong else "对端该角色的网络没有连通。")
                ),
                expected=[expected],
                actual=[wrong or net or "open"],
                facts=[host],
                number=host.pin,
            )
        )
    return findings


def _pullups(view, rule, name: str, roles: list):
    if name != "i2c":
        return []
    findings = []
    required = [item for item in roles if (item.value_text or "").strip().lower() in {"sda", "scl"} and item.side == "host"]
    domains = []
    for comp in view.circuit.components.values():
        domains.extend(_facts(view, comp, "pullup_domain", None))
        domains.extend(_facts(view, comp, "require_pullup", None))
    if not domains:
        return []
    asked = {item.parameter for item in domains}
    wanted = next(((item.value_text or "").strip() for item in domains if item.parameter == "pullup_domain" and item.value_text), "")
    for host in required:
        comp = view.circuit.components.get(host.refdes)
        pin = _pin(comp, host.pin) if comp is not None else None
        net = view.net_of(host.refdes, host.pin)
        if not net:
            continue
        bias = view.bias(net)
        rails = [item for item in bias.boundaries if is_power(view.circuit, item)]
        if ("require_pullup" in asked or wanted) and not rails:
            if bias.missing:
                decision = "needs_review"
            elif not view.circuit.connectivity_complete:
                decision = "insufficient_netlist"
            else:
                decision = "confirmed_error"
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    decision,
                    title=f"I²C {host.value_text} 没有上拉",
                    description=f"{host.refdes}.{host.pin} 所在网络 {net} 没有到电源的上拉通路。",
                    expected=["pullup"],
                    actual=[net],
                    facts=domains,
                    number=host.pin,
                )
            )
            continue
        if not wanted or not rails:
            continue
        if wanted in rails:
            continue
        wanted_v = rail_voltage(view.circuit, wanted)
        other_v = [rail_voltage(view.circuit, rail) for rail in rails]
        if wanted_v is None or any(item is None for item in other_v):
            findings.append(
                _undecided(view, rule, comp, pin, "needs_review", "pullup_domain", f"I²C {host.value_text} 的上拉电压没有都给出，不能判断电压域。", host.pin)
            )
            continue
        if any(compare("!=", wanted_v, item)["result"] == "true" for item in other_v):
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    "confirmed_error",
                    title=f"I²C {host.value_text} 的上拉电源不符合约束",
                    description=f"已复核上拉网络是 {wanted}，实际偏置来自 {', '.join(rails)}。",
                    expected=[wanted],
                    actual=rails,
                    facts=domains,
                    number=host.pin,
                )
            )
    return findings


def _cs_target(view, rule, roles: list, target: str):
    hosts = [item for item in roles if item.side == "host" and (item.value_text or "").strip().lower() == "cs"]
    findings = []
    for host in hosts:
        net = view.net_of(host.refdes, host.pin)
        reached = signal_reach(view, net)
        devices = [item for item in roles if item.side == "device" and (item.value_text or "").strip().lower() == "cs"]
        hit = [item.refdes for item in devices if view.net_of(item.refdes, item.pin) in reached]
        comp = view.circuit.components.get(host.refdes)
        pin = _pin(comp, host.pin) if comp is not None else None
        if hit == [target]:
            continue
        if not hit and not view.circuit.connectivity_complete:
            decision = "insufficient_netlist"
        elif len(hit) > 1:
            decision = "needs_review"
        else:
            decision = "confirmed_error" if view.circuit.connectivity_complete or hit else "insufficient_netlist"
        findings.append(
            _decide(
                view,
                rule,
                comp,
                pin,
                decision,
                title="SPI 片选没有选中指定器件",
                description=f"{host.refdes}.{host.pin} 应当选中 {target}，实际到达 {', '.join(hit) or '没有器件'}。",
                expected=[target],
                actual=hit or ["open"],
                facts=[host],
                number=host.pin,
            )
        )
    return findings


def _state_finding(view, rule, comp, pin, item, text: str):
    net = pin.net if pin is not None else None
    if text == "not_strongly_driven":
        if not net:
            return []
        drivers = []
        for name in signal_reach(view, net):
            for other, other_pin in view.on_net.get(name, []):
                if other.refdes == comp.refdes:
                    continue
                if other_pin.output_kind == "push_pull" and other_pin.direction in {"output", "bidirectional"}:
                    drivers.append(f"{other.refdes}.{other_pin.number}")
                elif other_pin.direction in {"output", "bidirectional"} and other_pin.output_kind in {"", "unknown"}:
                    return [
                        _undecided(
                            view,
                            rule,
                            comp,
                            pin,
                            "needs_review",
                            "output_kind",
                            f"{comp.refdes}.{item.pin} 上还有输出，但输出类型未知，不能判断启动配置是否被强驱动。",
                            item.pin,
                        )
                    ]
        if not drivers:
            return []
        return [
            _decide(
                view,
                rule,
                comp,
                pin,
                "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist",
                title=f"{comp.refdes}.{item.pin} 的启动状态被强驱动",
                description=f"已复核约束要求该脚不要被推挽输出占住，网表里 {', '.join(drivers)} 驱动着同一网络。",
                expected=["not_strongly_driven"],
                actual=drivers,
                facts=[item],
                number=item.pin,
            )
        ]
    if not net:
        decision = "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist"
        return [
            _decide(
                view,
                rule,
                comp,
                pin,
                decision,
                title=f"{comp.refdes}.{item.pin} 没有默认电平",
                description=f"已复核默认状态是 {text}，这个引脚没有网络。",
                expected=[text],
                actual=["open"],
                facts=[item],
                number=item.pin,
            )
        ]
    bias = view.bias(net)
    rails = [name for name in bias.boundaries if is_power(view.circuit, name)]
    grounds = [name for name in bias.boundaries if is_ground(view.circuit, name)]
    if text == "idle_high" and _direct(view, net, grounds) and not rails:
        return [_level_error(view, rule, comp, pin, item, text, "地")]
    if text == "idle_low" and _direct(view, net, rails) and not grounds:
        return [_level_error(view, rule, comp, pin, item, text, "电源")]
    if text == "idle_high" and rails:
        return []
    if text == "idle_low" and grounds:
        return []
    if bias.missing or not view.circuit.connectivity_complete:
        return [
            _undecided(view, rule, comp, pin, "needs_review" if bias.missing else "insufficient_netlist", "pin_state", f"{comp.refdes}.{item.pin} 的默认电平还不能确定。", item.pin)
        ]
    return [
        _decide(
            view,
            rule,
            comp,
            pin,
            "confirmed_error",
            title=f"{comp.refdes}.{item.pin} 的默认电平不符合约束",
            description=f"已复核默认状态是 {text}，直流通路没有到达对应的电源或地。",
            expected=[text],
            actual=[net],
            facts=[item],
            number=item.pin,
        )
    ]


def _polarity_clashes(view, rule, polarities):
    groups: dict[str, list] = {}
    findings = []
    for comp, item in polarities:
        mode = (item.operating_mode or "").strip()
        if mode and mode != (comp.mode or "").strip():
            if not (comp.mode or "").strip():
                findings.append(
                    _undecided(view, rule, comp, _pin(comp, item.pin), "needs_review", "operating_mode", f"{comp.refdes}.{item.pin} 的复位极性取决于工作模式。", item.pin)
                )
            continue
        key = (item.condition or "").strip()
        if not key:
            continue
        groups.setdefault(key, []).append((comp, item))
    for members in groups.values():
        texts = {(item.value_text or "").strip().lower() for _comp, item in members}
        if len(texts) < 2:
            continue
        nets = []
        for comp, item in members:
            net = view.net_of(comp.refdes, item.pin)
            if net:
                nets.append(net)
        if len(nets) < 2:
            continue
        if all(signal_reach(view, nets[0]) & {net} or net == nets[0] for net in nets):
            if view.short_root(nets[0]) and all(view.short_root(net) == view.short_root(nets[0]) for net in nets):
                comp, item = members[0]
                findings.append(
                    _decide(
                        view,
                        rule,
                        comp,
                        _pin(comp, item.pin),
                        "confirmed_error",
                        title="复位极性相反的引脚被短接",
                        description="同一组里已复核的复位极性不一致，而这些引脚经过 0Ω 连在一起。",
                        expected=["matching_polarity"],
                        actual=sorted(texts),
                        facts=[item for _comp, item in members],
                        number=item.pin,
                    )
                )
            else:
                comp, item = members[0]
                findings.append(
                    _undecided(view, rule, comp, _pin(comp, item.pin), "needs_review", "reset_polarity", "复位极性相反，但连接不是直接短接，不能确定经过反相。", item.pin)
                )
    return findings


def _profiles(view: CircuitView):
    found = []
    for comp in view.circuit.components.values():
        if comp.fitted is False:
            continue
        for item in _facts(view, comp, "interface_profile", None):
            found.append((comp, item))
    return found


def _interface_roles(view: CircuitView, interface: str):
    roles = []
    unknown = False
    for comp in view.circuit.components.values():
        if comp.fitted is False:
            continue
        for pin in comp.pins:
            pin_state, items = classify_pin_roles(view, comp, pin.number)
            relevant = [item for item in items if (item.interface or "").strip().lower() == interface and (item.value_text or "").strip()]
            if pin_state == "conflict" and relevant:
                return "conflict", []
            if pin_state == "mode_unknown" and relevant:
                unknown = True
                continue
            roles.extend(relevant)
    if unknown:
        return "mode_unknown", roles
    return "ok", roles


def _missing_roles(name: str, roles: list) -> list[str]:
    needed = list(_SAME.get(name, _CROSS.get(name, {})))
    found = {(item.side, (item.value_text or "").strip().lower()) for item in roles}
    missing = []
    for role in needed:
        if ("host", role) not in found or ("device", role) not in found:
            missing.append(role)
    return missing


def _swapped(view, name: str, host, roles: list, reached: set[str]) -> bool:
    pairs = _DIFF.get(_kind_of(name), ())
    role = host.value_text.strip().lower()
    for positive, negative in pairs:
        if role not in {positive, negative}:
            continue
        other = negative if role == positive else positive
        opposite = _CROSS[name][other]
        for device in roles:
            if device.side != "device" or (device.value_text or "").strip().lower() != opposite:
                continue
            net = view.net_of(device.refdes, device.pin)
            if net and net in reached:
                return True
    return False


def _wrong_arrival(view, roles, host, expected: str, reached: set[str]) -> str:
    got = []
    for device in roles:
        if device.side != "device" or device.refdes == host.refdes:
            continue
        net = view.net_of(device.refdes, device.pin)
        if net and net in reached:
            got.append((device.value_text or "").strip().lower())
    others = [item for item in got if item and item != expected]
    return ",".join(sorted(set(others)))


def _component_facts(view, parameter: str):
    found = []
    for comp in view.circuit.components.values():
        if comp.fitted is False:
            continue
        for item in _facts(view, comp, parameter, None):
            if item.pin:
                found.append((comp, item))
        for pin in comp.pins:
            for item in _facts(view, comp, parameter, pin.number):
                found.append((comp, item))
    return _unique_pairs(found)


def _pin(comp: Component | None, number: str):
    if comp is None:
        return None
    for pin in comp.pins:
        if pin.number == number or pin.name == number:
            return pin
    return None


def _kind_of(name: str) -> str:
    if name.startswith("sdio"):
        return "sdio"
    if name.startswith("uart"):
        return "uart"
    return name


def _same_bus(host, device) -> bool:
    left = (host.interface or "").strip().lower()
    right = (device.interface or "").strip().lower()
    return not left or not right or left == right


def _direct(view, net: str, others: list[str]) -> bool:
    root = view.short_root(net)
    return bool(root) and any(view.short_root(item) == root for item in others)


def _level_error(view, rule, comp, pin, item, text: str, landed: str):
    return _decide(
        view,
        rule,
        comp,
        pin,
        "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist",
        title=f"{comp.refdes}.{item.pin} 的默认电平被{landed}固定",
        description=f"已复核默认状态是 {text}，该脚经 0Ω 接到了{landed}。",
        expected=[text],
        actual=[landed],
        facts=[item],
        number=item.pin,
    )


def _role_name(role: str) -> str:
    return {"mux": "多路开关", "analog_switch": "模拟开关", "isolator": "隔离器"}.get(role, role)


def _unique_pairs(pairs):
    seen = set()
    found = []
    for comp, item in pairs:
        key = (comp.refdes, item.constraint_id, item.pin, item.parameter, item.value_text, item.operating_mode)
        if key in seen:
            continue
        seen.add(key)
        found.append((comp, item))
    return found
