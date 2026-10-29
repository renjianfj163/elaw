"""电平转换、数字接口、复位、USB、装配和资料完整性。"""

from __future__ import annotations

from app.schematic.engine.checks_common import pages, pin_label
from app.schematic.engine.dc import Bypass, CircuitView, bypass_between, rail_voltage
from app.schematic.engine.model import Component, SignalGroup
from app.schematic.engine.report import NOT_APPLICABLE, CheckResult, Rule, finish, make_finding
from app.schematic.engine.values import format_ohms, format_volts
from app.schematic.engine.checks_common import cite, exceeds, volts


def check_ls_001(view: CircuitView, rule: Rule) -> CheckResult:
    devices = list(_shifters(view))
    if not devices:
        return NOT_APPLICABLE
    findings = []
    for comp in devices:
        for index, channel in enumerate(comp.channels, start=1):
            a_net = view.net_of(comp.refdes, channel.a_pin)
            b_net = view.net_of(comp.refdes, channel.b_pin)
            if not a_net or not b_net:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 通道 {index} 的引脚没有进入网表",
                        status="not_assessable",
                        severity="info",
                        description=f"{comp.refdes} 的 {channel.a_pin}/{channel.b_pin} 缺少网络，不能判断是否旁路。",
                        components=[comp.refdes],
                        missing=[f"{comp.refdes}.{channel.a_pin}", f"{comp.refdes}.{channel.b_pin}"],
                    )
                )
                continue
            unknown = [
                edge.refdes
                for edge in view.edges
                if edge.resistance is None and {edge.a, edge.b} == {a_net, b_net}
            ]
            if unknown:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 通道 {index} 两侧有阻值未知的跨接",
                        status="not_assessable",
                        severity="info",
                        description=f"{', '.join(unknown)} 跨接 {a_net} 与 {b_net}，阻值未知，不能判断旁路影响。",
                        components=[comp.refdes, *unknown],
                        nets=[a_net, b_net],
                        missing=unknown,
                    )
                )
            for path in bypass_between(view.circuit, view.edges, a_net, b_net):
                findings.append(_bypass_finding(view, rule, comp, index, path))
    return finish(findings)


def check_ls_002(view: CircuitView, rule: Rule) -> CheckResult:
    devices = list(_shifters(view))
    if not devices:
        return NOT_APPLICABLE
    findings = []
    for comp in devices:
        for kind, names in (("vcca", {"VCCA"}), ("vccb", {"VCCB"})):
            pin = _named(comp, kind, names)
            if pin is None or not pin.net:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 缺少 {kind.upper()} 引脚",
                        status="not_assessable",
                        severity="info",
                        description=f"网表没有给出 {comp.refdes} 的 {kind.upper()}，供电范围无法判定。",
                        components=[comp.refdes],
                        missing=[kind],
                    )
                )
                continue
            voltage = rail_voltage(view.circuit, pin.net)
            limits = view.verified(comp, kind)
            if voltage is None or not limits:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 的 {kind.upper()} 无法对照手册",
                        status="not_assessable",
                        severity="info",
                        description=(
                            f"{kind.upper()} 网络 {pin.net} 的电压或已核验供电范围缺失，不能判断是否匹配。"
                        ),
                        components=[comp.refdes],
                        nets=[pin.net],
                        missing=["rail_voltage" if voltage is None else f"verified {kind}"],
                    )
                )
                continue
            item = limits[0]
            if item.unit.lower() not in {"", "v", "volt", "volts"}:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 的 {kind.upper()} 单位无法换算",
                        status="not_assessable",
                        severity="info",
                        description="供电范围目前只接受伏特。",
                        components=[comp.refdes],
                        nets=[pin.net],
                        missing=[f"{kind}.unit"],
                    )
                )
                continue
            if item.min_value is None or item.max_value is None:
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 的 {kind.upper()} 约束不完整",
                        status="not_assessable",
                        severity="info",
                        description="已核验约束需要同时给出最小值和最大值。",
                        components=[comp.refdes],
                        nets=[pin.net],
                        datasheet_references=[cite(item)],
                        missing=[kind],
                    )
                )
                continue
            if exceeds(volts(voltage), volts(item.max_value)) or exceeds(volts(item.min_value), volts(voltage)):
                findings.append(
                    make_finding(
                        rule,
                        title=f"{comp.refdes} 的 {kind.upper()} 超出已核验范围",
                        status="confirmed",
                        severity="high",
                        description=(
                            f"{pin.net} 为 {format_volts(voltage)}，"
                            f"手册范围是 {format_volts(item.min_value)} 到 {format_volts(item.max_value)}。"
                        ),
                        components=[comp.refdes],
                        nets=[pin.net],
                        datasheet_references=[cite(item)],
                        device=[comp.mpn or ""],
                    )
                )
    return finish(findings)


