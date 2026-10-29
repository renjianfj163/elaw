"""网表上的连接正确性。第一阶段只检查电源路径、必需引脚、输入驱动和通路语义。

确定结论只来自网表里已经写明的连接，以及已复核的硬性约束。
电容、晶振和没有通道模型的器件不会被当成直流短路。
"""

from __future__ import annotations

from app.schematic.engine.calculators import compare
from app.schematic.engine.checks_mode import classify_pin_roles
from app.schematic.engine.checks_common import pages, pin_label
from app.schematic.engine.dc import CircuitView, is_ground, is_power
from app.schematic.engine.model import Component, Pin
from app.schematic.engine.paths import Span, dc_span
from app.schematic.engine.report import NOT_APPLICABLE, CheckResult, Rule, finish, make_finding

_HARD = {"", "hard_limit"}
_INPUT_TYPES = {"", "gpio", "io", "signal", "nrst", "boot", "reset"}
_SOURCE_ROLES = {"regulator", "load_switch", "power_input"}
_SOURCE_PINS = {"vout", "power_out", "power"}


def check_conn_001(view: CircuitView, rule: Rule) -> CheckResult:
    pins = [item for item in _power_pins(view) if not _verified_texts(view, item[0], "supply_net", item[1].number)]
    if not pins:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in pins:
        found = _power_path_finding(view, rule, comp, pin, expected_net="")
        if found is not None:
            findings.append(found)
    return finish(findings) if findings else _ok(view)


def check_conn_002(view: CircuitView, rule: Rule) -> CheckResult:
    pins = _power_pins(view)
    if not pins:
        return NOT_APPLICABLE
    findings = []
    checked = False
    for comp, pin in pins:
        if not pin.net:
            continue
        checked = True
        span = dc_span(view.circuit, pin.net)
        if not _span_has_supply(view, span):
            continue
        required = _verified_texts(view, comp, "supply_source", pin.number)
        if _conflicted(view, comp, "supply_source"):
            findings.append(_undecided(view, rule, comp, pin, "conflicting_constraints", "supply_source", "供电来源约束互相冲突。"))
            continue
        if required:
            if _source_matches(view, span, required):
                continue
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    "confirmed_error",
                    title=f"{pin_label(comp, pin)} 没有接到指定供电源",
                    description=(
                        f"{pin_label(comp, pin)} 的直流通路没有到达已复核的供电源 {', '.join(required)}。"
                    ),
                    expected=required,
                    actual=["source_missing"],
                    span=span,
                    facts=_verified_items(view, comp, "supply_source", pin.number),
                )
            )
            continue
        if _identified_source(view, span):
            continue
        findings.append(
            _decide(
                view,
                rule,
                comp,
                pin,
                "needs_review",
                title=f"{pin_label(comp, pin)} 的供电源无法从网表识别",
                description=(
                    f"{pin_label(comp, pin)} 接到了电源网络，但网表里看不到稳压器输出、负载开关或外部电源输入。"
                    "这不能据此认定电源接错。"
                ),
                expected=["identified_supply"],
                actual=[pin.net],
                span=span,
                missing=["supply_source"],
            )
        )
    if findings:
        return finish(findings)
    if not checked:
        return NOT_APPLICABLE
    return _ok(view)


