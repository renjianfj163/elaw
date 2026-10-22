"""已核验的剂量限值表。未核验的行不参与比对，程序不从条款正文里猜数字。"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from pydantic import BaseModel

from app.db import repo_root

_TRUE = {"1", "true", "yes", "是"}
_POPULATION = {"职业": "职业", "工作人员": "职业", "公众": "公众"}


class DoseLimit(BaseModel):
    doc_id: str
    clause_path: str
    population: str
    organ: str = ""
    period: str = ""
    value: float
    unit: str
    quote: str
    verified: bool = False


def default_limits_path() -> Path:
    override = os.environ.get("DOSE_LIMITS")
    if override:
        return Path(override)
    return repo_root() / "data" / "dose_limits.csv"


def load_verified_limits(path: Path | None = None) -> list[DoseLimit]:
    file_path = path or default_limits_path()
    if not file_path.is_file():
        return []
    rows: list[DoseLimit] = []
    with file_path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            row = _row(record)
            if row is not None and row.verified:
                rows.append(row)
    return rows


def limits_fingerprint(rows: list[DoseLimit]) -> str:
    from app.hashing import stable_hash

    payload = [
        row.model_dump()
        for row in sorted(rows, key=lambda item: (item.doc_id, item.clause_path, item.population, item.organ, item.period, item.value))
    ]
    return stable_hash({"limits": payload})


def subject_of_quantity(quantity) -> tuple[str, str, str] | None:
    population = population_name(quantity.population)
    if population is None:
        return None
    periods = set(quantity.periods)
    if periods & {"五年", "任何一年", "单一年"}:
        periods.discard("年有效")
    if len(periods) > 1:
        return None
    period = next(iter(periods)) if periods else ""
    organ = "|".join(sorted(quantity.organs))
    return population, organ, period


def population_name(roles) -> str | None:
    found = {_POPULATION[role] for role in roles if role in _POPULATION}
    if len(found) != 1:
        return None
    return next(iter(found))


def quantity_key(quantity) -> tuple[str, str, str, str] | None:
    subject = subject_of_quantity(quantity)
    if subject is None:
        return None
    return (*subject, quantity.unit)


def _row(record: dict) -> DoseLimit | None:
    doc_id = (record.get("doc_id") or "").strip()
    path = (record.get("clause_path") or "").strip()
    population = _POPULATION.get((record.get("population") or "").strip(), "")
    quote = (record.get("quote") or "").strip()
    unit = (record.get("unit") or "").strip()
    if not doc_id or not path or not population or not quote or not unit:
        return None
    try:
        value = float((record.get("value") or "").strip())
    except ValueError:
        return None
    organ = "|".join(part.strip() for part in (record.get("organ") or "").replace("、", "|").replace("或", "|").split("|") if part.strip())
    organ = "|".join(sorted(organ.split("|"))) if organ else ""
    return DoseLimit(
        doc_id=doc_id,
        clause_path=path,
        population=population,
        organ=organ,
        period=(record.get("period") or "").strip(),
        value=value,
        unit=unit,
        quote=quote,
        verified=(record.get("verified") or "").strip().lower() in _TRUE,
    )
