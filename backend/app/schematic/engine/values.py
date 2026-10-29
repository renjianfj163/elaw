"""把原理图上的阻容字符串换成国际单位。解析不了就返回空，不猜一个数。"""

from __future__ import annotations

import re

_OHM = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*(meg|m|k|r)?\s*(ohm|ohms|ω|Ω)?\s*$",
    re.IGNORECASE,
)
_SPLIT = re.compile(r"^\s*([0-9]+)([kKmMrR])([0-9]+)\s*(ohm|ohms|ω|Ω)?\s*$")
_CAP = re.compile(
    r"^\s*([0-9]*\.?[0-9]+)\s*([pnuμµmk])?\s*(f|farad)?\s*$",
    re.IGNORECASE,
)
_DNP = {"DNP", "DNS", "NF", "NM", "DNI"}


def is_unfitted_value(value: str | None) -> bool:
    return (value or "").strip().upper() in _DNP


def parse_resistance(value: str | None) -> float | None:
    """返回欧姆。电阻上的 M/m 按兆欧，不用毫欧。"""
    text = _clean(value)
    if text is None:
        return None
    split = _SPLIT.fullmatch(text)
    if split:
        whole, prefix, frac, _unit = split.groups()
        number = float(f"{whole}.{frac}")
        return number * _ohm_factor(prefix)
    match = _OHM.fullmatch(text)
    if not match:
        return None
    number = float(match.group(1))
    return number * _ohm_factor(match.group(2) or "")


def parse_capacitance(value: str | None) -> float | None:
    """返回法拉。没有单位时不把裸数字当成法拉。"""
    text = _clean(value)
    if text is None:
        return None
    match = _CAP.fullmatch(text)
    if not match or not match.group(2):
        return None
    return float(match.group(1)) * _farad_factor(match.group(2))


def parse_first_capacitance(text: str | None, *, pattern: str | None = None) -> float | None:
    if not text:
        return None
    source = text
    if pattern:
        found = re.search(pattern, source, re.IGNORECASE)
        if not found:
            return None
        source = found.group(1)
    found = re.search(r"([0-9]*\.?[0-9]+)\s*([pnuμµmk])\s*f", source, re.IGNORECASE)
    if not found:
        return None
    return float(found.group(1)) * _farad_factor(found.group(2))


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().replace(" ", "")
    if not text or text.upper() in _DNP:
        return None
    return text


def _ohm_factor(prefix: str) -> float:
    key = prefix.lower()
    if key in {"", "r"}:
        return 1.0
    if key == "k":
        return 1e3
    if key in {"m", "meg"}:
        return 1e6
    return 1.0


def _farad_factor(prefix: str) -> float:
    key = prefix.lower().replace("μ", "u").replace("µ", "u")
    return {
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "m": 1e-3,
        "k": 1e3,
    }[key]


def format_ohms(value: float) -> str:
    if value >= 1e6:
        return f"{value / 1e6:.4g} MΩ"
    if value >= 1e3:
        return f"{value / 1e3:.4g} kΩ"
    return f"{value:.4g} Ω"


def format_farads(value: float) -> str:
    if value >= 1e-3:
        return f"{value * 1e3:.4g} mF"
    if value >= 1e-6:
        return f"{value * 1e6:.4g} µF"
    if value >= 1e-9:
        return f"{value * 1e9:.4g} nF"
    return f"{value * 1e12:.4g} pF"


def format_volts(value: float) -> str:
    return f"{value:.4g} V"


def format_amps(value: float) -> str:
    if abs(value) >= 1:
        return f"{value:.4g} A"
    if abs(value) >= 1e-3:
        return f"{value * 1e3:.4g} mA"
    return f"{value * 1e6:.4g} µA"