def check_conn_003(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    saw = False
    for comp in view.circuit.components.values():
        if comp.fitted is False or comp.role != "regulator":
            continue
        vin = _typed(comp, {"vin", "power_in"})
        vout = _typed(comp, {"vout", "power_out"})
        if not vin or not vout or not vin.net or not vout.net:
            continue
        saw = True
        findings.extend(_short_finding(view, rule, vin.net, vout.net, f"{comp.refdes} 的输入和输出", [comp.refdes]))
    domains = [item for item in view.circuit.power_domains if item.net]
    for index, left in enumerate(domains):
        for right in domains[index + 1 :]:
            if left.net == right.net and _same_voltage(left.nominal_v, right.nominal_v):
                continue
            saw = True
            findings.extend(
                _domain_short(view, rule, left.net, right.net, left.nominal_v, right.nominal_v)
            )
    if findings:
        return finish(findings)
    if not saw:
        return NOT_APPLICABLE
    return _ok(view)


def check_conn_004(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    saw = False
    for comp in view.circuit.components.values():
        if comp.fitted is False:
            continue
        for pin in comp.pins:
            texts = _verified_texts(view, comp, "supply_net", pin.number)
            pending = _pending(view, comp, "supply_net", pin.number)
            if not texts and not pending and not _conflicted(view, comp, "supply_net"):
                continue
            saw = True
            if _conflicted(view, comp, "supply_net") or len(texts) > 1:
                findings.append(_undecided(view, rule, comp, pin, "conflicting_constraints", "supply_net", "供电网络约束不一致。"))
                continue
            if not texts:
                findings.append(_undecided(view, rule, comp, pin, "missing_pin_role", "supply_net", "供电网络约束尚未复核，不能判断该接到哪一路。"))
                continue
            target = texts[0]
            if pin.net and (pin.net == target or target in dc_span(view.circuit, pin.net).nets):
                continue
            decision = "confirmed_error" if view.circuit.connectivity_complete or pin.net else "insufficient_netlist"
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    decision,
                    title=f"{pin_label(comp, pin)} 没有接到要求的电源",
                    description=(
                        f"{pin_label(comp, pin)} 应当直流连接到 {target}，"
                        f"当前网络是 {pin.net or '未连接'}。"
                    ),
                    expected=[target],
                    actual=[pin.net or "open"],
                    span=dc_span(view.circuit, pin.net) if pin.net else None,
                    facts=_verified_items(view, comp, "supply_net", pin.number),
                )
            )
        policy = _verified_texts(view, comp, "ground_policy", None)
        if _conflicted(view, comp, "ground_policy") or len(policy) > 1:
            saw = True
            findings.append(
                _undecided(view, rule, comp, None, "conflicting_constraints", "ground_policy", "地连接要求互相冲突。")
            )
        elif policy:
            saw = True
            parsed = _policy(policy[0])
            if parsed is None:
                findings.append(
                    _undecided(view, rule, comp, None, "needs_review", "ground_policy", "地连接要求的写法无法执行。")
                )
            else:
                findings.extend(_ground_findings(view, rule, comp, parsed, _verified_items(view, comp, "ground_policy", None)))
        elif _pending(view, comp, "ground_policy", None):
            saw = True
            findings.append(
                _undecided(view, rule, comp, None, "missing_pin_role", "ground_policy", "地连接要求尚未复核。不同的地网络名本身不会判成错误。")
            )
    if findings:
        return finish(findings)
    if not saw:
        return NOT_APPLICABLE
    return _ok(view)


