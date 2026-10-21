from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hashing import new_id, sha256_bytes, sha256_text, utc_now
from app.laws.splitter import split_legal, split_standard
from app.models import LawArticle, LawInstrument, LawVersion, ReviewLog, VerificationReport


def split_for_level(level: str, text: str) -> list[dict]:
    if level in {"国家标准", "行业标准"}:
        pieces = split_standard(text)
        if pieces:
            return pieces
    pieces = split_legal(text)
    if pieces:
        return pieces
    return [{"path": "全文", "text": text}]


def get_or_create_instrument(
    db: Session,
    *,
    name: str,
    level: str,
    issuer: str | None = None,
    source_url: str | None = None,
) -> LawInstrument:
    found = db.scalar(select(LawInstrument).where(LawInstrument.name == name))
    if found:
        return found
    row = LawInstrument(
        id=new_id(),
        name=name,
        level=level,
        issuer=issuer,
        source_url=source_url,
        doc_no=None,
    )
    db.add(row)
    db.flush()
    return row


def add_version(
    db: Session,
    *,
    name: str,
    level: str,
    source: str,
    text: str,
    status: str,
    publish_date: date | None = None,
    effective_date: date | None = None,
    raw_status_code: str | None = None,
    issuer: str | None = None,
    file_bytes: bytes | None = None,
    filename: str | None = None,
    verified_by: str | None = None,
    verified_at: datetime | None = None,
) -> LawVersion:
    instrument = get_or_create_instrument(db, name=name, level=level, issuer=issuer)
    file_path = None
    if file_bytes is not None and filename:
        from app.db import data_dir

        folder = data_dir() / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = PathSuffix(filename)
        file_path = folder / f"{sha256_bytes(file_bytes)}{suffix}"
        file_path.write_bytes(file_bytes)
        file_path = str(file_path)
    version = LawVersion(
        id=new_id(),
        instrument_id=instrument.id,
        publish_date=publish_date,
        effective_date=effective_date,
        repeal_date=None,
        status=status,
        raw_text=text,
        raw_text_sha256=sha256_text(text),
        raw_file_path=file_path,
        source=source,
        raw_status_code=raw_status_code,
        verified_by=verified_by,
        verified_at=verified_at,
        created_at=utc_now(),
    )
    db.add(version)
    db.flush()
    for piece in split_for_level(level, text):
        db.add(
            LawArticle(
                id=new_id(),
                version_id=version.id,
                path=piece["path"],
                text=piece["text"],
                text_sha256=sha256_text(piece["text"]),
                embedding=None,
            )
        )
    return version


def add_report(db: Session, outcome: dict, *, level: str, version_id: str | None) -> VerificationReport:
    report = VerificationReport(
        id=new_id(),
        seed_name=outcome["seed_name"],
        source=outcome["source"],
        outcome=outcome["outcome"],
        version_label=outcome.get("version_label"),
        publish_date=outcome.get("publish_date"),
        effective_date=outcome.get("effective_date"),
        raw_status=outcome.get("raw_status"),
        superseded_note=outcome.get("superseded_note"),
        fulltext=outcome.get("fulltext") or "不适用",
        version_id=version_id,
        detail=outcome.get("detail") or {},
        created_at=utc_now(),
    )
    db.add(report)
    return report


def confirm_version(
    db: Session,
    *,
    version_id: str,
    status: str,
    by: str,
    publish_date: date | None,
    effective_date: date | None,
    note: str,
) -> LawVersion:
    if status not in {"有效", "已修订", "已废止", "未生效"}:
        raise ValueError("状态只能是有效、已修订、已废止或未生效")
    if not by.strip():
        raise ValueError("请填写审核人姓名")
    version = db.get(LawVersion, version_id)
    if version is None:
        raise ValueError("找不到这个版本")
    version.status = status
    version.publish_date = publish_date
    version.effective_date = effective_date
    version.verified_by = by.strip()
    version.verified_at = utc_now()
    db.add(
        ReviewLog(
            id=new_id(),
            finding_id=None,
            target_type="law_version",
            target_id=version.id,
            action="confirm",
            by=by.strip(),
            at=utc_now(),
            note=note or "已对照官网核对",
        )
    )
    return version


def PathSuffix(filename: str) -> str:
    dot = filename.rfind(".")
    if dot == -1:
        return ""
    suffix = filename[dot:].lower()
    if suffix in {".pdf", ".docx", ".txt", ".md"}:
        return suffix
    return ""
