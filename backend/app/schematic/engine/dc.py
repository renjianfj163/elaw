"""无源网络的直流通路和节点电压。

电源和地是边界，不会被当成可以随便穿过去的信号节点。
电阻值缺失时不估算电压。
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.schematic.engine.model import Circuit, Component, Pin
from app.schematic.engine.values import is_unfitted_value, parse_resistance


@dataclass(frozen=True)
class Edge:
    a: str
    b: str
    refdes: str
    resistance: float | None
    direct: bool
    uncertain: bool
    assumed_short: bool


@dataclass
class Bias:
    voltage: float | None = None
    rth: float | None = None
    determined: bool = False
    missing: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    uncertain: bool = False
    assumed_short: bool = False
    conflict: bool = False
    note: str = ""


@dataclass(frozen=True)
class Bypass:
    a: str
    b: str
    refdes: tuple[str, ...]
    resistance: float | None
    direct: bool
    uncertain: bool


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, name: str) -> None:
        self.parent.setdefault(name, name)

    def find(self, name: str) -> str:
        self.add(name)
        root = name
        while self.parent[root] != root:
            root = self.parent[root]
        while name != root:
            nxt = self.parent[name]
            self.parent[name] = root
            name = nxt
        return root

    def union(self, left: str, right: str) -> None:
        lr, rr = self.find(left), self.find(right)
        if lr != rr:
            self.parent[rr] = lr


def passive_edges(circuit: Circuit) -> list[Edge]:
    edges: list[Edge] = []
    for comp in circuit.components.values():
        if comp.fitted is False or is_unfitted_value(comp.value):
            continue
        if comp.kind not in {"resistor", "jumper", "ferrite", "inductor"}:
            continue
        nets: list[str] = []
        for pin in comp.pins:
            if pin.net and pin.net not in nets:
                nets.append(pin.net)
        if len(nets) != 2:
            continue
        uncertain = comp.fitted is None
        if comp.kind == "resistor":
            resistance = parse_resistance(comp.value)
            edges.append(Edge(nets[0], nets[1], comp.refdes, resistance, resistance == 0, uncertain, False))
        elif comp.kind == "jumper":
            resistance = parse_resistance(comp.value)
            if resistance is None:
                resistance = 0.0
            edges.append(Edge(nets[0], nets[1], comp.refdes, resistance, resistance == 0, uncertain, False))
        else:
            edges.append(Edge(nets[0], nets[1], comp.refdes, 0.0, True, True, True))
    return edges


def is_ground(circuit: Circuit, net: str) -> bool:
    item = circuit.nets.get(net)
    return bool(item and item.is_ground)


def is_power(circuit: Circuit, net: str) -> bool:
    item = circuit.nets.get(net)
    if item is None or item.is_ground:
        return False
    if item.is_power:
        return True
    return any(domain.net == net and (domain.nominal_v or 0) > 0 for domain in circuit.power_domains)


def rail_voltage(circuit: Circuit, net: str) -> float | None:
    """应力用电压上限；只有标称值时用标称值。"""
    item = circuit.nets.get(net)
    if item is None:
        return None
    if item.is_ground:
        return 0.0
    if item.max_v is not None:
        return item.max_v
    return item.nominal_v


def domain_voltage(circuit: Circuit, name: str | None) -> float | None:
    if not name:
        return None
    for domain in circuit.power_domains:
        if domain.name == name or domain.net == name:
            if domain.max_v is not None:
                return domain.max_v
            return domain.nominal_v
    item = circuit.nets.get(name)
    if item is None:
        return None
    if item.max_v is not None:
        return item.max_v
    return item.nominal_v


def bias_at(circuit: Circuit, edges: list[Edge], start: str) -> Bias:
    groups = _Union()
    groups.add(start)
    for edge in edges:
        groups.add(edge.a)
        groups.add(edge.b)
        if edge.resistance == 0:
            groups.union(edge.a, edge.b)
    members: dict[str, list[str]] = defaultdict(list)
    for net in list(groups.parent):
        members[groups.find(net)].append(net)

    def fixed(root: str) -> tuple[str, float | None]:
        kinds: list[str] = []
        volts: list[float] = []
        for net in members[root]:
            if is_ground(circuit, net):
                kinds.append("ground")
                volts.append(0.0)
            elif is_power(circuit, net):
                voltage = rail_voltage(circuit, net)
                if voltage is None:
                    kinds.append("unknown")
                else:
                    kinds.append("power")
                    volts.append(voltage)
        if "ground" in kinds and "power" in kinds:
            return "conflict", None
        if len({round(item, 9) for item in volts}) > 1:
            return "conflict", None
        if "unknown" in kinds and "power" not in kinds and "ground" not in kinds:
            return "unknown", None
        if "ground" in kinds:
            return "ground", 0.0
        if "power" in kinds:
            return "power", volts[0]
        return "free", None

    start_root = groups.find(start)
    kind, voltage = fixed(start_root)
    shorts = [edge.refdes for edge in edges if edge.resistance == 0 and groups.find(edge.a) == start_root]
    if kind == "conflict":
        return Bias(conflict=True, parts=shorts, note="该网络经 0Ω 把不同电位连在一起")
    if kind in {"power", "ground"} and voltage is not None:
        return Bias(
            voltage=voltage,
            rth=0.0,
            determined=True,
            boundaries=list(members[start_root]),
            parts=shorts,
            uncertain=any(edge.uncertain for edge in edges if edge.refdes in shorts),
            assumed_short=any(edge.assumed_short for edge in edges if edge.refdes in shorts),
        )

    adjacent: dict[str, list[tuple[str, Edge]]] = defaultdict(list)
    for edge in edges:
        if edge.resistance == 0:
            continue
        left, right = groups.find(edge.a), groups.find(edge.b)
        if left == right:
            continue
        adjacent[left].append((right, edge))
        adjacent[right].append((left, edge))

    seen = {start_root}
    queue: deque[str] = deque([start_root])
    used: list[Edge] = []
    unknown: list[Edge] = []
    fixed_roots: dict[str, float | None] = {}
    while queue:
        current = queue.popleft()
        for nxt, edge in adjacent[current]:
            used.append(edge)
            if edge.resistance is None:
                unknown.append(edge)
                other_kind, _other_voltage = fixed(nxt)
                if other_kind in {"power", "ground", "unknown", "conflict"}:
                    fixed_roots[nxt] = None
                continue
            other_kind, other_voltage = fixed(nxt)
            if other_kind == "conflict":
                return Bias(conflict=True, parts=[edge.refdes], note="通路上的 0Ω 把不同电位连在一起")
            if other_kind == "unknown":
                unknown.append(edge)
                fixed_roots[nxt] = None
                continue
            if other_kind in {"power", "ground"}:
                fixed_roots[nxt] = other_voltage
                continue
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)

    uncertain = any(edge.uncertain or edge.assumed_short for edge in used)
    assumed = any(edge.assumed_short for edge in used)
    parts = _parts(edges, groups, seen, fixed_roots, used)
    boundaries = [net for root in fixed_roots for net in members[root]]
    if unknown:
        return Bias(
            determined=False,
            missing=[edge.refdes for edge in unknown],
            boundaries=boundaries,
            parts=parts,
            uncertain=uncertain,
            assumed_short=assumed,
            note="通路上有阻值未知的元件或电压未知的电源",
        )
    if not fixed_roots:
        return Bias(determined=False, parts=parts, uncertain=uncertain, note="没有到达电源或地")
    unknown_nodes = [root for root in seen if root not in fixed_roots]
    index = {root: pos for pos, root in enumerate(unknown_nodes)}
    size = len(unknown_nodes)
    matrix = [[0.0] * size for _ in range(size)]
    current = [0.0] * size
    for edge in edges:
        if edge.resistance is None or edge.resistance == 0:
            continue
        left, right = groups.find(edge.a), groups.find(edge.b)
        if left == right:
            continue
        if left not in index and left not in fixed_roots:
            continue
        if right not in index and right not in fixed_roots:
            continue
        _stamp(matrix, current, index, fixed_roots, left, right, 1.0 / edge.resistance)
    solved = _solve(matrix, current)
    if solved is None or start_root not in index:
        return Bias(determined=False, parts=parts, boundaries=boundaries, note="电阻网络没有唯一解")
    stimulus = [0.0] * size
    stimulus[index[start_root]] = 1.0
    resistance_solve = _solve(matrix, stimulus)
    rth = None if resistance_solve is None else resistance_solve[index[start_root]]
    return Bias(
        voltage=solved[index[start_root]],
        rth=rth,
        determined=True,
        boundaries=boundaries,
        parts=parts,
        uncertain=uncertain,
        assumed_short=assumed,
    )


def bypass_between(circuit: Circuit, edges: list[Edge], left: str, right: str) -> list[Bypass]:
    """寻找不穿过电源、地和有源器件的无源旁路。"""
    if not left or not right:
        return []
    if left == right:
        return [Bypass(left, right, (), 0.0, True, False)]
    groups = _Union()
    for edge in edges:
        if edge.resistance == 0 and not _blocked(circuit, edge.a) and not _blocked(circuit, edge.b):
            groups.union(edge.a, edge.b)
    if groups.find(left) == groups.find(right):
        parts = tuple(
            edge.refdes
            for edge in edges
            if edge.resistance == 0 and groups.find(edge.a) == groups.find(left)
        )
        uncertain = any(edge.uncertain for edge in edges if edge.refdes in parts)
        return [Bypass(left, right, parts, 0.0, True, uncertain)]

    adjacent: dict[str, list[tuple[str, Edge]]] = defaultdict(list)
    for edge in edges:
        if edge.resistance in {None, 0}:
            continue
        if _blocked(circuit, edge.a) or _blocked(circuit, edge.b):
            continue
        adjacent[groups.find(edge.a)].append((groups.find(edge.b), edge))
        adjacent[groups.find(edge.b)].append((groups.find(edge.a), edge))
    start, goal = groups.find(left), groups.find(right)
    queue: deque[tuple[str, tuple[str, ...], float, bool]] = deque([(start, (), 0.0, False)])
    seen = {start}
    found: list[Bypass] = []
    while queue and len(found) < 4:
        current, parts, resistance, uncertain = queue.popleft()
        for nxt, edge in adjacent[current]:
            if edge.refdes in parts:
                continue
            nparts = parts + (edge.refdes,)
            nres = resistance + (edge.resistance or 0)
            nunc = uncertain or edge.uncertain
            if nxt == goal:
                found.append(Bypass(left, right, nparts, nres, False, nunc))
                continue
            if len(nparts) >= 4 or nxt in seen:
                continue
            seen.add(nxt)
            queue.append((nxt, nparts, nres, nunc))
    return found


def logic_adjacency(circuit: Circuit, edges: list[Edge]) -> dict[str, set[str]]:
    adjacent: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.resistance is None:
            continue
        if _blocked(circuit, edge.a) or _blocked(circuit, edge.b):
            continue
        adjacent[edge.a].add(edge.b)
        adjacent[edge.b].add(edge.a)
    for comp in circuit.components.values():
        if comp.fitted is False or comp.role in {"mux", "analog_switch", "isolator"}:
            continue
        for channel in comp.channels:
            a_net = _pin_net(comp, channel.a_pin)
            b_net = _pin_net(comp, channel.b_pin)
            if a_net and b_net:
                adjacent[a_net].add(b_net)
                adjacent[b_net].add(a_net)
    return adjacent


def reachable_nets(adjacent: dict[str, set[str]], start: str | None) -> set[str]:
    if not start:
        return set()
    seen: set[str] = set()
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(adjacent[current] - seen)
    return seen


def _blocked(circuit: Circuit, net: str) -> bool:
    return is_ground(circuit, net) or is_power(circuit, net)


class CircuitView:
    """一次审查里复用的网表索引。"""

    def __init__(self, circuit: Circuit, knowledge=None):
        self.circuit = circuit
        self.knowledge = knowledge
        self.constraint_conflicts: list[str] = []
        self.edges = passive_edges(circuit)
        self.adjacent = logic_adjacency(circuit, self.edges)
        self.on_net: dict[str, list[tuple[Component, Pin]]] = defaultdict(list)
        self.pin_net: dict[tuple[str, str], str] = {}
        self._bias: dict[str, Bias] = {}
        self._shorts = _Union()
        for comp in circuit.components.values():
            for pin in comp.pins:
                if not pin.net:
                    continue
                self.on_net[pin.net].append((comp, pin))
                self.pin_net[(comp.refdes, pin.number)] = pin.net
                self._shorts.add(pin.net)
        for edge in self.edges:
            self._shorts.add(edge.a)
            self._shorts.add(edge.b)
            if edge.resistance == 0:
                self._shorts.union(edge.a, edge.b)

    def bias(self, net: str | None) -> Bias:
        if not net:
            return Bias(note="引脚没有网络")
        if net not in self._bias:
            self._bias[net] = bias_at(self.circuit, self.edges, net)
        return self._bias[net]

    def net_of(self, refdes: str, number: str) -> str | None:
        return self.pin_net.get((refdes, number))

    def reach(self, net: str | None) -> set[str]:
        return reachable_nets(self.adjacent, net)

    def short_root(self, net: str | None) -> str | None:
        if not net:
            return None
        return self._shorts.find(net)

    def verified(self, comp: Component, parameter: str, pin: str | None = None):
        found = []
        for item in self.circuit.constraints:
            if item.parameter != parameter or not item.verified:
                continue
            if item.refdes and item.refdes != comp.refdes:
                continue
            if item.mpn and item.mpn != (comp.mpn or ""):
                continue
            if not item.refdes and not item.mpn:
                continue
            if pin is not None and item.pin and item.pin not in _pin_names(comp, pin):
                continue
            found.append(item)
        if self.knowledge is not None:
            found.extend(self.knowledge.engine_constraints(comp, parameter, pin))
        if _signatures_differ(found):
            self.constraint_conflicts.append(f"{comp.refdes}:{parameter}")
            return []
        return found


def _pin_names(comp: Component, pin: str) -> set[str]:
    names = {pin}
    for item in comp.pins:
        if item.number == pin or item.name == pin:
            names.add(item.number)
            if item.name:
                names.add(item.name)
    return names


def _signatures_differ(items) -> bool:
    if len(items) < 2:
        return False
    signatures = {
        (item.value, item.min_value, item.max_value, (item.unit or "").lower(), item.value_text)
        for item in items
    }
    return len(signatures) > 1


def _pin_net(comp: Component, number: str) -> str | None:
    for pin in comp.pins:
        if pin.number == number:
            return pin.net
    return None


def _parts(edges, groups, seen, fixed_roots, used) -> list[str]:
    names = {edge.refdes for edge in used}
    interesting = set(seen) | set(fixed_roots)
    for edge in edges:
        if edge.resistance == 0 and groups.find(edge.a) in interesting:
            names.add(edge.refdes)
    return sorted(names)


def _stamp(matrix, current, index, fixed_roots, left, right, conductance) -> None:
    if left in index and right in index:
        i, j = index[left], index[right]
        matrix[i][i] += conductance
        matrix[j][j] += conductance
        matrix[i][j] -= conductance
        matrix[j][i] -= conductance
        return
    if left in index and right in fixed_roots and fixed_roots[right] is not None:
        matrix[index[left]][index[left]] += conductance
        current[index[left]] += conductance * fixed_roots[right]
        return
    if right in index and left in fixed_roots and fixed_roots[left] is not None:
        matrix[index[right]][index[right]] += conductance
        current[index[right]] += conductance * fixed_roots[left]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(vector)
    if size == 0:
        return []
    rows = [matrix[i][:] + [vector[i]] for i in range(size)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(rows[row][col]))
        if abs(rows[pivot][col]) < 1e-12:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        scale = rows[col][col]
        for col2 in range(col, size + 1):
            rows[col][col2] /= scale
        for row in range(size):
            if row == col:
                continue
            factor = rows[row][col]
            for col2 in range(col, size + 1):
                rows[row][col2] -= factor * rows[col][col2]
    return [rows[i][size] for i in range(size)]
