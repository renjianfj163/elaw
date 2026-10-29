"""电源、GPIO 和电压域的确定性检查。"""

from __future__ import annotations

from app.schematic.engine.calculators import compare, thevenin_current
from app.schematic.engine.dc import CircuitView, is_ground, is_power, rail_voltage
from app.schematic.engine.dc import domain_voltage as domain_level
from app.schematic.engine.report import NOT_APPLICABLE, CheckResult, Rule, finish, make_finding
from app.schematic.engine.values import format_amps, format_ohms, format_volts
from app.schematic.engine.checks_common import (
    amps,
    assumptions_for,
    can_sink,
    exceeds,
    io_pins,
    pages,
    pin_label,
    rating,
    soften,
    volts,
)


def check_pwr_001(view: CircuitView, rule: Rule) -> CheckResult:
    return _overvoltage(view, rule, enable=False)


def check_pwr_002(view: CircuitView, rule: Rule) -> CheckResult:
    return _overvoltage(view, rule, enable=True)


def _overvoltage(view: CircuitView, rule: Rule, *, enable: bool) -> CheckResult:
    pins = list(io_pins(view, enable=enable))
    if not pins:
        return NOT_APPLICABLE
    findings = []
    conflicts: set[str] = set()
    for comp, pin in pins:
        if not pin.net:
            continue
        bias = view.bias(pin.net)
        label = pin_label(comp, pin)
        if bias.conflict and pin.net not in conflicts:
            conflicts.add(pin.net)
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 所在网络把不同电位短接",
                    status="confirmed",
                    severity="critical",
                    description=f"{pin.net} 经 0Ω 通路把不同电位连在一起。{bias.note}",
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    electrical=[f"网络 {pin.net}", f"元件 {'、'.join(bias.parts) or '无'}"],
                    assumptions=assumptions_for(bias),
                )
            )
            continue
        if not bias.determined:
            if bias.missing and bias.boundaries:
                findings.append(
                    _missing(
                        rule,
                        title=f"{label} 到电源的通路缺少阻值或电压",
                        description=(
                            f"{label} 能走到电源或地，但 {', '.join(bias.missing)} 的阻值或电源电压未知。"
                            "不能据此判断是否过压。"
                        ),
                        comp=comp,
                        pin_net=pin.net,
                        bias_parts=bias.parts,
                        boundaries=bias.boundaries,
                        missing=bias.missing,
                    )
                )
            continue
        if bias.voltage is None:
            continue
        decision = _voltage_decision(view, comp, pin)
        if decision.blocked:
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 的耐压约束不能用于确定结论",
                    status="needs_review" if _reviewable(decision.blocked) else "not_assessable",
                    severity="high",
                    description=(
                        f"{label} 经 {'、'.join(bias.parts) or '直接连接'} 能到达电源，"
                        f"开路电压约为 {format_volts(bias.voltage)}。{decision.blocked}"
                    ),
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    missing=["abs_max_v"],
                    assumptions=assumptions_for(bias),
                    verification_steps=["复核对应资料中的电压限值，或消除互相冲突的约束后再判定。"],
                )
            )
            continue
        voltage = volts(bias.voltage)
        limit, limit_name = decision.limit, decision.name
        domain = domain_level(view.circuit, pin.voltage_domain)
        path = "、".join(bias.parts) or "直接连接"
        electrical = [
            f"开路电压 {format_volts(voltage)}",
            f"戴维南电阻 {format_ohms(bias.rth) if bias.rth is not None else '未知'}",
            f"通路元件 {path}",
            f"边界网络 {', '.join(bias.boundaries) or '无'}",
        ]
        if limit is not None and exceeds(voltage, limit):
            status = "needs_review" if decision.disagree else soften("confirmed", bias)
            calculation = None
            assumptions = assumptions_for(bias)
            if decision.item is not None:
                calculation = _stress_calculation(bias.voltage, bias.rth, pin.vol_v, voltage, limit)
                if decision.disagree:
                    assumptions.append(decision.disagree)
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 的直流偏置高于{limit_name}",
                    status=status,
                    severity="critical" if status == "confirmed" else "high",
                    description=(
                        f"{label} 在高阻状态下的直流电压约为 {format_volts(voltage)}，"
                        f"高于{limit_name} {format_volts(limit)}。通路是 {path}。"
                        + (decision.disagree if decision.disagree else "")
                    ),
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    electrical=electrical,
                    device=[f"{comp.mpn or '型号未给出'} {limit_name} {format_volts(limit)}"],
                    constraints=[decision.item] if decision.item is not None else None,
                    calculation=calculation,
                    assumptions=assumptions,
                    verification_steps=["用网表核对电阻两端和 0Ω 跳线。", "核对引脚绝对最大额定值的手册版本和页码。"],
                )
            )
        elif limit is None and domain is not None and exceeds(voltage, domain):
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 的直流偏置高于所属电压域",
                    status="needs_review",
                    severity="high",
                    description=(
                        f"{label} 的开路电压约为 {format_volts(voltage)}，高于电压域 {format_volts(domain)}。"
                        "没有绝对最大额定值，不能认定器件损坏。"
                    ),
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    electrical=electrical,
                    missing=["pin.abs_max_v"],
                    assumptions=assumptions_for(bias),
                    verification_steps=["补齐该引脚的绝对最大额定值后再判定。"],
                )
            )
        elif limit is None and domain is None and any(is_power(view.circuit, net) for net in bias.boundaries):
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 接到了电源，但引脚耐压未知",
                    status="needs_review",
                    severity="high",
                    description=(
                        f"{label} 经 {path} 接到电源，开路电压约为 {format_volts(voltage)}。"
                        "没有电压域，也没有绝对最大额定值，不能认定器件损坏。"
                    ),
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    electrical=electrical,
                    missing=["pin.abs_max_v", "pin.voltage_domain"],
                    assumptions=assumptions_for(bias),
                )
            )
    return finish(findings)


