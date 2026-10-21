from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hashing import new_id, stable_hash, utc_now
from app.laws.catalog_decision import compile_when, decide, diff_types
from app.models import ApprovalSnapshot, CatalogDiff, CatalogRelease, CatalogRow, ReviewLog


def save_draft(db: Session, *, name: str, rows: list[dict], note: str = "") -> CatalogRelease:
    name = name.strip()
    if not name:
        raise ValueError("请填写名录名称")
    if not rows:
        raise ValueError("至少要有一行")
    release = db.scalar(select(CatalogRelease).where(CatalogRelease.name == name, CatalogRelease.status == "draft"))
    if release is None:
        release = CatalogRelease(
            id=new_id(),
            name=name,
            status="draft",
            content_hash="",
            note=note,
            confirmed_by=None,
            confirmed_at=None,
            created_at=utc_now(),
        )
        db.add(release)
        db.flush()
    else:
        release.note = note
        for old in db.scalars(select(CatalogRow).where(CatalogRow.release_id == release.id)):
            db.delete(old)
        db.flush()
    packed = []
    for index, raw in enumerate(rows):
        category = str(raw.get("category") or "").strip()
        if not category:
            raise ValueError("每一行都要有项目类别")
        compiled = {
            "category": category,
            "book_text": str(raw.get("book_text") or ""),
            "form_text": str(raw.get("form_text") or ""),
            "register_text": str(raw.get("register_text") or ""),
            "book_rule": compile_when(raw.get("book_when")),
            "form_rule": compile_when(raw.get("form_when")),
            "register_rule": compile_when(raw.get("register_when")),
        }
        packed.append(compiled)
        db.add(
            CatalogRow(
                id=new_id(),
                release_id=release.id,
                row_index=index,
                **compiled,
            )
        )
    release.content_hash = stable_hash({"name": name, "rows": packed})
    return release


def confirm_release(db: Session, *, release_id: str, by: str, note: str) -> dict:
    if not by.strip():
        raise ValueError("请填写审核人姓名")
    release = db.get(CatalogRelease, release_id)
    if release is None or release.status != "draft":
        raise ValueError("没有可确认的草稿")
    rows = _rows(db, release.id)
    if not rows:
        raise ValueError("草稿是空的")
    for row in rows:
        if not (row.book_rule and row.form_rule and row.register_rule):
            raise ValueError(f"「{row.category}」还有栏位没写成条件，不能整表确认")
    previous = _latest_confirmed(db, release.name)
    release.status = "confirmed"
    release.confirmed_by = by.strip()
    release.confirmed_at = utc_now()
    if note:
        release.note = note
    db.add(
        ReviewLog(
            id=new_id(),
            finding_id=None,
            target_type="catalog_release",
            target_id=release.id,
            action="confirm_catalog",
            by=by.strip(),
            at=utc_now(),
            note=note or "名录已整表校对",
        )
    )
    changes = []
    if previous is not None:
        changes = diff_types(_row_dicts(db, previous.id), _row_dicts(db, release.id), _snapshots(db, release.name))
    db.add(
        CatalogDiff(
            id=new_id(),
            catalog_name=release.name,
            from_release_id=previous.id if previous else None,
            to_release_id=release.id,
            changes=changes,
            created_at=utc_now(),
        )
    )
    return {"release_id": release.id, "changes": changes}


def decide_confirmed(db: Session, *, name: str, category: str, params: dict) -> dict:
    release = _latest_confirmed(db, name)
    if release is None:
        return {
            "determination": "无法判定",
            "eia_type": None,
            "reason": "这份名录还没有整表确认，不能判断环评类型",
            "columns": [],
            "missing": [],
        }
    return decide(_row_dicts(db, release.id), category, params) | {"release_id": release.id}


def remember_snapshot(db: Session, *, name: str, label: str, category: str, params: dict) -> None:
    db.add(
        ApprovalSnapshot(
            id=new_id(),
            catalog_name=name,
            label=label.strip() or category,
            category=category,
            params=params,
            created_at=utc_now(),
        )
    )


def current_view(db: Session, name: str) -> dict | None:
    draft = db.scalar(select(CatalogRelease).where(CatalogRelease.name == name, CatalogRelease.status == "draft"))
    confirmed = _latest_confirmed(db, name)
    release = draft or confirmed
    if release is None:
        return None
    return {
        "release": _release_dict(release),
        "rows": [_public_row(row) for row in _rows(db, release.id)],
        "confirmed_release_id": confirmed.id if confirmed else None,
    }


def list_diffs(db: Session, name: str) -> list[dict]:
    rows = db.scalars(
        select(CatalogDiff).where(CatalogDiff.catalog_name == name).order_by(CatalogDiff.created_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "from_release_id": row.from_release_id,
            "to_release_id": row.to_release_id,
            "changes": row.changes,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def _latest_confirmed(db: Session, name: str) -> CatalogRelease | None:
    return db.scalar(
        select(CatalogRelease)
        .where(CatalogRelease.name == name, CatalogRelease.status == "confirmed")
        .order_by(CatalogRelease.confirmed_at.desc())
    )


def _rows(db: Session, release_id: str) -> list[CatalogRow]:
    return list(
        db.scalars(select(CatalogRow).where(CatalogRow.release_id == release_id).order_by(CatalogRow.row_index))
    )


def _row_dicts(db: Session, release_id: str) -> list[dict]:
    return [
        {
            "id": row.id,
            "category": row.category,
            "book_rule": row.book_rule,
            "form_rule": row.form_rule,
            "register_rule": row.register_rule,
        }
        for row in _rows(db, release_id)
    ]


def _snapshots(db: Session, name: str) -> list[dict]:
    rows = db.scalars(select(ApprovalSnapshot).where(ApprovalSnapshot.catalog_name == name)).all()
    return [{"label": row.label, "category": row.category, "params": row.params} for row in rows]


def _release_dict(row: CatalogRelease) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "status": row.status,
        "content_hash": row.content_hash,
        "confirmed_by": row.confirmed_by,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
    }


def _public_row(row: CatalogRow) -> dict:
    return {
        "id": row.id,
        "category": row.category,
        "book_text": row.book_text,
        "form_text": row.form_text,
        "register_text": row.register_text,
        "book_rule": row.book_rule,
        "form_rule": row.form_rule,
        "register_rule": row.register_rule,
    }