def check_ls_003(view: CircuitView, rule: Rule) -> CheckResult:
    devices = list(_shifters(view))
    controls = []
    for comp in devices:
        for pin in comp.pins:
            if pin.pin_type in {"dir", "oe"} or pin.name.upper() in {"DIR", "OE"}:
                controls.append((comp, pin))
    if not controls:
        return NOT_APPLICABLE
    findings = []
    for comp, pin in controls:
        if _level_known(view, comp, pin):
            continue
        findings.append(
            make_finding(
                rule,
                title=f"{pin_label(comp, pin)} 的静态电平不确定",
                status="needs_review",
                severity="medium",
                description=(
                    f"{pin_label(comp, pin)} 没有接到电源、地或其他输出。"
                    "方向或输出使能是否正确，还要看设计意图，这里不能判成确定故障。"
                ),
                components=[comp.refdes],
                nets=[pin.net or ""],
                pages=pages(view, [comp.refdes]),
                verification_steps=["核对 DIR/OE 的有效电平，以及复位期间是否有确定偏置。"],
            )
        )
    return finish(findings)


def check_if_001(view: CircuitView, rule: Rule) -> CheckResult:
    return _same_role(view, rule, "sdio", {"d0", "d1", "d2", "d3"})


def check_if_002(view: CircuitView, rule: Rule) -> CheckResult:
    return _same_role(view, rule, "spi", {"mosi", "miso", "sclk", "cs"})


def check_if_003(view: CircuitView, rule: Rule) -> CheckResult:
    return _paired_role(view, rule, "uart", {"tx": "rx", "rx": "tx"})


def check_if_006(view: CircuitView, rule: Rule) -> CheckResult:
    findings = []
    groups = [group for group in view.circuit.signal_groups if group.kind in {"diff", "usb", "lvds"}]
    if not groups:
        return NOT_APPLICABLE
    for group in groups:
        roles = {"dp", "dm"} if group.kind == "usb" else {"p", "n"}
        result = _roles_of(view, group, roles)
        if result is None:
            findings.append(_endpoint_gap(rule, group))
            continue
        for host, got in result:
            expected = {host.role}
            if got == expected:
                continue
            swapped = got and got.isdisjoint(expected)
            if swapped and group.swap_allowed is True:
                continue
            if swapped and group.swap_allowed is None:
                status = "needs_review"
                text = "该接口没有标明是否允许交换极性，不能直接判成错误。"
            elif not got:
                status = "confirmed" if view.circuit.connectivity_complete else "not_assessable"
                text = "没有到达对端同名极性。"
            else:
                status = "confirmed"
                text = f"到达了 {', '.join(sorted(got))}。"
            findings.append(
                make_finding(
                    rule,
                    title=f"{group.name} 的 {host.role} 极性需要核对",
                    status=status,
                    severity="high" if status == "confirmed" else "medium",
                    description=f"{host.component}.{host.pin} 的 {host.role} {text}",
                    components=[host.component],
                    nets=[view.net_of(host.component, host.pin) or ""],
                )
            )
    return finish(findings)


def check_if_010(view: CircuitView, rule: Rule) -> CheckResult:
    return _same_role(view, rule, "mapped", None)