def check_pwr_003(view: CircuitView, rule: Rule) -> CheckResult:
    pins = [(comp, pin) for comp, pin in io_pins(view, enable=False) if can_sink(pin)]
    pins += [(comp, pin) for comp, pin in io_pins(view, enable=True) if can_sink(pin)]
    if not pins:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in pins:
        if not pin.net:
            continue
        bias = view.bias(pin.net)
        if not bias.determined or bias.voltage is None or bias.rth is None:
            continue
        if bias.rth > 0 and bias.voltage <= (pin.vol_v or 0):
            continue
        calc = thevenin_current(bias.voltage, bias.rth, pin.vol_v)
        label = pin_label(comp, pin)
        if pin.iol_max_a is None:
            findings.append(
                _missing(
                    rule,
                    title=f"{label} 有上拉通路，但没有灌电流限额",
                    description=f"{label} 拉低时的电流无法和器件限额比较。",
                    comp=comp,
                    pin_net=pin.net,
                    bias_parts=bias.parts,
                    boundaries=bias.boundaries,
                    missing=["pin.iol_max_a"],
                    calculation=calc,
                )
            )
            continue
        if bias.rth == 0 and exceeds(bias.voltage, pin.vol_v or 0):
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 被低阻拉到电源",
                    status=soften("confirmed", bias),
                    severity="critical",
                    description=f"{label} 到电源的戴维南电阻为 0，拉低时没有串联电阻限流。",
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    calculation=calc,
                    electrical=[f"Rth=0", f"电压 {format_volts(bias.voltage)}"],
                    assumptions=assumptions_for(bias) + calc.assumptions,
                )
            )
            continue
        if calc.value is None:
            continue
        if exceeds(amps(calc.value), pin.iol_max_a):
            status = "needs_review" if pin.vol_v is None else soften("confirmed", bias)
            findings.append(
                make_finding(
                    rule,
                    title=f"{label} 拉低时的灌电流可能超过限额",
                    status=status,
                    severity="high",
                    description=(
                        f"{label} 按 I=(Vth-VOL)/Rth 得到 {format_amps(calc.value)}，"
                        f"器件灌电流限额为 {format_amps(pin.iol_max_a)}。"
                    ),
                    components=[comp.refdes, *bias.parts],
                    nets=[pin.net, *bias.boundaries],
                    pages=pages(view, [comp.refdes, *bias.parts]),
                    calculation=calc,
                    assumptions=assumptions_for(bias) + calc.assumptions,
                    verification_steps=["若 VOL 未给出，需用手册 VOL 重算后再确认。"],
                )
            )
    return finish(findings)


