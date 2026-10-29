"""反馈分压、输出电容和晶振负载。计算只使用已经给出的数。"""

from __future__ import annotations

import re

from app.schematic.engine.calculators import between, compare, crystal_load, feedback_vout, parallel_sum
from app.schematic.engine.checks_common import cite, pages
from app.schematic.engine.dc import CircuitView, is_ground
from app.schematic.engine.model import Component, Constraint
from app.schematic.engine.report import NOT_APPLICABLE, CheckResult, Rule, finish, make_finding
from app.schematic.engine.values import format_farads, format_ohms, format_volts, parse_capacitance, parse_resistance


def check_reg_001(view: CircuitView, rule: Rule) -> CheckResult:
    regulators = list(_regulators(view))
    if not regulators:
        return NOT_APPLICABLE
    findings = []
    for comp in regulators:
        feedback = _pin(comp, {"feedback"}, {"FB", "ADJ", "FEEDBACK"})
        if feedback is None or not feedback.net:
            findings.append(
                _gap(rule, comp, "找不到反馈脚", "调节器没有 FB/ADJ 网络，不能计算输出电压。", ["feedback"])
            )
            continue
        vout_net = _vout_net(view, comp)
        if vout_net is None:
            findings.append(_gap(rule, comp, "找不到输出网络", "没有 VOUT 引脚，也没有以该器件为源的电源域。", ["vout"]))
            continue
        tops = _between(view, vout_net, feedback.net, "resistor")
        bottoms = []
        for item in view.circuit.components.values():
            if item.kind != "resistor" or item.fitted is False:
                continue
            nets = _two_nets(item)
            if len(nets) != 2:
                continue
            roots = {view.short_root(net) for net in nets}
            if view.short_root(feedback.net) in roots and any(is_ground(view.circuit, net) for net in nets):
                bottoms.append(item)
        if len(tops) != 1 or len(bottoms) != 1:
            findings.append(
                _gap(
                    rule,
                    comp,
                    "反馈电阻网络不唯一",
                    f"上电阻 {len(tops)} 颗，下电阻 {len(bottoms)} 颗。电阻位置必须来自网表，不能按位号猜测。",
                    ["feedback_resistors"],
                )
            )
            continue
        r_top = parse_resistance(tops[0].value)
        r_bottom = parse_resistance(bottoms[0].value)
        vref_items = view.verified(comp, "vref")
        vref = _volts_of(vref_items[0]) if vref_items else None
        calc = feedback_vout(vref, r_top, r_bottom)
        refs = [cite(item) for item in vref_items]
        common = dict(
            components=[comp.refdes, tops[0].refdes, bottoms[0].refdes],
            nets=[vout_net, feedback.net],
            pages=pages(view, [comp.refdes, tops[0].refdes, bottoms[0].refdes]),
            calculation=calc,
            datasheet_references=refs,
            electrical=[
                f"上电阻 {tops[0].refdes}={tops[0].value}",
                f"下电阻 {bottoms[0].refdes}={bottoms[0].value}",
            ],
        )
        if vref is None or calc.value is None:
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 缺少已核验的反馈基准",
                    status="not_assessable",
                    severity="info",
                    description="没有已核验的 Vref，不计算一个看似精确的输出电压。",
                    missing=["vref"],
                    **common,
                )
            )
            continue
        target, target_note = _target(view, comp)
        if target is None:
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 已算出输出电压，但没有目标值",
                    status="not_assessable",
                    severity="info",
                    description=f"按网表电阻算出 {format_volts(calc.value)}。没有目标电压，不能判断对错。",
                    missing=["target_vout"],
                    **common,
                )
            )
            continue
        if target_note == "conflict":
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 的目标电压来源不一致",
                    status="needs_review",
                    severity="medium",
                    description="元件参数里的目标电压和电源域标称值不一致。",
                    **common,
                )
            )
            continue
        tolerance = comp.parameters.get("vout_tolerance_pct")
        if tolerance is None:
            same = compare("==", round(calc.value, 6), round(target, 6))["result"] == "true"
            if same:
                continue
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 的计算输出与目标不一致",
                    status="probable",
                    severity="high",
                    description=(
                        f"计算值 {format_volts(calc.value)}，目标 {format_volts(target)}。"
                        "没有给出允许偏差，所以不把偏差直接判成确定错误。"
                    ),
                    assumptions=["未提供允许偏差。"],
                    **common,
                )
            )
            continue
        low = target * (1 - tolerance / 100)
        high = target * (1 + tolerance / 100)
        judged = between(calc.value, low, high)
        calc.evaluator_trace = judged["trace"]
        if judged["result"] == "true":
            continue
        if judged["result"] != "false":
            findings.append(
                _gap(rule, comp, "输出电压比较无法完成", "规则引擎没有给出确定比较结果。", ["vout"])
            )
            continue
        findings.append(
            make_finding(
                rule,
                title=f"{comp.refdes} 的反馈分压偏离目标",
                status="confirmed",
                severity="high",
                description=(
                    f"Vout=Vref*(1+Rtop/Rbottom) 得到 {format_volts(calc.value)}，"
                    f"目标 {format_volts(target)}，允许 ±{tolerance:g}%。"
                ),
                assumptions=[f"目标来自{target_note}。"],
                **common,
            )
        )
    return finish(findings)