def check_conn_005(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    saw = False
    for comp in view.circuit.components.values():
        if comp.fitted is False:
            continue
        numbers = _pins_of(view, comp, "pin_tie")
        for number in numbers:
            saw = True
            texts = _verified_texts(view, comp, "pin_tie", number)
            if _conflicted(view, comp, "pin_tie") or len(texts) > 1:
                pin = _find_pin(comp, number)
                findings.append(_undecided(view, rule, comp, pin, "conflicting_constraints", "pin_tie", f"引脚 {number} 的连接要求互相冲突。", number))
                continue
            if not texts:
                pin = _find_pin(comp, number)
                if _conditional(view, comp, "pin_tie", number) or _marked(view, comp, "pin_tie", number, "recommended"):
                    kind = "needs_review"
                else:
                    kind = "missing_pin_role"
                findings.append(_undecided(view, rule, comp, pin, kind, "pin_tie", f"引脚 {number} 的连接要求不能单独支撑确定结论。", number))
                continue
            text = texts[0]
            pin = _find_pin(comp, number)
            net = view.net_of(comp.refdes, number)
            facts = _verified_items(view, comp, "pin_tie", number)
            if text in {"required", "must_connect"}:
                if net:
                    continue
                decision = "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist"
                present = pin is not None
                findings.append(
                    _decide(
                        view,
                        rule,
                        comp,
                        pin,
                        decision,
                        title=f"{comp.refdes}.{number} 是必需引脚但没有连接",
                        description=(
                            f"{comp.refdes}.{number} 的已复核约束要求连接。"
                            + ("网表里没有这个引脚。" if not present else "这个引脚没有网络。")
                        ),
                        expected=["connected"],
                        actual=["missing_pin" if not present else "open"],
                        facts=facts,
                        number=number,
                    )
                )
            elif text in {"open", "nc", "float"}:
                if not net:
                    continue
                findings.append(
                    _decide(
                        view,
                        rule,
                        comp,
                        pin,
                        "confirmed_error",
                        title=f"{comp.refdes}.{number} 应按资料保持不连接",
                        description=f"{comp.refdes}.{number} 的已复核约束要求悬空或保持 NC，网表把它接到了 {net}。",
                        expected=["open"],
                        actual=[net],
                        facts=facts,
                        number=number,
                    )
                )
            else:
                findings.append(
                    _undecided(view, rule, comp, pin, "needs_review", "pin_tie", f"引脚 {number} 的连接要求是 {text}，没有按必须连接或必须悬空执行。", number)
                )
    if findings:
        return finish(findings)
    if not saw:
        return NOT_APPLICABLE
    return _ok(view)


def check_conn_006(view: CircuitView, rule: Rule) -> CheckResult:
    pins = [(comp, pin) for comp, pin in _input_pins(view)]
    if not pins:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in pins:
        role_state, _roles = classify_pin_roles(view, comp, pin.number)
        if role_state == "conflict":
            findings.append(
                _undecided(
                    view,
                    rule,
                    comp,
                    pin,
                    "conflicting_constraints",
                    "pin_role",
                    f"{pin_label(comp, pin)} 在同一工作模式下有互相冲突的引脚角色，不能判断它是不是需要驱动的输入。",
                )
            )
            continue
        if role_state == "mode_unknown":
            findings.append(
                _undecided(
                    view,
                    rule,
                    comp,
                    pin,
                    "needs_review",
                    "operating_mode",
                    f"{pin_label(comp, pin)} 的角色取决于工作模式，网表没有给出当前模式，不能判断它是否需要驱动。",
                )
            )
            continue
        if not pin.net:
            decision = "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist"
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    decision,
                    title=f"{pin_label(comp, pin)} 没有驱动源",
                    description=f"{pin_label(comp, pin)} 在网表里是输入，但没有接入网络。",
                    expected=["driver"],
                    actual=["open"],
                )
            )
            continue
        kind, driver = _driver(view, comp, pin)
        if kind == "driver":
            continue
        if kind == "bias":
            continue
        span = dc_span(view.circuit, pin.net)
        if span.opaque:
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    "needs_review",
                    title=f"{pin_label(comp, pin)} 的驱动通路无法确定",
                    description=(
                        f"{pin_label(comp, pin)} 旁边有没有通路模型的器件 {', '.join(ref for ref, _net in span.opaque)}。"
                        "不能把它当成导线，也不能据此认定输入悬空。"
                    ),
                    expected=["driver"],
                    actual=[f"opaque:{ref}" for ref, _net in span.opaque],
                    span=span,
                )
            )
            continue
        if span.ac or span.not_dc:
            coupled = span.ac or span.not_dc
            findings.append(
                _decide(
                    view,
                    rule,
                    comp,
                    pin,
                    "needs_review",
                    title=f"{pin_label(comp, pin)} 只有非直流通路",
                    description=(
                        f"{pin_label(comp, pin)} 没有直流驱动源。{', '.join(ref for ref, _net in coupled)} 不把两侧视为同一个直流网络。"
                        "交流耦合是否符合该信号的工作方式，网表本身不能确定。"
                    ),
                    expected=["dc_driver"],
                    actual=[f"not_dc:{ref}" for ref, _net in coupled],
                    span=span,
                    assumptions=["串联电容和晶振可以是交流通路，这里不把它记成直流驱动，也不直接判成断线故障。"],
                )
            )
            continue
        decision = "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist"
        findings.append(
            _decide(
                view,
                rule,
                comp,
                pin,
                decision,
                title=f"{pin_label(comp, pin)} 没有驱动源",
                description=f"{pin_label(comp, pin)} 所在网络 {pin.net} 上没有输出、开漏驱动，也没有到电源或地的偏置。",
                expected=["driver"],
                actual=[pin.net],
                span=span,
            )
        )
    return finish(findings) if findings else _ok(view)