def check_pwr_004(view: CircuitView, rule: Rule) -> CheckResult:
    pins = list(io_pins(view, enable=False))
    if not pins:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in pins:
        if not pin.net or not comp.supply_net or comp.ioff is True:
            continue
        supply = rail_voltage(view.circuit, comp.supply_net)
        if supply is None:
            continue
        bias = view.bias(pin.net)
        if not bias.determined or bias.voltage is None:
            continue
        foreign = [
            net
            for net in bias.boundaries
            if is_power(view.circuit, net)
            and view.short_root(net) != view.short_root(comp.supply_net)
            and (rail_voltage(view.circuit, net) or 0) > supply + 1e-4
        ]
        if not foreign:
            continue
        proven = comp.ioff is False and comp.clamp_to_supply is True
        label = pin_label(comp, pin)
        findings.append(
            make_finding(
                rule,
                title=f"{label} 存在跨电源域的直流通路",
                status="probable" if proven else "needs_review",
                severity="high",
                description=(
                    f"{label} 的供电网络是 {comp.supply_net}（{format_volts(supply)}），"
                    f"引脚直流通路接到 {', '.join(foreign)}。"
                    + (
                        "器件标明没有 Ioff，且引脚会钳位到自身电源。"
                        if proven
                        else "静态网表不能证明掉电后一定反灌，需要 Ioff 或钳位说明。"
                    )
                ),
                components=[comp.refdes, *bias.parts],
                nets=[pin.net, comp.supply_net, *foreign],
                pages=pages(view, [comp.refdes, *bias.parts]),
                missing=[] if proven else ["ioff 或 clamp_to_supply"],
                assumptions=["未做上电时序仿真，本项只说明存在跨域直流通路。"],
                verification_steps=["核对未供电时的 Ioff、钳位二极管和允许的反向电流。"],
            )
        )
    return finish(findings)


def check_pwr_005(view: CircuitView, rule: Rule) -> CheckResult:
    pins = list(io_pins(view, enable=True))
    if not pins:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in pins:
        if not pin.net:
            findings.append(
                _missing(
                    rule,
                    title=f"{pin_label(comp, pin)} 没有网络",
                    description="使能脚没有进入网表，默认电平无法判定。",
                    comp=comp,
                    pin_net="",
                    bias_parts=[],
                    boundaries=[],
                    missing=["en_net"],
                )
            )
            continue
        bias = view.bias(pin.net)
        grounded = any(is_ground(view.circuit, net) for net in bias.boundaries)
        powered = any(is_power(view.circuit, net) for net in bias.boundaries)
        if bias.determined and (grounded or powered) and not bias.conflict:
            continue
        others = [
            other
            for other_comp, other in view.on_net.get(pin.net, [])
            if other_comp.refdes != comp.refdes and other.direction in {"output", "bidirectional"}
        ]
        findings.append(
            make_finding(
                rule,
                title=f"{pin_label(comp, pin)} 的上电默认电平不确定",
                status="not_assessable" if bias.missing else "needs_review",
                severity="medium",
                description=(
                    f"{pin_label(comp, pin)} 没有独立的上拉或下拉。"
                    + ("该网络还连着其他输出，复位期间的电平取决于驱动器。" if others else "静态连接不能确定使能默认值。")
                ),
                components=[comp.refdes, *[item.refdes for item, _pin in view.on_net.get(pin.net, [])]],
                nets=[pin.net],
                pages=pages(view, [comp.refdes]),
                missing=bias.missing,
                verification_steps=["核对使能脚推荐默认电平、外部电阻和驱动器复位态。"],
            )
        )
    return finish(findings)


