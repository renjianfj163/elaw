"""几条规则共用的取证和比较。"""

from __future__ import annotations

from app.schematic.engine.calculators import compare
from app.schematic.engine.dc import Bias, CircuitView
from app.schematic.engine.model import Component, Pin
from app.schematic.engine.report import Status

IO_TYPES = {"gpio", "io", "signal", "nrst", "boot", "reset", "oe", "dir"}
MCU_ROLES = {"mcu", "som", "module", "io"}


def is_en(pin: Pin) -> bool:
    return pin.pin_type in {"en", "enable"} or pin.name.upper() in {"EN", "ENABLE"}


def ics(view: CircuitView):
    for comp in view.circuit.components.values():
        if comp.fitted is not False and comp.kind == "ic":
            yield comp


def io_pins(view: CircuitView, *, enable: bool):
    for comp in ics(view):
        for pin in comp.pins:
            if is_en(pin):
                if enable:
                    yield comp, pin
                continue
            if enable:
                continue
            if pin.pin_type in IO_TYPES:
                yield comp, pin
            elif (
                comp.role in MCU_ROLES
                and pin.direction in {"input", "output", "bidirectional"}
                and pin.pin_type not in {"power", "ground", "feedback", "vout"}
            ):
                yield comp, pin


def can_sink(pin: Pin) -> bool:
    if pin.direction == "input" and pin.output_kind != "open_drain":
        return False
    return (
        pin.direction in {"output", "bidirectional"}
        or pin.output_kind == "open_drain"
        or pin.pin_type in {"gpio", "io"}
    )


def rating(pin: Pin) -> tuple[float | None, str]:
    if pin.abs_max_v is not None:
        return pin.abs_max_v, "绝对最大额定值"
    if pin.tolerant_v is not None:
        return pin.tolerant_v, "已给出的耐受电压"
    return None, ""


def volts(value: float) -> float:
    return round(value, 6)


def amps(value: float) -> float:
    return round(value, 9)


def exceeds(left: float, right: float) -> bool:
    return compare(">", left, right)["result"] == "true"


def soften(status: Status, bias: Bias) -> Status:
    if status == "confirmed" and (bias.uncertain or bias.assumed_short):
        return "probable"
    return status


def pages(view: CircuitView, refdes: list[str]) -> list[int]:
    found: list[int] = []
    for name in refdes:
        comp = view.circuit.components.get(name)
        if comp is not None and comp.page is not None and comp.page not in found:
            found.append(comp.page)
    return found


def cite(item) -> str:
    title = getattr(item, "document_title", "") or item.source
    parts = [item.parameter, title, item.revision]
    page = getattr(item, "source_page", None)
    if page is not None:
        parts.append(f"第{page}页")
    section = getattr(item, "source_section", "")
    if section:
        parts.append(section)
    if item.condition:
        parts.append(item.condition)
    quote = getattr(item, "source_quote", "")
    if quote:
        parts.append(quote)
    return "，".join(part for part in parts if part)


def assumptions_for(bias: Bias) -> list[str]:
    notes = ["应力按电源电压上限计算；电源只给了标称值时用标称值。"]
    if bias.assumed_short:
        notes.append("磁珠或电感按直流短路估计，结论降为 probable。")
    if bias.uncertain:
        notes.append("通路上有装配状态未确认的元件，结论从 confirmed 降为 probable。")
    return notes


def pin_label(comp: Component, pin: Pin) -> str:
    name = f" {pin.name}" if pin.name else ""
    return f"{comp.refdes}.{pin.number}{name}"
