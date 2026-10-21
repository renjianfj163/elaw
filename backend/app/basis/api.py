"""依据库的手工入口。不采集、不核对国家标准平台。"""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.basis.search import search_basis
from app.basis.store import BasisError, add_document, disable_document, list_documents, verify_clause

router = APIRouter(prefix="/api/basis", tags=["basis"])


class AddBasisIn(BaseModel):
    doc_id: str
    title: str
    kind: str
    effective_date: date
    publish_date: date | None = None
    source_note: str
    file: str
    by: str
    expect_count: int | None = None


class VerifyIn(BaseModel):
    path: str
    by: str
    note: str = ""


class DisableIn(BaseModel):
    by: str
    reason: str = Field(min_length=1)


def _session():
    from app.main import SessionFactory

    return SessionFactory()


@router.get("/documents")
def documents():
    db = _session()
    try:
        return {
            "items": [
                {
                    "doc_id": row.doc_id,
                    "title": row.title,
                    "kind": row.kind,
                    "status": row.status,
                    "effective_date": row.effective_date.isoformat(),
                    "relative_path": row.relative_path,
                    "clause_count": row.clause_count,
                    "text_sha256": row.text_sha256,
                }
                for row in list_documents(db)
            ]
        }
    finally:
        db.close()


@router.post("/documents")
def create_document(body: AddBasisIn):
    db = _session()
    try:
        document = add_document(
            db,
            doc_id=body.doc_id,
            title=body.title,
            kind=body.kind,
            effective_date=body.effective_date,
            publish_date=body.publish_date,
            source_note=body.source_note,
            file_path=Path(body.file),
            by=body.by,
            expect_count=body.expect_count,
        )
        db.commit()
        return {"doc_id": document.doc_id, "clause_count": document.clause_count, "verified": False}
    except BasisError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/documents/{doc_id}/verify")
def verify_document_clause(doc_id: str, body: VerifyIn):
    db = _session()
    try:
        clause = verify_clause(db, doc_id=doc_id, path=body.path, by=body.by, note=body.note)
        db.commit()
        return {"doc_id": doc_id, "path": clause.path, "verified": True, "text_sha256": clause.text_sha256}
    except BasisError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/documents/{doc_id}/disable")
def disable(doc_id: str, body: DisableIn):
    db = _session()
    try:
        document = disable_document(db, doc_id=doc_id, by=body.by, reason=body.reason)
        db.commit()
        return {"doc_id": document.doc_id, "status": document.status, "relative_path": document.relative_path}
    except BasisError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.get("/search")
def search(q: str, as_of: date = date(2026, 10, 21), limit: int = 8):
    db = _session()
    try:
        return {"as_of": as_of.isoformat(), "items": search_basis(db, q, as_of, limit)}
    finally:
        db.close()