def check_reg_003(view: CircuitView, rule: Rule) -> CheckResult:
    regulators = list(_regulators(view))
    if not regulators:
        return NOT_APPLICABLE
    findings = []
    for comp in regulators:
        vout_net = _vout_net(view, comp)
        if vout_net is None:
            findings.append(_gap(rule, comp, "找不到输出网络", "没有输出网络，不能汇总输出电容。", ["vout"]))
            continue
        caps = _caps_to_ground(view, vout_net)
        limits = view.verified(comp, "cout_min")
        if not limits:
            reason = ""
            if view.knowledge is not None:
                reason = view.knowledge.block_reason(comp, None, "cout_min")
            if reason:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 的输出电容约束不能用于确定结论",
                        status="needs_review" if any(token in reason for token in ("冲突", "尚未复核", "推荐", "适用条件")) else "not_assessable",
                        severity="medium",
                        description=f"{comp.refdes} 的输出电容没有用已复核硬性限值完成判断。{reason}",
                        components=[comp.refdes],
                        nets=[vout_net],
                        missing=["cout_min"],
                        assumptions=["未复核、推荐值或互相冲突的电容要求不会被当成已经满足或已经违反。"],
                    )
                )
                continue
            findings.append(
                _gap(
                    rule,
                    comp,
                    f"{comp.refdes} 没有已核验的最小输出电容",
                    "不使用通用电容经验值代替具体型号的手册要求。",
                    ["cout_min"],
                )
            )
            continue
        minimum = _farads_of(limits[0])
        if minimum is None:
            findings.append(
                _gap(rule, comp, "最小输出电容的单位无法换算", "约束需要带 F、µF、nF 或 pF 单位。", ["cout_min.unit"])
            )
            continue
        if not caps or any(parse_capacitance(cap.value) is None for cap in caps):
            findings.append(
                _gap(
                    rule,
                    comp,
                    f"{comp.refdes} 的输出电容无法汇总",
                    "输出到地的电容缺失，或容值无法解析。不把缺失电容当成 0。",
                    ["output_capacitors"],
                )
            )
            continue
        optimistic = []
        derated = []
        incomplete = False
        for cap in caps:
            parsed = parse_capacitance(cap.value)
            if parsed is None:
                continue
            optimistic.append(parsed)
            factor = cap.parameters.get("bias_factor")
            if cap.tolerance_pct is None or factor is None:
                incomplete = True
            else:
                derated.append(parsed * (1 - cap.tolerance_pct / 100) * factor)
        total = parallel_sum(optimistic)
        optimistic_sum = sum(optimistic)
        short = compare("<", optimistic_sum, minimum)
        total.evaluator_trace = short["trace"]
        if short["result"] == "true":
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 的输出电容低于已核验下限",
                    status="confirmed",
                    severity="high",
                    description=(
                        f"标称并联值 {format_farads(optimistic_sum)}，"
                        f"已核验下限 {format_farads(minimum)}。即使还未计入降容也已经不足。"
                    ),
                    components=[comp.refdes, *[cap.refdes for cap in caps]],
                    nets=[vout_net],
                    pages=pages(view, [comp.refdes, *[cap.refdes for cap in caps]]),
                    calculation=total,
                    datasheet_references=[cite(limits[0])],
                    constraints=limits[:1],
                    assumptions=["比较使用标称值。直流偏压降容只会更小，所以不足的结论仍然成立。"],
                )
            )
            continue
        if incomplete or len(derated) != len(optimistic):
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 的有效输出电容还不能确认",
                    status="needs_review",
                    severity="medium",
                    description=(
                        f"标称并联值 {format_farads(optimistic_sum)} 不低于下限 {format_farads(minimum)}，"
                        "但容差或直流偏压降容没有给全，不能判为通过。"
                    ),
                    components=[comp.refdes, *[cap.refdes for cap in caps]],
                    nets=[vout_net],
                    calculation=total,
                    datasheet_references=[cite(limits[0])],
                    constraints=limits[:1],
                    missing=["tolerance_pct 或 bias_factor"],
                    assumptions=["未提供的降容系数不会被替换成默认值。"],
                )
            )
            continue
        effective = sum(derated)
        judged = compare(">=", effective, minimum)
        if judged["result"] == "true":
            continue
        findings.append(
            make_finding(
                rule,
                title=f"{comp.refdes} 的有效输出电容低于下限",
                status="confirmed",
                severity="high",
                description=(
                    f"计入容差和直流偏压降容后为 {format_farads(effective)}，"
                    f"下限为 {format_farads(minimum)}。"
                ),
                components=[comp.refdes, *[cap.refdes for cap in caps]],
                nets=[vout_net],
                calculation=parallel_sum(derated),
                datasheet_references=[cite(limits[0])],
                constraints=limits[:1],
                assumptions=["有效值 = 标称值 × (1-容差) × 直流偏压系数。布局和 ESR 不在本项判断。"],
            )
        )
    return finish(findings)