def check_if_004(view: CircuitView, rule: Rule) -> CheckResult:
    groups = [group for group in view.circuit.signal_groups if group.kind == "uart"]
    devices = list(_shifters(view))
    if not groups or not devices:
        return NOT_APPLICABLE
    findings = []
    for group in groups:
        for comp in devices:
            for index, channel in enumerate(comp.channels, start=1):
                a_net = view.net_of(comp.refdes, channel.a_pin)
                b_net = view.net_of(comp.refdes, channel.b_pin)
                if not a_net or not b_net:
                    continue
                if not _touches_group(view, a_net, group) or not _touches_group(view, b_net, group):
                    continue
                for path in bypass_between(view.circuit, view.edges, a_net, b_net):
                    findings.append(
                        _bypass_finding(
                            view,
                            rule,
                            comp,
                            index,
                            path,
                            title=f"{group.name} 绕过 {comp.refdes} 通道 {index}",
                        )
                    )
    return finish(findings)


def check_rst_004(view: CircuitView, rule: Rule) -> CheckResult:
    return _reset_path(view, rule, {"boot"}, "BOOT")


def check_rst_005(view: CircuitView, rule: Rule) -> CheckResult:
    return _reset_path(view, rule, {"nrst", "reset"}, "复位")


def check_usb_001(view: CircuitView, rule: Rule) -> CheckResult:
    phys = [comp for comp in view.circuit.components.values() if comp.role == "usb_phy" and comp.fitted is not False]
    if len(phys) < 2:
        return NOT_APPLICABLE if not phys else CheckResult(applicability="applicable")
    clusters: dict[tuple[str | None, str | None], list[Component]] = {}
    for comp in phys:
        dp, dm = _usb_pins(comp)
        key = (view.short_root(dp), view.short_root(dm))
        if key[0] is None or key[1] is None:
            continue
        clusters.setdefault(key, []).append(comp)
    findings = []
    for (dp, dm), group in clusters.items():
        if len(group) < 2:
            continue
        fitted = [comp.fitted for comp in group]
        exclusive = _exclusive(view, [comp.refdes for comp in group])
        if any(flag is None for flag in fitted):
            status = "needs_review"
            text = "有 PHY 的装配状态未知。若存在互斥的 0Ω 或 DNP，不能直接判成并联故障。"
        else:
            status = "confirmed"
            text = "这些 PHY 的 D+ 和 D− 落在同一对通过 0Ω 连通的网络上，网表里没有隔离。"
        if exclusive and status == "confirmed":
            text += "装配选项要求互斥，但当前标记为同时装配。"
        findings.append(
            make_finding(
                rule,
                title="同一对 USB 数据线连接了多个 PHY",
                status=status,
                severity="high",
                description=f"{'、'.join(comp.refdes for comp in group)}：{text}",
                components=[comp.refdes for comp in group],
                nets=[net for net in (dp, dm) if net],
                pages=pages(view, [comp.refdes for comp in group]),
                assumptions=["只把同一网络或 0Ω 合并视为并联；串联电阻不视为两个 PHY 直接并联。"],
                verification_steps=["核对 DNP、0Ω 装配选项和模拟开关是否保证同一时间只有一个 PHY 接入。"],
            )
        )
    return finish(findings)


def check_doc_001(view: CircuitView, rule: Rule) -> CheckResult:
    roles = {"mcu", "som", "module", "regulator", "level_shifter", "usb_phy", "phy", "opamp", "crystal"}
    targets = [
        comp
        for comp in view.circuit.components.values()
        if comp.fitted is not False and (comp.kind == "ic" or comp.role in roles)
    ]
    if not targets:
        return NOT_APPLICABLE
    findings = []
    for comp in targets:
        mpn = (comp.mpn or "").strip()
        if mpn and mpn.upper() not in {"TBD", "TODO", "?"}:
            continue
        findings.append(
            make_finding(
                rule,
                title=f"{comp.refdes} 缺少完整型号",
                status="confirmed",
                severity="info",
                description=f"{comp.refdes} 是关键器件，网表里没有可核对的完整型号。这是资料缺失，不是电路故障结论。",
                components=[comp.refdes],
                pages=pages(view, [comp.refdes]),
                missing=["mpn"],
            )
        )
    return finish(findings)