def _power_pins(view: CircuitView) -> list[tuple[Component, Pin]]:
    found = []
    for comp in view.circuit.components.values():
        if comp.fitted is False or comp.kind != "ic":
            continue
        for pin in comp.pins:
            if pin.pin_type == "power" or "required" in _verified_texts(view, comp, "power_pin", pin.number):
                found.append((comp, pin))
    return found


def _power_path_finding(view: CircuitView, rule: Rule, comp: Component, pin: Pin, expected_net: str):
    label = pin_label(comp, pin)
    if not pin.net:
        decision = "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist"
        return _decide(
            view,
            rule,
            comp,
            pin,
            decision,
            title=f"{label} 没有连接到电源",
            description=f"{label} 是电源引脚，网表里没有网络。",
            expected=[expected_net or "dc_supply"],
            actual=["open"],
        )
    span = dc_span(view.circuit, pin.net)
    if _span_has_supply(view, span) and not _only_ground(view, span):
        return None
    if span.opaque:
        return _decide(
            view,
            rule,
            comp,
            pin,
            "needs_review",
            title=f"{label} 的供电路径无法确定",
            description=(
                f"{label} 与其它网络之间有 {', '.join(ref for ref, _net in span.opaque)}。"
                "这些器件没有已复核的直流通路模型，不能默认短路，也不能默认断开。"
            ),
            expected=[expected_net or "dc_supply"],
            actual=[f"opaque:{ref}" for ref, _net in span.opaque],
            span=span,
        )
    bridged = span.ac or span.not_dc
    if bridged:
        names = ", ".join(ref for ref, _net in bridged)
        return _decide(
            view,
            rule,
            comp,
            pin,
            "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist",
            title=f"{label} 没有直流通路到达电源",
            description=f"{label} 只经过 {names} 碰到其它网络。电容和晶振不构成供电。",
            expected=[expected_net or "dc_supply"],
            actual=[f"not_dc:{ref}" for ref, _net in bridged],
            span=span,
            assumptions=["串联电容和晶振保持在交流或振荡通路里，两侧不是同一个直流网络。"],
        )
    if _only_ground(view, span):
        return _decide(
            view,
            rule,
            comp,
            pin,
            "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist",
            title=f"{label} 的直流通路只到地",
            description=f"{label} 所在的直流通路到达了地，没有到达正电源。",
            expected=[expected_net or "dc_supply"],
            actual=span.nets,
            span=span,
        )
    return _decide(
        view,
        rule,
        comp,
        pin,
        "confirmed_error" if view.circuit.connectivity_complete else "insufficient_netlist",
        title=f"{label} 没有直流通路到达电源",
        description=f"{label} 在网络 {pin.net} 上，沿电阻、0Ω、电感和磁珠没有到达供电源。",
        expected=[expected_net or "dc_supply"],
        actual=span.nets,
        span=span,
    )