def check_ana_001(view: CircuitView, rule: Rule) -> CheckResult:
    crystals = [
        comp
        for comp in view.circuit.components.values()
        if comp.fitted is not False and (comp.kind == "crystal" or comp.role == "crystal")
    ]
    if not crystals:
        return NOT_APPLICABLE
    findings = []
    for comp in crystals:
        sides = _crystal_sides(view, comp)
        limits = view.verified(comp, "cl")
        if sides is None:
            findings.append(
                _gap(rule, comp, f"{comp.refdes} 的负载电容拓扑不完整", "Pierce 近似需要两侧各自有到地的电容。", ["load_capacitors"])
            )
            continue
        c1, c2 = sides
        stray, stray_missing = _stray(view, comp)
        cl = _farads_of(limits[0]) if limits else None
        calc = crystal_load(c1, c2, stray)
        if cl is None:
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 缺少已核验的负载电容规格",
                    status="not_assessable",
                    severity="info",
                    description="没有晶振 CL，不把计算结果当成合格或不合格。",
                    components=[comp.refdes],
                    calculation=calc,
                    missing=["cl"],
                )
            )
            continue
        if stray_missing or calc.missing:
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 的寄生电容未知",
                    status="needs_review",
                    severity="medium",
                    description=(
                        f"两侧电容的串联部分为 {format_farads(calc.value or 0)}。"
                        "缺少寄生电容，这个数不是完整负载电容。"
                    ),
                    components=[comp.refdes],
                    calculation=calc,
                    datasheet_references=[cite(limits[0])],
                    missing=calc.missing,
                    assumptions=calc.assumptions,
                )
            )
            continue
        item = limits[0]
        low, high = _band(item, cl)
        judged = between(calc.value, low, high)
        calc.evaluator_trace = judged["trace"]
        if judged["result"] == "true":
            continue
        status = "confirmed" if judged["result"] == "false" and (item.min_value is not None or item.tolerance_pct is not None) else "probable"
        if item.min_value is None and item.max_value is None and item.tolerance_pct is None:
            status = "probable"
        findings.append(
            make_finding(
                rule,
                title=f"{comp.refdes} 的负载电容与规格不一致",
                status=status,
                severity="medium",
                description=(
                    f"CL≈C1*C2/(C1+C2)+Cstray 得到 {format_farads(calc.value or 0)}，"
                    f"规格为 {format_farads(cl)}。"
                ),
                components=[comp.refdes],
                calculation=calc,
                datasheet_references=[cite(item)],
                assumptions=calc.assumptions,
            )
        )
    return finish(findings)


