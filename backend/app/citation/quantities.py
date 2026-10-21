"""从报告或依据原文里抽出带单位的剂量，不做数值判断。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_QUANTITY = re.compile(
    r"(?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<unit>mSv/a|mSv/年|μSv/h|µSv/h|uSv/h|mSv|μSv|µSv|uSv)"
)
_POPULATION = ("工作人员", "职业", "公众")
_ORGAN = ("眼晶体", "皮肤", "四肢")
_PERIOD = ("任何一年", "单一年", "五年", "年有效")
_ANNUAL_HINT = ("年", "/a")


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str
    raw: str
    roles: frozenset[str]
    window: str
    local_start: int
    local_end: int

    @property
    def population(self) -> frozenset[str]:
        return frozenset(role for role in self.roles if role in _POPULATION)

    @property
    def organs(self) -> frozenset[str]:
        return frozenset(role for role in self.roles if role in _ORGAN)

    @property
    def periods(self) -> frozenset[str]:
        return frozenset(role for role in self.roles if role in _PERIOD)


def roles_of(text: str) -> frozenset[str]:
    return frozenset(_roles(text))


def sentence_around(text: str, quantity: Quantity) -> tuple[str, int, int]:
    start = max(text.rfind("。", 0, quantity.local_start), text.rfind("\n", 0, quantity.local_start)) + 1
    ends = [pos for pos in (text.find("。", quantity.local_end), text.find("\n", quantity.local_end)) if pos >= 0]
    end = min(ends) if ends else len(text)
    sentence = text[start:end]
    return sentence, start, end


def extract_quantities(text: str, extra_roles: tuple[str, ...] = ()) -> list[Quantity]:
    found: list[Quantity] = []
    for match in _QUANTITY.finditer(text or ""):
        window, start, end = _window(text, match.start(), match.end())
        unit = _canonical_unit(match.group("unit"), window)
        roles = set(_roles(window))
        roles.update(role for role in extra_roles if role)
        found.append(
            Quantity(
                value=float(match.group("num")),
                unit=unit,
                raw=match.group(0),
                roles=frozenset(roles),
                window=window,
                local_start=start,
                local_end=end,
            )
        )
    return found


def _window(text: str, match_start: int, match_end: int) -> tuple[str, int, int]:
    separators = "，、；;。\n（）()"
    start = max((text.rfind(char, 0, match_start) for char in separators), default=-1) + 1
    ends = [text.find(char, match_end) for char in separators]
    ends = [pos for pos in ends if pos >= 0]
    end = min(ends) if ends else len(text)
    return text[start:end], start, end


def _roles(window: str) -> tuple[str, ...]:
    roles = [word for word in (*_POPULATION, *_ORGAN, *_PERIOD) if word in window]
    if "约束" in window:
        roles.append("约束")
    return tuple(roles)


def _canonical_unit(raw: str, window: str) -> str:
    raw = raw.replace("µ", "μ")
    if raw in {"μSv/h", "uSv/h"}:
        return "uSv/h"
    if raw in {"mSv/a", "mSv/年"}:
        return "mSv/a"
    if raw == "mSv" and any(hint in window for hint in _ANNUAL_HINT):
        return "mSv/a"
    if raw in {"μSv", "uSv"}:
        return "uSv"
    return raw


@dataclass
class LocatedQuantity:
    quantity: Quantity
    text: str
    char_start: int
    char_end: int
    path: str = ""
    doc_id: str = ""
    extra: dict = field(default_factory=dict)