def check_sys_002(view: CircuitView, rule: Rule) -> CheckResult:
    options = [item for item in view.circuit.assembly if item.exclusive]
    if not options:
        return NOT_APPLICABLE
    findings = []
    for option in options:
        comps = []
        missing = []
        for refdes in option.refdes:
            comp = view.circuit.components.get(refdes)
            if comp is None:
                missing.append(refdes)
            else:
                comps.append(comp)
        if missing:
            findings.append(
                make_finding(
                    rule,
                    title=f"装配选项 {option.id} 指向了不存在的位号",
                    status="not_assessable",
                    severity="info",
                    description=f"找不到 {', '.join(missing)}。",
                    missing=missing,
                )
            )
            continue
        chosen = [comp for comp in comps if comp.fitted is True]
        unknown = [comp for comp in comps if comp.fitted is None]
        if len(chosen) > 1:
            findings.append(
                make_finding(
                    rule,
                    title=f"装配选项 {option.id} 有多个器件同时装配",
                    status="confirmed",
                    severity="high",
                    description=f"{'、'.join(comp.refdes for comp in chosen)} 被标为装配，但选项 {option.id} 要求互斥。",
                    components=[comp.refdes for comp in chosen],
                )
            )
        elif unknown:
            findings.append(
                make_finding(
                    rule,
                    title=f"装配选项 {option.id} 的装配状态不完整",
                    status="needs_review",
                    severity="medium",
                    description=f"{'、'.join(comp.refdes for comp in unknown)} 没有明确的装配或 DNP 状态。",
                    components=[comp.refdes for comp in unknown],
                    missing=[f"{comp.refdes}.fitted" for comp in unknown],
                )
            )
    return finish(findings)


def _same_role(view: CircuitView, rule: Rule, kind: str, roles: set[str] | None) -> CheckResult:
    groups = [group for group in view.circuit.signal_groups if group.kind == kind]
    if not groups:
        return NOT_APPLICABLE
    findings = []
    for group in groups:
        mapping = _roles_of(view, group, roles)
        if mapping is None:
            findings.append(_endpoint_gap(rule, group))
            continue
        for host, got in mapping:
            expected = {host.role}
            findings.extend(_mapping_finding(view, rule, group, host, got, expected))
    return finish(findings)


def _paired_role(view: CircuitView, rule: Rule, kind: str, pairs: dict[str, str]) -> CheckResult:
    groups = [group for group in view.circuit.signal_groups if group.kind == kind]
    if not groups:
        return NOT_APPLICABLE
    findings = []
    for group in groups:
        mapping = _roles_of(view, group, set(pairs))
        if mapping is None:
            findings.append(_endpoint_gap(rule, group))
            continue
        for host, got in mapping:
            expected = {pairs[host.role]}
            findings.extend(_mapping_finding(view, rule, group, host, got, expected))
    return finish(findings)


def _roles_of(view: CircuitView, group: SignalGroup, roles: set[str] | None):
    hosts = [
        item
        for item in group.endpoints
        if item.side == "host" and (roles is None or item.role in roles)
    ]
    devices = [
        item
        for item in group.endpoints
        if item.side == "device" and (roles is None or item.role in roles)
    ]
    if not hosts or not devices:
        return None
    mapping = []
    for host in hosts:
        net = view.net_of(host.component, host.pin)
        reach = view.reach(net)
        got = set()
        for device in devices:
            other = view.net_of(device.component, device.pin)
            if other and other in reach:
                got.add(device.role)
        mapping.append((host, got))
    return mapping


