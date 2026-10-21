"""依据库：只接收 data/basis/ 里的文本，默认未核验，停用后不删原文。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.basis.validate import BasisValidationError, attach_hashes, validate_pieces
from app.db import data_dir, make_engine, make_session_factory
from app.hashing import new_id, sha256_bytes, sha256_text, utc_now
from app.laws.extract import TextExtractError, extract_text
from app.laws.splitter import split_legal, split_standard
from app.models import BasisClause, BasisDocument, ReviewLog

PENDING_PDF_NAME = "1739780846955_94511.pdf"
KINDS = {"法律", "国家标准", "其他"}


class BasisError(Exception):
    pass


def basis_dir() -> Path:
    folder = data_dir() / "basis"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def add_document(
    db: Session,
    *,
    doc_id: str,
    title: str,
    kind: str,
    effective_date: date,
    source_note: str,
    file_path: Path,
    by: str,
    publish_date: date | None = None,
    expect_count: int | None = None,
) -> BasisDocument:
    doc_id = doc_id.strip()
    by = by.strip()
    if not doc_id:
        raise BasisError("请填写 doc_id")
    if kind not in KINDS:
        raise BasisError("文件类型只能是法律、国家标准或其他")
    if not by:
        raise BasisError("请填写操作者")
    if not title.strip() or not source_note.strip():
        raise BasisError("请填写标题和来源说明")
    if db.scalar(select(BasisDocument).where(BasisDocument.doc_id == doc_id)) is not None:
        raise BasisError(f"{doc_id} 已在库中。停用不会腾出这个代号，请换一个 doc_id")

    resolved, relative = resolve_basis_file(file_path)
    raw = resolved.read_bytes()
    try:
        text = extract_text(resolved.name, raw)
    except TextExtractError as exc:
        raise BasisError(str(exc)) from exc
    pieces = attach_hashes(split_for_kind(kind, text))
    try:
        summary = validate_pieces(kind, pieces, expect_count=expect_count)
    except BasisValidationError as exc:
        raise BasisError(str(exc)) from exc

    document = BasisDocument(
        id=new_id(),
        doc_id=doc_id,
        title=title.strip(),
        kind=kind,
        publish_date=publish_date,
        effective_date=effective_date,
        source_note=source_note.strip(),
        relative_path=relative,
        file_sha256=sha256_bytes(raw),
        text_sha256=sha256_text(text),
        clause_count=summary["clause_count"],
        status="active",
        created_at=utc_now(),
    )
    db.add(document)
    db.flush()
    for piece in pieces:
        db.add(
            BasisClause(
                id=new_id(),
                document_id=document.id,
                ordinal=piece["ordinal"],
                path=piece["path"],
                text=piece["text"],
                text_sha256=piece["text_sha256"],
                verified=False,
                verified_by=None,
                verified_at=None,
            )
        )
    _log(db, target_type="basis_document", target_id=document.id, action="add", by=by, note=source_note.strip())
    db.flush()
    return document


def verify_clause(
    db: Session,
    *,
    doc_id: str,
    path: str,
    by: str,
    note: str = "",
) -> BasisClause:
    by = by.strip()
    if not by:
        raise BasisError("请填写操作者")
    document = _active_document(db, doc_id)
    clause = db.scalar(
        select(BasisClause).where(BasisClause.document_id == document.id, BasisClause.path == path)
    )
    if clause is None:
        raise BasisError(f"找不到条款 {path}")
    if sha256_text(clause.text) != clause.text_sha256:
        raise BasisError("条款 sha256 与正文不一致，不能核验")
    clause.verified = True
    clause.verified_by = by
    clause.verified_at = utc_now()
    _log(
        db,
        target_type="basis_clause",
        target_id=clause.id,
        action="verify",
        by=by,
        note=note or path,
    )
    db.flush()
    return clause


def disable_document(db: Session, *, doc_id: str, by: str, reason: str) -> BasisDocument:
    by = by.strip()
    reason = reason.strip()
    if not by or not reason:
        raise BasisError("停用时要填写操作者和原因")
    document = _active_document(db, doc_id)
    document.status = "disabled"
    kept = basis_dir() / document.relative_path
    if not kept.is_file():
        raise BasisError("原文文件不在 data/basis/，不能停用")
    _log(db, target_type="basis_document", target_id=document.id, action="disable", by=by, note=reason)
    db.flush()
    return document


def list_documents(db: Session) -> list[BasisDocument]:
    return list(db.scalars(select(BasisDocument).order_by(BasisDocument.created_at, BasisDocument.doc_id)))


def evaluable_clauses(db: Session, as_of: date) -> list[tuple[BasisDocument, BasisClause]]:
    rows = db.execute(
        select(BasisDocument, BasisClause)
        .join(BasisClause, BasisClause.document_id == BasisDocument.id)
        .where(BasisDocument.status == "active", BasisClause.verified.is_(True))
        .order_by(BasisDocument.doc_id, BasisClause.ordinal)
    ).all()
    kept = []
    for document, clause in rows:
        if document.effective_date > as_of:
            continue
        if sha256_text(clause.text) != clause.text_sha256:
            continue
        kept.append((document, clause))
    return kept


def resolve_basis_file(file_path: Path) -> tuple[Path, str]:
    resolved = file_path.expanduser().resolve()
    if resolved.name == PENDING_PDF_NAME:
        raise BasisError("这份 PDF 尚未确认标题，不能入库")
    root = basis_dir().resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise BasisError("原文必须放在 data/basis/ 里") from exc
    if resolved.suffix.lower() not in {".txt", ".md", ".pdf"}:
        raise BasisError("只接受 data/basis/ 中的 .txt、.md 或文本型 PDF")
    if not resolved.is_file():
        raise BasisError("找不到依据原文")
    return resolved, relative.as_posix()


def split_for_kind(kind: str, text: str) -> list[dict]:
    if kind == "国家标准":
        pieces = split_standard(text)
    elif kind == "法律":
        pieces = split_legal(text)
    else:
        pieces = split_standard(text) or split_legal(text)
    if not pieces:
        raise BasisError("没有切出条款。请检查原文是按条还是按条款号书写")
    return pieces


def open_session(database_url: str | None = None) -> Session:
    engine = make_engine(database_url)
    from app.models import Base

    if (database_url or "").startswith("sqlite") or database_url is None:
        Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def _active_document(db: Session, doc_id: str) -> BasisDocument:
    document = db.scalar(select(BasisDocument).where(BasisDocument.doc_id == doc_id))
    if document is None:
        raise BasisError(f"找不到依据 {doc_id}")
    if document.status != "active":
        raise BasisError(f"{doc_id} 已停用")
    return document


def _log(db: Session, *, target_type: str, target_id: str, action: str, by: str, note: str) -> None:
    db.add(
        ReviewLog(
            id=new_id(),
            finding_id=None,
            target_type=target_type,
            target_id=target_id,
            action=action,
            by=by,
            at=utc_now(),
            note=note,
        )
    )


def clause_hash_ok(clause: BasisClause) -> bool:
    return sha256_text(clause.text) == clause.text_sha256