def _short_finding(view, rule, left: str, right: str, title: str, components: list[str]):
    if left == right or view.short_root(left) == view.short_root(right):
        return [
            make_finding(
                rule,
                title=f"{title}被直接短接",
                status="confirmed",
                decision="confirmed_error",
                description=f"{left} 与 {right} 在网表里是同一直流节点，或只经过 0Ω。",
                components=components,
                nets=[left, right],
                path=_trace(view, left, right),
                expected=["isolated"],
                actual=["short"],
                fact_ids=[f"netlist:{left}", f"netlist:{right}"],
                sources=["netlist"],
                assumptions=["只有同一网络或 0Ω 合并视为直接短接。电阻和电容不在这条规则里被当成短接。"],
            )
        ]
    span = dc_span(view.circuit, left)
    if right in span.nets:
        return [
            make_finding(
                rule,
                title=f"{title}之间有电阻通路",
                status="needs_review",
                decision="needs_review",
                severity="medium",
                description=f"{left} 与 {right} 经过 {', '.join(span.parts) or '无源器件'} 相连，不能直接认定输入输出并联。",
                components=[*components, *span.parts],
                nets=[left, right],
                path=span.trace_to(right),
                expected=["isolated"],
                actual=span.hops,
                sources=["netlist"],
            )
        ]
    return []


def _domain_short(view, rule, left: str, right: str, left_v: float | None, right_v: float | None):
    same_node = left == right or view.short_root(left) == view.short_root(right)
    span = dc_span(view.circuit, left)
    resistive = right in span.nets and not same_node
    if not same_node and not resistive:
        return []
    if left_v is None or right_v is None:
        return [
            make_finding(
                rule,
                title=f"{left} 与 {right} 相连，但电压没有都给出",
                status="needs_review",
                decision="needs_review",
                severity="medium",
                description=f"{left} 和 {right} 有直流通路，其中一路没有电压，不能认定不同电源被并联。",
                nets=[left, right],
                path=span.trace_to(right) if right in span.parent else [left, right],
                expected=["known_voltage"],
                actual=["voltage_missing"],
                missing=["nominal_v"],
                sources=["netlist"],
            )
        ]
    if not _different(left_v, right_v):
        return []
    if same_node:
        return [
            make_finding(
                rule,
                title=f"{left} 与 {right} 被直接并联",
                status="confirmed",
                decision="confirmed_error",
                description=f"{left}（{left_v:g} V）和 {right}（{right_v:g} V）连在同一直流节点上。",
                nets=[left, right],
                path=_trace(view, left, right),
                expected=[f"{left_v:g}V isolated from {right_v:g}V"],
                actual=["short"],
                fact_ids=[f"netlist:{left}", f"netlist:{right}"],
                sources=["netlist"],
                assumptions=["电压来自网表里的电源域。只有同一网络或 0Ω 才记为直接并联。"],
            )
        ]
    return [
        make_finding(
            rule,
            title=f"{left} 与 {right} 之间有电阻",
            status="needs_review",
            decision="needs_review",
            severity="medium",
            description=f"{left}（{left_v:g} V）和 {right}（{right_v:g} V）经过电阻或电感相连，不能直接认定电源输出并联。",
            nets=[left, right],
            path=span.trace_to(right),
            expected=["isolated"],
            actual=span.hops,
            sources=["netlist"],
        )
    ]


