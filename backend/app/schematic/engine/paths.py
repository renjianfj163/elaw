"""串联器件的通路模型。

电阻、0Ω、电感、磁珠和已声明的通道可以沿直流追踪。
串联电容和晶振不是直流通路。没有通道模型的器件不视为短路。
电平转换器、开关和集成电路不在这里被当成导线。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.schematic.engine.model import Circuit, Component
from app.schematic.engine.values import is_unfitted_value

DC_KINDS = {"resistor", "jumper", "inductor", "ferrite"}
NOT_DC_KINDS = {"capacitor", "crystal"}


@dataclass
class Span:
    start: str
    nets: list[str] = field(default_factory=list)
    hops: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    parent: dict[str, tuple[str, str, str] | None] = field(default_factory=dict)
    ac: list[tuple[str, str]] = field(default_factory=list)
    not_dc: list[tuple[str, str]] = field(default_factory=list)
    opaque: list[tuple[str, str]] = field(default_factory=list)

    def trace_to(self, target: str) -> list[str]:
        if target not in self.parent:
            return [self.start] if self.start else []
        steps: list[str] = []
        cursor = target
        while cursor:
            link = self.parent.get(cursor)
            if link is None:
                steps.append(cursor)
                break
            previous, refdes, _kind = link
            steps.append(f"{refdes} {previous}→{cursor}")
            cursor = previous
        steps.reverse()
        return steps


def dc_span(circuit: Circuit, start: str | None) -> Span:
    span = Span(start=start or "")
    if not start:
        return span
    hops = _dc_hops(circuit)
    adjacent: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for left, right, refdes, kind in hops:
        adjacent[left].append((right, refdes, kind))
        adjacent[right].append((left, refdes, kind))
    parent: dict[str, tuple[str, str, str] | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for nxt, refdes, kind in adjacent[current]:
            if nxt in parent:
                continue
            parent[nxt] = (current, refdes, kind)
            queue.append(nxt)
    span.parent = parent
    span.nets = list(parent)
    seen_parts: list[str] = []
    hop_lines: list[str] = []
    for net, link in parent.items():
        if link is None:
            continue
        previous, refdes, _kind = link
        line = f"{refdes} {previous}→{net}"
        hop_lines.append(line)
        if refdes not in seen_parts:
            seen_parts.append(refdes)
    span.hops = hop_lines
    span.parts = seen_parts
    inside = set(parent)
    span.ac, span.not_dc, span.opaque = _bridges(circuit, inside, hops)
    return span


def _dc_hops(circuit: Circuit) -> list[tuple[str, str, str, str]]:
    found: list[tuple[str, str, str, str]] = []
    for comp in circuit.components.values():
        if comp.fitted is False or is_unfitted_value(comp.value):
            continue
        if comp.kind not in DC_KINDS:
            continue
        if comp.channels:
            for channel in comp.channels:
                left = _net(comp, channel.a_pin)
                right = _net(comp, channel.b_pin)
                if left and right and left != right:
                    found.append((left, right, comp.refdes, comp.kind))
            continue
        nets = _nets(comp)
        if len(nets) == 2:
            found.append((nets[0], nets[1], comp.refdes, comp.kind))
    return found


def _bridges(
    circuit: Circuit,
    inside: set[str],
    hops: list[tuple[str, str, str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    hopped = {refdes for _left, _right, refdes, _kind in hops}
    ac: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []
    opaque: list[tuple[str, str]] = []
    for comp in circuit.components.values():
        if comp.fitted is False or is_unfitted_value(comp.value) or comp.refdes in hopped:
            continue
        if comp.kind == "ic":
            continue
        nets = _nets(comp)
        if len(nets) < 2:
            continue
        inbound = [net for net in nets if net in inside]
        outbound = [net for net in nets if net not in inside]
        if not inbound or not outbound:
            continue
        other = outbound[0]
        if comp.kind == "capacitor":
            ac.append((comp.refdes, other))
        elif comp.kind == "crystal":
            blocked.append((comp.refdes, other))
        elif comp.kind in DC_KINDS or comp.kind in {"unknown", "other"} or len(nets) == 2:
            opaque.append((comp.refdes, other))
    return ac, blocked, opaque


def _nets(comp: Component) -> list[str]:
    found: list[str] = []
    for pin in comp.pins:
        if pin.net and pin.net not in found:
            found.append(pin.net)
    return found


def _net(comp: Component, number: str) -> str | None:
    for pin in comp.pins:
        if pin.number == number:
            return pin.net
    return None