def check_ana_002(view: CircuitView, rule: Rule) -> CheckResult:
    crystals = [
        comp
        for comp in view.circuit.components.values()
        if comp.fitted is not False and (comp.kind == "crystal" or comp.role == "crystal")
    ]
    caps = [comp for comp in view.circuit.components.values() if comp.kind == "capacitor" and comp.fitted is not False]
    if not crystals and not any(comp.comment for comp in caps):
        return NOT_APPLICABLE
    findings = []
    for cap in caps:
        commented = _picofarads(cap.comment)
        placed = parse_capacitance(cap.value)
        if commented is None or placed is None:
            continue
        if compare("==", round(commented, 15), round(placed, 15))["result"] == "true":
            continue
        findings.append(
            make_finding(
                rule,
                title=f"{cap.refdes} 的注释和容值不一致",
                status="confirmed",
                severity="low",
                description=(
                    f"注释中的电容为 {format_farads(commented)}，元件值为 {format_farads(placed)}。"
                    "这是标注矛盾，不能单独证明振荡电路一定不起振。"
                ),
                components=[cap.refdes],
                pages=pages(view, [cap.refdes]),
            )
        )
    for comp in crystals:
        match = re.search(r"两侧\s*([0-9]*\.?[0-9]+)\s*pf", comp.comment, re.IGNORECASE)
        if not match or comp.kind == "capacitor":
            continue
        expected = float(match.group(1)) * 1e-12
        for side in _side_caps(view, comp):
            placed = parse_capacitance(side.value)
            if placed is None:
                continue
            if compare("==", round(expected, 15), round(placed, 15))["result"] == "true":
                continue
            findings.append(
                make_finding(
                    rule,
                    title=f"{comp.refdes} 的两侧电容注释和 {side.refdes} 不一致",
                    status="confirmed",
                    severity="low",
                    description=f"注释写两侧 {match.group(1)} pF，{side.refdes} 的值是 {side.value}。",
                    components=[comp.refdes, side.refdes],
                )
            )
    if not crystals and not findings:
        return NOT_APPLICABLE
    return finish(findings)


def _regulators(view: CircuitView):
    for comp in view.circuit.components.values():
        if comp.fitted is False or comp.kind != "ic":
            continue
        if comp.role == "regulator" or _pin(comp, {"feedback"}, {"FB", "ADJ", "FEEDBACK"}) is not None:
            yield comp


def _pin(comp: Component, types: set[str], names: set[str]):
    for pin in comp.pins:
        if pin.pin_type in types or pin.name.upper() in names:
            return pin
    return None


def _vout_net(view: CircuitView, comp: Component) -> str | None:
    pin = _pin(comp, {"vout"}, {"VOUT", "OUT", "VO"})
    if pin and pin.net:
        return pin.net
    for domain in view.circuit.power_domains:
        if domain.source == comp.refdes:
            return domain.net
    return None


def _between(view: CircuitView, left: str, right: str, kind: str) -> list[Component]:
    found = []
    for comp in view.circuit.components.values():
        if comp.kind != kind or comp.fitted is False:
            continue
        nets = _two_nets(comp)
        if len(nets) != 2:
            continue
        if {view.short_root(nets[0]), view.short_root(nets[1])} == {view.short_root(left), view.short_root(right)}:
            found.append(comp)
    return found


def _two_nets(comp: Component) -> list[str]:
    nets = []
    for pin in comp.pins:
        if pin.net and pin.net not in nets:
            nets.append(pin.net)
    return nets


