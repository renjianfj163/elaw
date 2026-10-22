"""把安装包里已经人工确认的依据写入本机库。正文来自 seeds/confirmed，不写在代码里。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hashing import new_id, sha256_text, utc_now
from app.laws.store import add_report, add_version
from app.models import LawInstrument, LawVersion, ReviewLog, VerificationReport


def confirmed_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "seeds" / "confirmed"


def load_confirmed(folder: Path | None = None) -> list[dict]:
    root = folder or confirmed_dir()
    data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8")) or {}
    return list(data.get("items") or [])


def install_confirmed(db: Session, folder: Path | None = None) -> int:
    root = folder or confirmed_dir()
    if not (root / "manifest.yaml").is_file():
        return 0
    added = 0
    for item in load_confirmed(root):
        text = (root / item["file"]).read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        digest = sha256_text(text)
        if digest != item["sha256"]:
            raise ValueError(f"确认依据的哈希对不上：{item['name']}")
        version = _existing(db, item["name"], digest)
        if version is None:
            version = add_version(
                db,
                name=item["name"],
                level=item["level"],
                source=item["source"],
                text=text,
                status=item["status"],
                publish_date=_date(item.get("publish_date")),
                effective_date=_date(item.get("effective_date")),
                verified_by=item["verified_by"],
                verified_at=_when(item.get("verified_at")),
            )
            db.add(
                ReviewLog(
                    id=new_id(),
                    finding_id=None,
                    target_type="law_version",
                    target_id=version.id,
                    action="bundle_confirm",
                    by=item["verified_by"],
                    at=_when(item.get("verified_at")) or utc_now(),
                    note="安装包带入此前已对照官网确认的文本",
                )
            )
            added += 1
        _ensure_report(db, version, item)
    db.commit()
    return added


def _existing(db: Session, name: str, digest: str) -> LawVersion | None:
    return db.scalar(
        select(LawVersion)
        .join(LawInstrument, LawVersion.instrument_id == LawInstrument.id)
        .where(LawInstrument.name == name, LawVersion.raw_text_sha256 == digest)
    )


def _ensure_report(db: Session, version: LawVersion, item: dict) -> None:
    found = db.scalar(select(VerificationReport.id).where(VerificationReport.version_id == version.id))
    if found:
        return
    add_report(
        db,
        {
            "seed_name": item["name"],
            "source": item["source"],
            "outcome": "找到",
            "version_label": item["name"],
            "publish_date": version.publish_date,
            "effective_date": version.effective_date,
            "raw_status": None,
            "superseded_note": None,
            "fulltext": "已获取",
            "detail": {"confirmed": True, "bundled": True},
        },
        level=item["level"],
        version_id=version.id,
    )


def _date(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _when(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))
