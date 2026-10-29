"""只做已经给齐输入的电气计算。缺一项就不填结果，也不套默认值。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.rules.evaluator import evaluate


class Calculation(BaseModel):
    formula: str
    inputs: dict[str, str] = Field(default_factory=dict)
    result: str | None = None
    unit: str = ""
    value: float | None = None
    assumptions: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    evaluator_trace: list = Field(default_factory=list)


def compare(op: str, left: float, right: float) -> dict:
    """数值比较走现有规则引擎，避免在审查模块里另写一套比较。"""
    return evaluate({op: [{"var": "left"}, right]}, {"left": left})


def between(value: float, low: float, high: float) -> dict:
    return evaluate({"between": [{"var": "value"}, low, high]}, {"value": value})


def pullup_current(v_pullup: float | None, v_ol: float | None, resistance: float | None) -> Calculation:
    missing: list[str] = []
    assumptions: list[str] = []
    if v_pullup is None:
        missing.append("v_pullup")
    if resistance is None or resistance <= 0:
        missing.append("resistance")
    vol = 0.0 if v_ol is None else v_ol
    if v_ol is None and not missing:
        assumptions.append("未提供 VOL，灌电流按 VOL=0V 估算上限。")
    if missing or v_pullup is None or resistance is None:
        return Calculation(
            formula="I=(Vpullup-VOL)/R",
            missing=missing,
            assumptions=assumptions,
            unit="A",
        )
    amps = (v_pullup - vol) / resistance
    return Calculation(
        formula="I=(Vpullup-VOL)/R",
        inputs={
            "Vpullup": f"{v_pullup:.6g} V",
            "VOL": f"{vol:.6g} V",
            "R": f"{resistance:.6g} Ω",
        },
        result=f"{amps:.6g} A",
        unit="A",
        value=amps,
        assumptions=assumptions,
    )


def thevenin_current(v_th: float | None, r_th: float | None, v_ol: float | None) -> Calculation:
    missing: list[str] = []
    assumptions: list[str] = []
    if v_th is None:
        missing.append("v_th")
    if r_th is None:
        missing.append("r_th")
    vol = 0.0 if v_ol is None else v_ol
    if v_ol is None and not missing:
        assumptions.append("未提供 VOL，灌电流按 VOL=0V 估算上限。")
    if r_th == 0:
        assumptions.append("从引脚看进去的戴维南电阻为 0，灌电流不受串联电阻限制。")
        return Calculation(
            formula="I=(Vth-VOL)/Rth",
            inputs={"Vth": f"{v_th} V" if v_th is not None else "", "Rth": "0 Ω"},
            unit="A",
            assumptions=assumptions,
            missing=missing,
        )
    if missing or v_th is None or r_th is None:
        return Calculation(formula="I=(Vth-VOL)/Rth", missing=missing, assumptions=assumptions, unit="A")
    if r_th < 0:
        return Calculation(formula="I=(Vth-VOL)/Rth", missing=["r_th"], unit="A")
    amps = (v_th - vol) / r_th
    return Calculation(
        formula="I=(Vth-VOL)/Rth",
        inputs={"Vth": f"{v_th:.6g} V", "VOL": f"{vol:.6g} V", "Rth": f"{r_th:.6g} Ω"},
        result=f"{amps:.6g} A",
        unit="A",
        value=amps,
        assumptions=assumptions,
    )


def feedback_vout(vref: float | None, r_top: float | None, r_bottom: float | None) -> Calculation:
    missing = [name for name, value in (("vref", vref), ("r_top", r_top), ("r_bottom", r_bottom)) if value is None]
    if r_bottom == 0:
        missing.append("r_bottom")
    if missing or vref is None or r_top is None or r_bottom is None or r_bottom == 0:
        return Calculation(formula="Vout=Vref*(1+Rtop/Rbottom)", missing=list(dict.fromkeys(missing)), unit="V")
    vout = vref * (1 + r_top / r_bottom)
    return Calculation(
        formula="Vout=Vref*(1+Rtop/Rbottom)",
        inputs={
            "Vref": f"{vref:.6g} V",
            "Rtop": f"{r_top:.6g} Ω",
            "Rbottom": f"{r_bottom:.6g} Ω",
        },
        result=f"{vout:.6g} V",
        unit="V",
        value=vout,
        assumptions=["电阻位置来自网表：上电阻跨接输出与反馈，下电阻跨接反馈与地。"],
    )


def crystal_load(c1: float | None, c2: float | None, c_stray: float | None) -> Calculation:
    missing: list[str] = []
    if c1 is None:
        missing.append("c1")
    if c2 is None:
        missing.append("c2")
    assumptions = ["采用 Pierce 常用近似 CL≈C1*C2/(C1+C2)+Cstray，未包含晶振驱动等级和放大器输入电容的单独核对。"]
    if missing or c1 is None or c2 is None or c1 + c2 == 0:
        return Calculation(formula="CL=C1*C2/(C1+C2)+Cstray", missing=missing, assumptions=assumptions, unit="F")
    series = (c1 * c2) / (c1 + c2)
    if c_stray is None:
        assumptions.append("未提供寄生电容，结果只包含两颗负载电容的串联部分。")
        return Calculation(
            formula="Cseries=C1*C2/(C1+C2)",
            inputs={"C1": f"{c1:.6g} F", "C2": f"{c2:.6g} F"},
            result=f"{series:.6g} F",
            unit="F",
            value=series,
            assumptions=assumptions,
            missing=["c_stray"],
        )
    load = series + c_stray
    return Calculation(
        formula="CL=C1*C2/(C1+C2)+Cstray",
        inputs={"C1": f"{c1:.6g} F", "C2": f"{c2:.6g} F", "Cstray": f"{c_stray:.6g} F"},
        result=f"{load:.6g} F",
        unit="F",
        value=load,
        assumptions=assumptions,
    )


def parallel_sum(values: list[float]) -> Calculation:
    if not values:
        return Calculation(formula="Cout=C1+C2+...", missing=["capacitors"], unit="F")
    total = sum(values)
    return Calculation(
        formula="Cout=C1+C2+...",
        inputs={f"C{i}": f"{value:.6g} F" for i, value in enumerate(values, start=1)},
        result=f"{total:.6g} F",
        unit="F",
        value=total,
        assumptions=["只累加一端在输出、另一端在地的电容。未给出的直流偏压降容不会被补成默认系数。"],
    )