def _ground_findings(view, rule, comp: Component, policy: tuple[str, list[str]], facts):
    kind, nets = policy
    findings = []
    for index, left in enumerate(nets):
        for right in nets[index + 1 :]:
            span = dc_span(view.circuit, left)
            joined = left == right or right in span.nets
            if kind == "separate" and joined:
                findings.append(
                    _decide(
                        view,
                        rule,
                        comp,
                        None,
                        "confirmed_error" if view.circuit.connectivity_complete or joined else "insufficient_netlist",
                        title=f"{comp.refdes} 要求分开的地被连在一起",
                        description=f"已复核约束要求 {left} 与 {right} 不要直流相连，网表里它们有直流通路。",
                        expected=[f"separate {left} {right}"],
                        actual=span.hops or [left],
                        span=span,
                        facts=facts,
                    )
                )
            elif kind == "join" and not joined:
                blocked = span.ac or span.not_dc or span.opaque
                if span.opaque:
                    decision = "needs_review"
                    text = "中间器件没有通路模型，不能判断这两路地是否应该视为已连接。"
                elif not view.circuit.connectivity_complete:
                    decision = "insufficient_netlist"
                    text = "网表可能不完整，不能确定这两路地没有连上。"
                else:
                    decision = "confirmed_error"
                    text = "沿电阻、0Ω、电感和磁珠没有直流通路。"
                    if blocked:
                        text += "电容或晶振不作为直流连接。"
                findings.append(
                    _decide(
                        view,
                        rule,
                        comp,
                        None,
                        decision,
                        title=f"{comp.refdes} 要求连接的地没有直流连通",
                        description=f"已复核约束要求 {left} 与 {right} 直流相连。{text}",
                        expected=[f"join {left} {right}"],
                        actual=["open"],
                        span=span,
                        facts=facts,
                    )
                )
    return findings


def _driver(view: CircuitView, comp: Component, pin: Pin) -> tuple[str, str]:
    for name in view.reach(pin.net):
        for other, other_pin in view.on_net.get(name, []):
            if other.refdes == comp.refdes and other_pin.number == pin.number:
                continue
            if other.fitted is False or other.kind != "ic":
                continue
            if other_pin.direction in {"output", "bidirectional"} or other_pin.output_kind in {
                "push_pull",
                "open_drain",
                "open_source",
            }:
                return "driver", pin_label(other, other_pin)
    bias = view.bias(pin.net)
    rails = [net for net in bias.boundaries if is_power(view.circuit, net) or is_ground(view.circuit, net)]
    if bias.determined and rails and not bias.conflict:
        return "bias", ",".join(rails)
    return "", ""


def _input_pins(view: CircuitView):
    for comp in view.circuit.components.values():
        if comp.fitted is False or comp.kind != "ic":
            continue
        for pin in comp.pins:
            if pin.direction != "input" or pin.pin_type not in _INPUT_TYPES:
                continue
            yield comp, pin


def _span_has_supply(view: CircuitView, span: Span) -> bool:
    return any(_is_supply_net(view, net) for net in span.nets)


def _only_ground(view: CircuitView, span: Span) -> bool:
    grounds = [net for net in span.nets if is_ground(view.circuit, net)]
    supplies = [net for net in span.nets if _is_supply_net(view, net) and not is_ground(view.circuit, net)]
    return bool(grounds) and not supplies


def _source_matches(view: CircuitView, span: Span, required: list[str]) -> bool:
    for name in required:
        for domain in view.circuit.power_domains:
            if domain.net in span.nets and (domain.source or "") == name:
                return True
        for net in span.nets:
            for comp, pin in view.on_net.get(net, []):
                if comp.refdes == name and (pin.pin_type in _SOURCE_PINS or comp.role in _SOURCE_ROLES):
                    return True
    return False


def _identified_source(view: CircuitView, span: Span) -> bool:
    for domain in view.circuit.power_domains:
        if domain.net in span.nets and (domain.source or "").strip():
            return True
    for net in span.nets:
        for comp, pin in view.on_net.get(net, []):
            if comp.fitted is False:
                continue
            if pin.pin_type in _SOURCE_PINS and comp.role in _SOURCE_ROLES:
                return True
            if comp.role == "power_input":
                return True
            if pin.pin_type in {"vout", "power_out"}:
                return True
    return False


def _is_supply_net(view: CircuitView, net: str) -> bool:
    if is_power(view.circuit, net):
        return True
    for domain in view.circuit.power_domains:
        if domain.net == net and (domain.nominal_v or 0) > 0:
            return True
    for comp, pin in view.on_net.get(net, []):
        if pin.pin_type in {"vout", "power_out"}:
            return True
        if comp.role in _SOURCE_ROLES and pin.pin_type in _SOURCE_PINS:
            return True
    return False