def _caps_to_ground(view: CircuitView, net: str) -> list[Component]:
    found = []
    for comp in view.circuit.components.values():
        if comp.kind != "capacitor" or comp.fitted is False:
            continue
        nets = _two_nets(comp)
        if len(nets) != 2:
            continue
        roots = {view.short_root(item) for item in nets}
        if view.short_root(net) in roots and any(is_ground(view.circuit, item) for item in nets):
            found.append(comp)
    return found


def _target(view: CircuitView, comp: Component) -> tuple[float | None, str]:
    param = comp.parameters.get("target_vout")
    domain_value = None
    for domain in view.circuit.power_domains:
        if domain.source == comp.refdes and domain.nominal_v is not None:
            domain_value = domain.nominal_v
    if param is not None and domain_value is not None and abs(param - domain_value) > 1e-6:
        return None, "conflict"
    if param is not None:
        return param, "元件参数"
    if domain_value is not None:
        return domain_value, "电源域标称值"
    return None, ""


def _volts_of(item: Constraint) -> float | None:
    if item.value is None:
        return None
    unit = item.unit.lower()
    if unit in {"", "v", "volt", "volts"}:
        return item.value if unit else item.value
    return None


def _farads_of(item: Constraint) -> float | None:
    number = item.min_value if item.parameter == "cout_min" else item.value
    if item.parameter == "cout_min" and number is None:
        number = item.value
    if number is None:
        return None
    unit = item.unit.lower().replace("μ", "u").replace("µ", "u")
    factor = {"": None, "f": 1.0, "uf": 1e-6, "nf": 1e-9, "pf": 1e-12, "mf": 1e-3}.get(unit)
    if factor is None:
        return None
    return number * factor


def _band(item: Constraint, center: float) -> tuple[float, float]:
    if item.min_value is not None and item.max_value is not None:
        return _scaled(item, item.min_value), _scaled(item, item.max_value)
    if item.tolerance_pct is not None:
        return center * (1 - item.tolerance_pct / 100), center * (1 + item.tolerance_pct / 100)
    return center, center


def _scaled(item: Constraint, number: float) -> float:
    unit = item.unit.lower().replace("μ", "u").replace("µ", "u")
    factor = {"f": 1.0, "uf": 1e-6, "nf": 1e-9, "pf": 1e-12, "mf": 1e-3}.get(unit, 1.0)
    return number * factor


def _stray(view: CircuitView, comp: Component) -> tuple[float | None, bool]:
    items = view.verified(comp, "c_stray")
    param = comp.parameters.get("c_stray_pf")
    verified = _farads_of(items[0]) if items else None
    from_param = None if param is None else param * 1e-12
    if verified is not None and from_param is not None and abs(verified - from_param) > 1e-15:
        return None, True
    if verified is not None:
        return verified, False
    if from_param is not None:
        return from_param, False
    return None, True


def _crystal_sides(view: CircuitView, comp: Component) -> tuple[float, float] | None:
    groups = []
    for pin in comp.pins:
        if not pin.net or is_ground(view.circuit, pin.net):
            continue
        caps = _caps_to_ground(view, pin.net)
        values = [parse_capacitance(cap.value) for cap in caps]
        if not caps or any(value is None for value in values):
            return None
        groups.append(sum(value for value in values if value is not None))
    if len(groups) != 2:
        return None
    return groups[0], groups[1]


def _side_caps(view: CircuitView, comp: Component) -> list[Component]:
    found = []
    for pin in comp.pins:
        if pin.net and not is_ground(view.circuit, pin.net):
            found.extend(_caps_to_ground(view, pin.net))
    return found


def _picofarads(text: str) -> float | None:
    match = re.search(r"([0-9]*\.?[0-9]+)\s*pf", text or "", re.IGNORECASE)
    if not match:
        return None
    return float(match.group(1)) * 1e-12


def _gap(rule: Rule, comp: Component, title: str, description: str, missing: list[str]):
    return make_finding(
        rule,
        title=title,
        status="not_assessable",
        severity="info",
        description=description,
        components=[comp.refdes],
        pages=[comp.page] if comp.page is not None else [],
        missing=missing,
    )