def check_io_001(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    seen = False
    for net, pins in view.on_net.items():
        drivers = [(comp, pin) for comp, pin in pins if comp.kind == "ic" and pin.voh_v is not None]
        receivers = [(comp, pin) for comp, pin in pins if comp.kind == "ic" and pin.abs_max_v is not None]
        if not drivers or not receivers:
            continue
        seen = True
        for driver_comp, driver in drivers:
            for recv_comp, recv in receivers:
                if driver_comp.refdes == recv_comp.refdes and driver.number == recv.number:
                    continue
                if driver.voh_v is None or recv.abs_max_v is None:
                    continue
                if exceeds(volts(driver.voh_v), volts(recv.abs_max_v)):
                    findings.append(
                        make_finding(
                            rule,
                            title=f"{pin_label(driver_comp, driver)} 的高电平超过 {pin_label(recv_comp, recv)} 的额定值",
                            status="confirmed",
                            severity="high",
                            description=(
                                f"网络 {net} 上，{pin_label(driver_comp, driver)} 的 VOH 为 {format_volts(driver.voh_v)}，"
                                f"{pin_label(recv_comp, recv)} 的绝对最大值为 {format_volts(recv.abs_max_v)}。"
                            ),
                            components=[driver_comp.refdes, recv_comp.refdes],
                            nets=[net],
                            pages=pages(view, [driver_comp.refdes, recv_comp.refdes]),
                            device=[
                                f"{driver_comp.mpn or driver_comp.refdes} VOH {format_volts(driver.voh_v)}",
                                f"{recv_comp.mpn or recv_comp.refdes} 绝对最大 {format_volts(recv.abs_max_v)}",
                            ],
                        )
                    )
    if not seen and not findings:
        return NOT_APPLICABLE
    return finish(findings)


def check_io_002(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    seen = False
    for net, pins in view.on_net.items():
        labeled = []
        for comp, pin in pins:
            if comp.kind != "ic" or not pin.voltage_domain:
                continue
            level = domain_level(view.circuit, pin.voltage_domain)
            if level is None:
                continue
            labeled.append((comp, pin, level))
        if len({round(item[2], 6) for item in labeled}) < 2:
            continue
        seen = True
        high = max(item[2] for item in labeled)
        for comp, pin, level in labeled:
            if not exceeds(high, level):
                continue
            limit, limit_name = rating(pin)
            if limit is not None and exceeds(volts(high), volts(limit)):
                status, severity = "confirmed", "high"
                text = f"高于{limit_name} {format_volts(limit)}"
            elif limit is None:
                status, severity = "needs_review", "high"
                text = "没有绝对最大额定值，不能认定不兼容"
            else:
                continue
            findings.append(
                make_finding(
                    rule,
                    title=f"网络 {net} 上存在不同电压域的直接连接",
                    status=status,
                    severity=severity,
                    description=(
                        f"{pin_label(comp, pin)} 属于 {pin.voltage_domain}（{format_volts(level)}），"
                        f"同一网络上还有 {format_volts(high)} 的信号。{text}。"
                    ),
                    components=[comp.refdes],
                    nets=[net],
                    pages=pages(view, [comp.refdes]),
                    missing=[] if limit is not None else ["pin.abs_max_v"],
                    inferences=["同一网络名下的连接视为直接电气连接，没有再经过电平转换器。"],
                )
            )
    if not seen and not findings:
        return NOT_APPLICABLE
    return finish(findings)


def check_io_003(view: CircuitView, rule: Rule) -> CheckResult:
    pins = [
        (comp, pin)
        for comp, pin in io_pins(view, enable=False)
        if pin.output_kind == "open_drain" or pin.pin_type == "open_drain"
    ]
    if not pins:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in pins:
        if not pin.net:
            continue
        bias = view.bias(pin.net)
        powered = [net for net in bias.boundaries if is_power(view.circuit, net)]
        if bias.determined and powered and (bias.voltage or 0) > 0.2:
            continue
        if bias.missing:
            status = "not_assessable"
            description = f"{pin_label(comp, pin)} 的上拉通路缺少阻值或电源电压。"
        else:
            status = "confirmed" if view.circuit.connectivity_complete else "not_assessable"
            description = f"{pin_label(comp, pin)} 是开漏输出，网表里没有到电源的上拉。"
        findings.append(
            make_finding(
                rule,
                title=f"{pin_label(comp, pin)} 缺少有效上拉",
                status=status,
                severity="high" if status == "confirmed" else "info",
                description=description,
                components=[comp.refdes, *bias.parts],
                nets=[pin.net],
                pages=pages(view, [comp.refdes]),
                missing=bias.missing,
                assumptions=["这里只检查上拉是否存在，不把通用上拉阻值区间写成硬性限值。"],
            )
        )
    return finish(findings)


def check_io_004(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    seen = False
    for net, pins in view.on_net.items():
        drivers = []
        for comp, pin in pins:
            if comp.fitted is False or comp.kind != "ic":
                continue
            if pin.output_kind == "push_pull" and pin.direction in {"output", "bidirectional"}:
                drivers.append((comp, pin))
        if not drivers:
            continue
        seen = True
        if len(drivers) < 2:
            continue
        enables = {pin.enable_net for _comp, pin in drivers}
        exclusive = None not in enables and len(enables) > 1
        uncertain = any(comp.fitted is None for comp, _pin in drivers)
        names = "、".join(pin_label(comp, pin) for comp, pin in drivers)
        findings.append(
            make_finding(
                rule,
                title=f"网络 {net} 上有多个推挽输出",
                status="needs_review" if exclusive or uncertain else "confirmed",
                severity="high",
                description=(
                    f"{names} 都以推挽方式连到 {net}。"
                    + (
                        "有器件的装配状态未知，不能确认它们会同时在板上。"
                        if uncertain
                        else "它们的使能网络不同，是否会同时导通还要核对。"
                        if exclusive
                        else "网表里看不到互斥使能。"
                    )
                ),
                components=[comp.refdes for comp, _pin in drivers],
                nets=[net, *[pin.enable_net for _comp, pin in drivers if pin.enable_net]],
                pages=pages(view, [comp.refdes for comp, _pin in drivers]),
                verification_steps=["确认这些输出不会在同一时间处于驱动状态。"],
            )
        )
    if not seen:
        return NOT_APPLICABLE
    return finish(findings)


def _missing(rule, *, title, description, comp, pin_net, bias_parts, boundaries, missing, calculation=None):
    return make_finding(
        rule,
        title=title,
        status="not_assessable",
        severity="info",
        description=description,
        components=[comp.refdes, *bias_parts],
        nets=[net for net in [pin_net, *boundaries] if net],
        pages=pages_for(comp, bias_parts),
        missing=missing,
        calculation=calculation,
    )


def pages_for(comp, parts) -> list[int]:
    found = []
    if comp.page is not None:
        found.append(comp.page)
    return found


class _VoltageDecision:
    def __init__(self, limit, name, item=None, blocked="", disagree=""):
        self.limit = limit
        self.name = name
        self.item = item
        self.blocked = blocked
        self.disagree = disagree


def _voltage_decision(view, comp, pin) -> _VoltageDecision:
    pin_limit, pin_name = rating(pin)
    knowledge = view.knowledge
    if knowledge is None:
        return _VoltageDecision(pin_limit, pin_name)
    item = knowledge.primary(comp, pin.number, ("abs_max_v", "tolerant_v"))
    if item is not None:
        number = item.max_value if item.max_value is not None else item.value
        if number is None:
            return _VoltageDecision(None, "", blocked="已核验约束没有给出电压数值。")
        disagree = ""
        if pin_limit is not None and abs(pin_limit - number) > 1e-6:
            disagree = "原理图引脚上的耐压与已核验约束不一致。"
        return _VoltageDecision(number, "已核验设计约束", item, disagree=disagree)
    blocked = knowledge.block_reason(comp, pin.number, "abs_max_v") or knowledge.block_reason(
        comp, pin.number, "tolerant_v"
    )
    if blocked:
        return _VoltageDecision(None, "", blocked=blocked)
    return _VoltageDecision(pin_limit, pin_name)


def _reviewable(reason: str) -> bool:
    return any(token in reason for token in ("冲突", "尚未复核", "不一致", "适用条件", "推荐"))


def _stress_calculation(v_th, r_th, v_ol, voltage, limit):
    current = thevenin_current(v_th, r_th, v_ol)
    judged = compare(">", voltage, limit)
    current.formula = "Vbias > Vmax；" + current.formula
    current.inputs = {
        "Vbias": f"{voltage:.6g} V",
        "Vmax": f"{limit:.6g} V",
        **current.inputs,
    }
    current.evaluator_trace = judged["trace"]
    return current
