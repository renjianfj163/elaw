"""检查项的人工入口：保存草稿、查看草稿、审核后启用。"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.checks.items import CheckError, CheckItemView, activate_item, list_items, save_draft
from app.hashing import new_id
from app.models import BasisClause, BasisDocument

router = APIRouter(prefix="/api/check-items", tags=["check-items"])


class DraftIn(BaseModel):
    doc_id: str
    clause_path: str
    item_code: str = Field(min_length=1, max_length=80)
    kind: Literal["numeric", "presence", "qualitative"]
    report_topics: list[str] = Field(min_length=1)
    required_params: list[str] = Field(default_factory=list)
    requirement: dict = Field(default_factory=dict)
    quote: str = Field(min_length=1)
    by: str
    note: str = ""


class ActivateIn(BaseModel):
    by: str
    note: str = ""


def _session():
    from app.main import SessionFactory

    return SessionFactory()


def _clause(db, doc_id: str, path: str) -> BasisClause | None:
    stmt = (
        select(BasisClause)
        .join(BasisDocument, BasisClause.document_id == BasisDocument.id)
        .where(
            BasisDocument.doc_id == doc_id,
            BasisDocument.status == "active",
            BasisClause.path == path,
            BasisClause.verified.is_(True),
            BasisClause.verified_at.is_not(None),
        )
    )
    return db.scalar(stmt)


@router.get("")
def items(status: str | None = None):
    db = _session()
    try:
        return {
            "items": [
                {
                    "id": row.id,
                    "doc_id": row.doc_id,
                    "clause_path": row.clause_path,
                    "item_code": row.item_code,
                    "kind": row.kind,
                    "status": row.status,
                    "quote": row.quote,
                    "report_topics": row.report_topics,
                    "reviewed_by": row.reviewed_by,
                }
                for row in list_items(db, status)
            ]
        }
    finally:
        db.close()


@router.post("/draft")
def create_draft(body: DraftIn):
    db = _session()
    try:
        clause = _clause(db, body.doc_id, body.clause_path)
        if clause is None:
            raise CheckError("只能依据已核验条款起草")
        row = save_draft(
            db,
            CheckItemView(
                id=new_id(),
                doc_id=body.doc_id,
                clause_path=clause.path,
                clause_sha256=clause.text_sha256,
                item_code=body.item_code,
                kind=body.kind,
                report_topics=body.report_topics,
                required_params=body.required_params,
                requirement=body.requirement,
                quote=body.quote,
                status="draft",
            ),
            by=body.by,
            note=body.note,
        )
        db.commit()
        return {"id": row.id, "status": row.status, "item_code": row.item_code}
    except CheckError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/{item_id}/activate")
def activate(item_id: str, body: ActivateIn):
    db = _session()
    try:
        row = activate_item(db, item_id, by=body.by, note=body.note)
        db.commit()
        return {"id": row.id, "status": row.status, "reviewed_by": row.reviewed_by}
    except CheckError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()
