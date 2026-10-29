"""按工作模式筛选已复核的引脚角色和开关通路。

同一引脚在不同模式下角色不同，而网表没有给出模式时，不把它们当成冲突，也不直接判错。
同一模式下角色不一致，才是约束冲突。
"""

from __future__ import annotations

from app.schematic.engine.dc import CircuitView
from app.schematic.engine.model import Component

CONDITIONAL_ROLES = {"mux", "analog_switch", "isolator"}
_HARD = {"", "hard_limit"}


def classify_pin_roles(view: CircuitView, comp: Component, number: str):
    items = _facts(view, comp, "pin_role", number)
    return _classify(comp, items)


def classify_routes(comp: Component, routes: list):
    if not routes:
        return "unmodeled", []
    selected = (comp.mode or "").strip()
    if any((item.operating_mode or "").strip() for item in routes) and not selected:
        return "mode_unknown", routes
    applicable = [
        item
        for item in routes
        if not (item.operating_mode or "").strip() or item.operating_mode.strip() == selected
    ]
    texts = {_text(item) for item in applicable if _text(item)}
    if len(texts) > 1:
        return "conflict", applicable
    if len(texts) != 1 or _route_pins(next(iter(texts))) is None:
        return "unmodeled", applicable
    return "ok", applicable


def signal_reach(view: CircuitView, start: str | None) -> set[str]:
    if not start:
        return set()
    reach = set(view.reach(start))
    changed = True
    while changed:
        changed = False
        for comp in view.circuit.components.values():
            if comp.fitted is False or comp.role not in CONDITIONAL_ROLES:
                continue
            status, routes = classify_routes(comp, _facts(view, comp, "switch_route", None))
            if status != "ok":
                continue
            for route in routes:
                ends = _route_pins(route.value_text)
                if ends is None:
                    continue
                left = view.net_of(comp.refdes, ends[0])
                right = view.net_of(comp.refdes, ends[1])
                if left in reach and right and right not in reach:
                    reach |= view.reach(right)
                    changed = True
                elif right in reach and left and left not in reach:
                    reach |= view.reach(left)
                    changed = True
    return reach


def conditional_bridge(view: CircuitView, source: str | None, target: str | None) -> Component | None:
    if not source or not target or target in signal_reach(view, source):
        return None
    reached = signal_reach(view, source)
    target_reach = signal_reach(view, target)
    for comp in view.circuit.components.values():
        if comp.fitted is False or comp.role not in CONDITIONAL_ROLES:
            continue
        nets = {pin.net for pin in comp.pins if pin.net}
        if nets & reached and nets & target_reach:
            return comp
    return None


def _facts(view: CircuitView, comp: Component, parameter: str, pin: str | None):
    found = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in view.circuit.constraints:
        if not _match(item, comp, parameter, pin):
            continue
        _add(found, seen, item)
    knowledge = view.knowledge
    if knowledge is not None:
        for bound in knowledge.bindings:
            engine = bound.engine
            if bound.refdes != comp.refdes or engine.parameter != parameter:
                continue
            if engine.constraint_type not in _HARD:
                continue
            if pin and engine.pin and engine.pin not in _names(comp, pin):
                continue
            if pin is None and engine.pin:
                continue
            _add(found, seen, engine)
    return found


def _classify(comp: Component, items: list):
    if not items:
        return "absent", []
    selected = (comp.mode or "").strip()
    conditional = [item for item in items if (item.operating_mode or "").strip()]
    if conditional and not selected:
        texts = {_text(item) for item in items}
        if len(texts) > 1:
            return "mode_unknown", items
    applicable = [
        item
        for item in items
        if not (item.operating_mode or "").strip() or (selected and item.operating_mode.strip() == selected)
    ]
    if selected and conditional and not applicable:
        return "mode_unknown", items
    texts = {_text(item) for item in applicable if _text(item)}
    if len(texts) > 1:
        return "conflict", applicable
    return "ok", applicable


def _match(item, comp: Component, parameter: str, pin: str | None) -> bool:
    if item.parameter != parameter or not item.verified or item.constraint_type not in _HARD:
        return False
    if item.refdes and item.refdes != comp.refdes:
        return False
    if item.mpn and item.mpn != (comp.mpn or ""):
        return False
    if not item.refdes and not item.mpn:
        return False
    if pin:
        if not item.pin or item.pin not in _names(comp, pin):
            return False
    elif item.pin:
        return False
    return True


def _add(found: list, seen: set[tuple[str, str, str, str]], item) -> None:
    key = (item.constraint_id, item.pin, _text(item), (item.operating_mode or "").strip())
    if key in seen:
        return
    seen.add(key)
    found.append(item)


def _text(item) -> str:
    return (item.value_text or "").strip()


def _names(comp: Component, pin: str) -> set[str]:
    names = {pin}
    for item in comp.pins:
        if item.number == pin or item.name == pin:
            names.add(item.number)
            if item.name:
                names.add(item.name)
    return names


def _route_pins(text: str) -> tuple[str, str] | None:
    if ":" not in text:
        return None
    left, right = text.split(":", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        return None
    return left, right