def _decide(
    view,
    rule,
    comp: Component,
    pin: Pin | None,
    decision: str,
    *,
    title: str,
    description: str,
    expected: list[str],
    actual: list[str],
    span: Span | None = None,
    facts=None,
    missing: list[str] | None = None,
    assumptions: list[str] | None = None,
    number: str = "",
):
    status = {
        "confirmed_error": "confirmed",
        "needs_review": "needs_review",
        "insufficient_netlist": "not_assessable",
        "missing_pin_role": "not_assessable",
        "conflicting_constraints": "needs_review",
    }[decision]
    severity = "critical" if decision == "confirmed_error" and rule.rule_id in {"CONN-001", "CONN-003"} else (
        "high" if decision == "confirmed_error" else "medium" if status != "not_assessable" else "info"
    )
    path = []
    nets = []
    parts = [comp.refdes]
    if pin is not None and pin.net:
        nets.append(pin.net)
    if span is not None:
        path = [pin_label(comp, pin)] if pin is not None else [comp.refdes]
        path.extend(span.hops)
        if not span.hops and span.start:
            path.append(span.start)
        nets = list(dict.fromkeys([*nets, *span.nets]))
        parts = list(dict.fromkeys([*parts, *span.parts]))
    elif pin is not None:
        path = [pin_label(comp, pin), "未接入网络" if not pin.net else pin.net]
    fact_ids = [getattr(item, "constraint_id", "") for item in (facts or []) if getattr(item, "constraint_id", "")]
    if pin is not None:
        fact_ids.append(f"netlist:{comp.refdes}.{pin.number}")
    elif number:
        fact_ids.append(f"netlist:{comp.refdes}.{number}")
    sources = ["netlist"]
    for item in facts or []:
        label = getattr(item, "constraint_id", "") or getattr(item, "parameter", "")
        if label and label not in sources:
            sources.append(label)
    return make_finding(
        rule,
        title=title,
        status=status,
        severity=severity,
        decision=decision,
        description=description,
        components=parts,
        nets=nets,
        pins=[pin_label(comp, pin)] if pin is not None else ([f"{comp.refdes}.{number}"] if number else []),
        path=path,
        expected=expected,
        actual=actual,
        fact_ids=fact_ids,
        sources=sources,
        pages=pages(view, [comp.refdes]),
        missing=missing,
        assumptions=assumptions,
        constraints=list(facts or []),
        verification_steps=["沿 traced_path 核对网表连接。", "核对引脚约束的复核状态、型号和页码。"],
    )


def _undecided(view, rule, comp, pin, decision: str, parameter: str, description: str, number: str = ""):
    return _decide(
        view,
        rule,
        comp,
        pin,
        decision,
        title=description,
        description=description,
        expected=[parameter],
        actual=[decision],
        missing=[parameter],
        number=number or (pin.number if pin is not None else ""),
    )


def _ok(view: CircuitView) -> CheckResult:
    if view.circuit.source == "pdf":
        return CheckResult(
            applicability="applicable",
            decision="needs_review",
            note="连接来自 PDF，不能记为确定正常。",
        )
    return CheckResult(applicability="applicable", decision="confirmed_ok")


def _verified_items(view: CircuitView, comp: Component, parameter: str, pin: str | None):
    items = [
        item
        for item in view.verified(comp, parameter, pin)
        if (getattr(item, "constraint_type", "") or "") in _HARD
    ]
    return items


