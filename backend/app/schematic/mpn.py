"""把图纸上的订购料号收成可以查手册的型号。

卷带、无铅这类后缀先去掉。ST、TI 再收成手册封面上常见的基础型号。
收得太狠会查错系列，所以只处理这几类能对上封面的写法。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

_INNER = re.compile(r"[（(]([^()（）]+)[)）]")
_PACK_WORD = ("CUTTAPE", "REEL7", "NOPB", "PBFREE", "REEL", "PBF", "R2R")
_PACK_SHORT = ("TR", "RL", "CT", "R7")
_PASSIVE = re.compile(r"^\d+(\.\d+)?(R|K|M|OHM|UF|NF|PF|UH|NH|V|A)?$")
_ST_ORDER = re.compile(r"^(STM32[A-Z]\d{3}[A-Z]{2})[A-Z]\d[A-Z0-9]*$")
_TI_REEL = re.compile(r"^([A-Z]{2,6}\d{3,6})([A-Z]{2,3})[RT]$")
_TI_PKG = re.compile(r"^([A-Z]{2,6}\d{3,6})([A-Z]{2,3})$")
_REFDES = re.compile(r"^[A-Z]{1,3}\d{1,4}[A-Z]?$")


class NormalizedMpn(BaseModel):
    raw: str = ""
    cleaned: str = ""
    base: str = ""
    vendor: str = ""
    needles: list[str] = Field(default_factory=list)

    @property
    def search_order(self) -> list[str]:
        found: list[str] = []
        for item in (self.cleaned, self.base):
            if item and item not in found:
                found.append(item)
        return found


def mpn_from_component(text: str) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    inner = _INNER.search(raw)
    if inner:
        return inner.group(1).strip()
    compact = re.sub(r"[\s_]+", "", raw).upper()
    if _REFDES.fullmatch(compact):
        return ""
    return raw.strip()


def normalize_mpn(raw: str) -> NormalizedMpn:
    picked = mpn_from_component(raw)
    compact = re.sub(r"[\s_]+", "", picked).upper()
    compact = re.sub(r"[^A-Z0-9.+-]", "", compact)
    cleaned = _strip_packaging(compact)
    if len(cleaned) < 4 or _PASSIVE.fullmatch(cleaned):
        return NormalizedMpn(raw=str(raw or "").strip())
    base = _base_of(cleaned)
    return NormalizedMpn(
        raw=str(raw or "").strip(),
        cleaned=cleaned,
        base=base,
        vendor=_vendor(base or cleaned),
        needles=_needles(cleaned, base),
    )


def _strip_packaging(text: str) -> str:
    current = text
    for _ in range(4):
        nxt = _strip_once(current)
        if not nxt or nxt == current:
            break
        current = nxt
    return current


def _strip_once(text: str) -> str:
    for token in _PACK_WORD:
        for sep in ("-", "/", "_", ""):
            suffix = sep + token
            if text.endswith(suffix) and len(text) > len(suffix) + 4:
                return text[: -len(suffix)]
    for token in _PACK_SHORT:
        for sep in ("-", "/", "_"):
            suffix = sep + token
            if text.endswith(suffix) and len(text) > len(suffix) + 4:
                return text[: -len(suffix)]
        if token == "TR" and text.endswith("TR") and len(text) > 10:
            return text[:-2]
    if text.endswith("+T") and len(text) > 6:
        return text[:-2]
    if text.endswith("+") and len(text) > 5:
        return text[:-1]
    return text


def _base_of(cleaned: str) -> str:
    st = _ST_ORDER.match(cleaned)
    if st:
        return st.group(1)
    if _is_ti(cleaned):
        reel = _TI_REEL.match(cleaned)
        if reel and len(reel.group(1)) >= 6:
            return reel.group(1)
        package = _TI_PKG.match(cleaned)
        if package and len(package.group(1)) >= 6:
            return package.group(1)
    return cleaned


def _needles(cleaned: str, base: str) -> list[str]:
    found: list[str] = []
    for item in (cleaned, base):
        if item and item not in found:
            found.append(item)
    if len(base) >= 10:
        short = base[:-2]
        if len(short) >= 8 and short not in found:
            found.append(short)
    return found


def _vendor(mpn: str) -> str:
    if mpn.startswith(("STM32", "STM8")):
        return "st"
    if _is_ti(mpn):
        return "ti"
    if _is_adi(mpn):
        return "adi"
    if mpn.startswith(("LPC", "MIMX", "S32K", "TJA", "PCA9")) or bool(re.match(r"^MK\d", mpn)):
        return "nxp"
    return ""


def _is_ti(mpn: str) -> bool:
    if mpn.startswith(
        ("TPS", "TLV", "DRV", "INA", "OPA", "BQ", "UCC", "CSD", "TPA", "THS", "ADS", "AMC", "SN74", "TLC", "TLE", "ISO")
    ):
        return True
    return bool(re.match(r"^(LM|REF|LP)\d", mpn))


def _is_adi(mpn: str) -> bool:
    if mpn.startswith(("ADP", "LTC", "LTM", "HMC", "ADA", "ADM", "ADN", "SSM")):
        return True
    return bool(re.match(r"^(AD|LT|MAX)\d", mpn))