def _mapping_finding(view, rule, group, host, got, expected):
    if got == expected:
        return []
    net = view.net_of(host.component, host.pin) or ""
    if not got:
        status = "confirmed" if view.circuit.connectivity_complete else "not_assessable"
        title = f"{group.name} 的 {host.role} 没有到达对端"
        description = f"{host.component}.{host.pin} 没有连到对端的 {', '.join(sorted(expected))}。"
    elif got.isdisjoint(expected):
        status = "confirmed"
        title = f"{group.name} 的 {host.role} 映射错误"
        description = (
            f"{host.component}.{host.pin} 最终到达对端的 {', '.join(sorted(got))}，"
            f"按信号身份应当到达 {', '.join(sorted(expected))}。通道编号或符号上下位置不作为依据。"
        )
    else:
        status = "needs_review"
        title = f"{group.name} 的 {host.role} 到达了多个对端"
        description = f"同时到达 {', '.join(sorted(got))}。"
    if status == "confirmed" and _uncertain_path(view, net):
        status = "probable"
    constraints = []
    if view.knowledge is not None:
        constraints.extend(view.knowledge.cite(host.component, "pin_role", host.pin))
        for endpoint in group.endpoints:
            if endpoint.side == "device":
                constraints.extend(view.knowledge.cite(endpoint.component, "pin_role", endpoint.pin))
    return [
        make_finding(
            rule,
            title=title,
            status=status,
            severity="high" if status == "confirmed" else "medium",
            description=description,
            components=[host.component],
            nets=[net],
            pages=pages(view, [host.component]),
            inferences=["端到端映射穿过串联电阻和电平转换器通道，不按通道号的图形顺序判断。"],
            constraints=constraints,
        )
    ]


def _uncertain_path(view: CircuitView, net: str) -> bool:
    for name in view.reach(net):
        for comp, _pin in view.on_net.get(name, []):
            if comp.fitted is None:
                return True
    return False


def _endpoint_gap(rule: Rule, group: SignalGroup):
    return make_finding(
        rule,
        title=f"{group.name} 的端点不完整",
        status="not_assessable",
        severity="info",
        description="信号组需要同时给出 host 和 device 两侧端点。",
        missing=[f"{group.name}.endpoints"],
    )


def _bypass_finding(view, rule, comp, index, path: Bypass, title: str | None = None):
    status = "probable"
    severity = "high"
    if path.direct and not path.uncertain:
        status = "confirmed"
        severity = "critical"
    resistance = "0Ω 直连" if path.direct else (format_ohms(path.resistance) if path.resistance is not None else "未知电阻")
    constraints = []
    missing = []
    if view.knowledge is not None:
        constraints = view.knowledge.engine_constraints(comp, "no_resistive_bypass", None)
        if path.direct and status == "confirmed" and not constraints:
            blocked = view.knowledge.block_reason(comp, None, "no_resistive_bypass")
            if blocked or view.knowledge.active:
                status = "needs_review"
                severity = "high"
                missing = ["no_resistive_bypass"]
    return make_finding(
        rule,
        title=title or f"{comp.refdes} 通道 {index} 存在旁路",
        status=status,
        severity=severity,
        description=(
            f"{comp.refdes} 的两侧网络 {path.a} 与 {path.b} 另有无源通路（{resistance}，"
            f"元件 {'、'.join(path.refdes) or '短接'}）。"
            "阻性跨接不能据此认定转换器已经失效，也不能在未分析直流和瞬态时认定它仍然有效。"
        ),
        components=[comp.refdes, *path.refdes],
        nets=[path.a, path.b],
        pages=pages(view, [comp.refdes, *path.refdes]),
        electrical=[resistance, f"元件 {'、'.join(path.refdes) or '同一网络'}"],
        assumptions=["旁路搜索不穿过电源和地，因此两侧各自上拉到同一电源不算旁路。"],
        verification_steps=["按直流工作点和边沿速度分别核对这条跨接。", "对照电平转换器手册里是否允许通道两侧被电阻短接。"],
        constraints=constraints,
        missing=missing,
    )