def _verified_texts(view: CircuitView, comp: Component, parameter: str, pin: str | None) -> list[str]:
    found = []
    for item in _verified_items(view, comp, parameter, pin):
        text = (item.value_text or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def _pending(view: CircuitView, comp: Component, parameter: str, pin: str | None) -> bool:
    for item in view.circuit.constraints:
        if item.parameter != parameter or item.verified:
            continue
        if item.refdes and item.refdes != comp.refdes:
            continue
        if item.mpn and item.mpn != (comp.mpn or ""):
            continue
        if pin and item.pin and item.pin not in {pin, _pin_name(comp, pin)}:
            continue
        return True
    knowledge = view.knowledge
    if knowledge is None:
        return False
    reason = knowledge.block_reason(comp, pin, parameter)
    return bool(reason)


def _conditional(view: CircuitView, comp: Component, parameter: str, pin: str) -> bool:
    return _marked(view, comp, parameter, pin, "conditional") or _blocked_text(view, comp, pin, parameter, "适用条件")


def _marked(view: CircuitView, comp: Component, parameter: str, pin: str, kind: str) -> bool:
    for item in view.circuit.constraints:
        if item.parameter == parameter and item.constraint_type == kind and item.refdes == comp.refdes:
            if not item.pin or item.pin == pin:
                return True
    if kind == "recommended":
        return _blocked_text(view, comp, pin, parameter, "推荐")
    return False


def _blocked_text(view: CircuitView, comp: Component, pin: str, parameter: str, text: str) -> bool:
    knowledge = view.knowledge
    if knowledge is None:
        return False
    return text in (knowledge.block_reason(comp, pin, parameter) or "")


def _conflicted(view: CircuitView, comp: Component, parameter: str) -> bool:
    token = f"{comp.refdes}:{parameter}"
    if token in view.constraint_conflicts:
        return True
    knowledge = view.knowledge
    if knowledge is None:
        return False
    return any(parameter in item.reason or parameter in item.detail for item in knowledge.conflicts)


def _pins_of(view: CircuitView, comp: Component, parameter: str) -> list[str]:
    numbers = []
    for item in view.circuit.constraints:
        if item.parameter != parameter:
            continue
        if item.refdes and item.refdes != comp.refdes:
            continue
        if item.mpn and item.mpn != (comp.mpn or ""):
            continue
        if item.pin and item.pin not in numbers:
            numbers.append(item.pin)
    knowledge = view.knowledge
    if knowledge is not None:
        for bound in knowledge.bindings:
            if bound.refdes != comp.refdes or bound.engine.parameter != parameter:
                continue
            pin = bound.constraint.applicability.pin or bound.engine.pin
            if pin and pin not in numbers:
                numbers.append(pin)
        for held in knowledge.withheld:
            if held.refdes != comp.refdes:
                continue
            if held.constraint.parameters and held.constraint.parameters[0].name != parameter:
                continue
            pin = held.constraint.applicability.pin
            if pin and pin not in numbers:
                numbers.append(pin)
    return numbers


def _policy(text: str) -> tuple[str, list[str]] | None:
    if ":" not in text:
        return None
    kind, rest = text.split(":", 1)
    kind = kind.strip().lower()
    nets = [part.strip() for part in rest.replace("，", ",").split(",") if part.strip()]
    if kind not in {"join", "separate"} or len(nets) < 2:
        return None
    return kind, nets


def _typed(comp: Component, kinds: set[str]) -> Pin | None:
    for pin in comp.pins:
        if pin.pin_type in kinds and pin.net:
            return pin
    return None


def _find_pin(comp: Component, number: str) -> Pin | None:
    for pin in comp.pins:
        if pin.number == number or pin.name == number:
            return pin
    return None


def _pin_name(comp: Component, number: str) -> str:
    pin = _find_pin(comp, number)
    return pin.name if pin is not None and pin.name else number


def _trace(view: CircuitView, left: str, right: str) -> list[str]:
    if left == right:
        return [left]
    span = dc_span(view.circuit, left)
    if right in span.parent:
        return span.trace_to(right)
    root = view.short_root(left)
    return [left, right] if root and root == view.short_root(right) else [left, right]


def _same_voltage(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return False
    return compare("==", left, right)["result"] == "true"


def _different(left: float, right: float) -> bool:
    return compare("!=", left, right)["result"] == "true"
