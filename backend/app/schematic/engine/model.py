"""原理图审查用的电路模型。

连接以引脚上的网络名为准，不把网络名里的电压或 EN 字样当成电气事实。
缺省字段保持未知，调用方没有给出的额定值不会在这里补默认数。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CircuitError(ValueError):
    pass


class Channel(BaseModel):
    a_pin: str
    b_pin: str


class Pin(BaseModel):
    number: str
    name: str = ""
    net: str | None = None
    direction: Literal["input", "output", "bidirectional", "passive", "power", "ground", "unknown"] = "unknown"
    pin_type: str = ""
    output_kind: Literal["push_pull", "open_drain", "open_source", "unknown"] = "unknown"
    abs_max_v: float | None = None
    tolerant_v: float | None = None
    vol_v: float | None = None
    voh_v: float | None = None
    iol_max_a: float | None = None
    voltage_domain: str | None = None
    enable_net: str | None = None


class Component(BaseModel):
    refdes: str
    mpn: str | None = None
    manufacturer: str | None = None
    package: str | None = None
    value: str | None = None
    comment: str = ""
    kind: Literal[
        "resistor",
        "capacitor",
        "inductor",
        "ferrite",
        "jumper",
        "diode",
        "ic",
        "connector",
        "crystal",
        "other",
        "unknown",
    ] = "unknown"
    role: str = ""
    fitted: bool | None = None
    page: int | None = None
    pins: list[Pin] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    supply_net: str | None = None
    ioff: bool | None = None
    clamp_to_supply: bool | None = None
    tolerance_pct: float | None = None
    parameters: dict[str, float] = Field(default_factory=dict)
    mode: str = ""


class Net(BaseModel):
    name: str
    is_power: bool = False
    is_ground: bool = False
    nominal_v: float | None = None
    min_v: float | None = None
    max_v: float | None = None
    aliases: list[str] = Field(default_factory=list)


class PowerDomain(BaseModel):
    name: str
    net: str
    nominal_v: float | None = None
    min_v: float | None = None
    max_v: float | None = None
    source: str | None = None


class Endpoint(BaseModel):
    component: str
    pin: str
    role: str
    side: str = ""


class SignalGroup(BaseModel):
    name: str
    kind: str
    swap_allowed: bool | None = None
    endpoints: list[Endpoint] = Field(default_factory=list)


class Constraint(BaseModel):
    """只有 verified 为真的约束可以参与判定。

    页码、摘录和约束编号来自设计资料。没有这些字段时保持空白，不补猜测值。
    """

    parameter: str
    mpn: str = ""
    refdes: str = ""
    pin: str = ""
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    tolerance_pct: float | None = None
    unit: str = ""
    value_text: str = ""
    condition: str = ""
    source: str = ""
    revision: str = ""
    verified: bool = False
    constraint_id: str = ""
    constraint_type: str = ""
    origin: str = ""
    document_title: str = ""
    source_page: int | None = None
    source_section: str = ""
    source_quote: str = ""
    verification_status: str = ""
    interface: str = ""
    side: str = ""
    operating_mode: str = ""


class AssemblyOption(BaseModel):
    id: str
    refdes: list[str] = Field(default_factory=list)
    exclusive: bool = True
    note: str = ""


_GROUND_NAMES = {"GND", "AGND", "DGND", "PGND", "VSS", "0V"}


class Circuit(BaseModel):
    name: str = ""
    source: Literal["netlist", "pdf", "synthetic", "mixed"] = "synthetic"
    connectivity_complete: bool = True
    components: dict[str, Component] = Field(default_factory=dict)
    nets: dict[str, Net] = Field(default_factory=dict)
    power_domains: list[PowerDomain] = Field(default_factory=list)
    signal_groups: list[SignalGroup] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    assembly: list[AssemblyOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize(self) -> Circuit:
        for key, comp in self.components.items():
            if not comp.refdes:
                comp.refdes = key
            elif comp.refdes != key:
                raise CircuitError(f"元件键 {key} 与位号 {comp.refdes} 不一致")
        found = {name: net.model_copy(deep=True) for name, net in self.nets.items()}
        for domain in self.power_domains:
            net = found.get(domain.net)
            if net is None:
                net = Net(name=domain.net)
                found[domain.net] = net
            net.is_power = True
            if domain.nominal_v is not None and net.nominal_v is None:
                net.nominal_v = domain.nominal_v
            if domain.min_v is not None and net.min_v is None:
                net.min_v = domain.min_v
            if domain.max_v is not None and net.max_v is None:
                net.max_v = domain.max_v
            if domain.nominal_v == 0 and (domain.max_v in {None, 0}):
                net.is_ground = True
        aliases: dict[str, str] = {}
        for net in found.values():
            if net.name.upper() in _GROUND_NAMES:
                net.is_ground = True
            for alias in net.aliases:
                if alias in aliases and aliases[alias] != net.name:
                    raise CircuitError(f"网络别名 {alias} 指向了两个网络")
                aliases[alias] = net.name
        for comp in self.components.values():
            for pin in comp.pins:
                if not pin.net:
                    continue
                pin.net = aliases.get(pin.net, pin.net)
                if pin.net not in found:
                    found[pin.net] = Net(name=pin.net)
                if found[pin.net].name.upper() in _GROUND_NAMES:
                    found[pin.net].is_ground = True
        self.nets = found
        return self