def _reset_path(view: CircuitView, rule: Rule, pin_types: set[str], label: str) -> CheckResult:
    devices = list(_shifters(view))
    if not devices:
        return NOT_APPLICABLE
    findings = []
    applicable = False
    for comp in devices:
        oe = _named(comp, "oe", {"OE"})
        if oe is None or not oe.net:
            continue
        drivers = [
            other
            for other, pin in view.on_net.get(oe.net, [])
            if other.refdes != comp.refdes
            and other.role in {"mcu", "som"}
            and (pin.pin_type in {"gpio", "io", "signal"} or pin.direction in {"output", "bidirectional"})
        ]
        if not drivers:
            continue
        applicable = True
        for channel in comp.channels:
            for number in (channel.a_pin, channel.b_pin):
                net = view.net_of(comp.refdes, number)
                if not net:
                    continue
                for owner, pin in view.on_net.get(net, []):
                    if pin.pin_type not in pin_types:
                        continue
                    if label == "复位" and owner.refdes not in {item.refdes for item in drivers}:
                        continue
                    timing = []
                    missing = []
                    if view.knowledge is not None:
                        timing = view.knowledge.cite(owner.refdes, "boot_timing", None)
                        if not timing:
                            missing.append("boot_timing")
                    findings.append(
                        make_finding(
                            rule,
                            title=f"{comp.refdes} 的 OE 与 {label} 通路相关",
                            status="needs_review",
                            severity="high",
                            description=(
                                f"{pin_label(owner, pin)} 位于 {comp.refdes} 通道一侧，"
                                f"OE 由 {', '.join(item.refdes for item in drivers)} 驱动。"
                                "复位期间 MCU 可能失去对这条通路的控制。静态网表不能证明启动失败。"
                            ),
                            components=[comp.refdes, owner.refdes, *[item.refdes for item in drivers]],
                            nets=[net, oe.net],
                            pages=pages(view, [comp.refdes, owner.refdes]),
                            constraints=timing,
                            missing=missing,
                            verification_steps=[
                                "核对上电、复位保持、复位释放和再次掉电时 OE 的电平。",
                                "确认是否有不依赖该 MCU 的上拉或下拉使转换器保持有效。",
                                "按手册核对复位脉宽和启动时序；资料没有给出时序时，本项保持待核实。",
                            ],
                            assumptions=["没有时序模型，本项只标出需要核实的控制依赖。"],
                        )
                    )
    if not applicable and not findings:
        return NOT_APPLICABLE
    return finish(findings)


def _touches_group(view: CircuitView, net: str, group: SignalGroup) -> bool:
    reach = view.reach(net)
    for endpoint in group.endpoints:
        other = view.net_of(endpoint.component, endpoint.pin)
        if other and other in reach:
            return True
    return False


def _level_known(view: CircuitView, comp: Component, pin) -> bool:
    if not pin.net:
        return False
    for other, item in view.on_net.get(pin.net, []):
        if other.refdes == comp.refdes and item.number == pin.number:
            continue
        if item.direction in {"output", "bidirectional"} or item.pin_type in {"gpio", "io"}:
            return True
    bias = view.bias(pin.net)
    return bias.determined and not bias.conflict


def _shifters(view: CircuitView):
    for comp in view.circuit.components.values():
        if comp.fitted is not False and comp.channels:
            yield comp


def _named(comp: Component, pin_type: str, names: set[str]):
    for pin in comp.pins:
        if pin.pin_type == pin_type or pin.name.upper() in names:
            return pin
    return None


def _usb_pins(comp: Component) -> tuple[str | None, str | None]:
    dp = dm = None
    for pin in comp.pins:
        token = pin.pin_type.lower()
        name = pin.name.upper()
        if token in {"dp", "d+"} or name in {"DP", "D+"}:
            dp = pin.net
        if token in {"dm", "d-"} or name in {"DM", "D-"}:
            dm = pin.net
    return dp, dm


def _exclusive(view: CircuitView, refdes: list[str]) -> bool:
    wanted = set(refdes)
    return any(item.exclusive and wanted <= set(item.refdes) for item in view.circuit.assembly)
