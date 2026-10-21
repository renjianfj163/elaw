from datetime import date

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.basis.api import router as basis_router
from app.checks.api import router as check_router
from app.report.api import router as report_router
from app.db import database_url, make_engine, make_session_factory
from app.laws.catalog import catalog_csv, parse_catalog
from app.laws.collect import run_collection
from app.laws.extract import TextExtractError, extract_text
from app.laws.http import HttpxClient
from app.laws.seeds import load_seeds
from app.laws.store import add_report, add_version, confirm_version
from app.llm.api import router as llm_router
from app.llm.provider import load_local_config
from app.models import Base, LawVersion, VerificationReport
from app.scope import judge_scope

DISCLAIMER = "辅助预审，不构成法律意见。"

engine = make_engine()
SessionFactory = make_session_factory(engine)
if database_url().startswith("sqlite"):
    Base.metadata.create_all(engine)

app = FastAPI(title="环评合规预审")
app.include_router(basis_router)
app.include_router(check_router)
app.include_router(report_router)
app.include_router(llm_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CollectIn(BaseModel):
    names: list[str] | None = None


class ConfirmIn(BaseModel):
    status: str
    by: str = ""
    publish_date: date | None = None
    effective_date: date | None = None
    note: str = ""


class CatalogIn(BaseModel):
    text: str


def _session():
    return SessionFactory()


@app.get("/")
def workbench():
    page = __file__
    from pathlib import Path

    return FileResponse(
        Path(page).resolve().parent / "static" / "workbench.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def health():
    return {"ok": True, "disclaimer": DISCLAIMER}


@app.get("/api/seeds")
def seeds():
    return {"items": load_seeds(), "disclaimer": DISCLAIMER}


@app.get("/api/scope")
def scope(topic: str):
    return {"topic": topic, "result": judge_scope(topic)}


@app.get("/api/reports")
def reports():
    db = _session()
    try:
        rows = db.scalars(select(VerificationReport).order_by(VerificationReport.created_at.desc())).all()
        shown = rows[:200]
        version_ids = [row.version_id for row in shown if row.version_id]
        versions = {}
        if version_ids:
            found = db.scalars(select(LawVersion).where(LawVersion.id.in_(version_ids))).all()
            versions = {item.id: item for item in found}
        return {"items": [_report_dict(row, versions.get(row.version_id)) for row in shown]}
    finally:
        db.close()


@app.post("/api/laws/collect")
def collect(body: CollectIn):
    db = _session()
    try:
        items = run_collection(db, HttpxClient(), body.names)
        return {"items": items, "disclaimer": DISCLAIMER}
    finally:
        db.close()


@app.post("/api/laws/upload")
async def upload(
    file: UploadFile = File(...),
    name: str = Form(...),
    level: str = Form(...),
    status: str = Form("有效"),
    confirm: str = Form("false"),
    by: str = Form(""),
    publish_date: str = Form(""),
    effective_date: str = Form(""),
):
    data = await file.read()
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413, "文件超过 30MB")
    try:
        text = extract_text(file.filename or "upload.txt", data)
    except TextExtractError as exc:
        raise HTTPException(400, str(exc)) from exc
    confirmed = str(confirm).lower() in {"1", "true", "on", "yes"}
    reviewer = _reviewer(by)
    if confirmed and not reviewer:
        raise HTTPException(400, "勾选核对时要填写审核人姓名")
    if confirmed and status not in {"有效", "已修订", "已废止", "未生效"}:
        raise HTTPException(400, "核对后的状态不正确")
    db = _session()
    try:
        version = add_version(
            db,
            name=name.strip(),
            level=level.strip(),
            source="manual",
            text=text,
            status=status if confirmed else "待确认",
            publish_date=_parse_date(publish_date),
            effective_date=_parse_date(effective_date),
            file_bytes=data,
            filename=file.filename or "upload.txt",
            verified_by=reviewer if confirmed else None,
            verified_at=utc_now() if confirmed else None,
        )
        if confirmed:
            from app.hashing import new_id, utc_now
            from app.models import ReviewLog

            db.add(
                ReviewLog(
                    id=new_id(),
                    finding_id=None,
                    target_type="law_version",
                    target_id=version.id,
                    action="upload_confirm",
                    by=reviewer,
                    at=utc_now(),
                    note="上传时确认已对照官网",
                )
            )
        outcome = {
            "seed_name": name.strip(),
            "source": "manual",
            "outcome": "找到",
            "version_label": name.strip(),
            "publish_date": _parse_date(publish_date),
            "effective_date": _parse_date(effective_date),
            "raw_status": None,
            "superseded_note": None,
            "fulltext": "已获取",
            "detail": {"confirmed": confirmed},
        }
        report = add_report(db, outcome, level=level, version_id=version.id)
        db.commit()
        return {"version_id": version.id, "report_id": report.id, "status": version.status}
    finally:
        db.close()


@app.post("/api/laws/versions/{version_id}/confirm")
def confirm(version_id: str, body: ConfirmIn):
    reviewer = _reviewer(body.by)
    if not reviewer:
        raise HTTPException(400, "请填写审核人姓名")
    db = _session()
    try:
        version = confirm_version(
            db,
            version_id=version_id,
            status=body.status,
            by=reviewer,
            publish_date=body.publish_date,
            effective_date=body.effective_date,
            note=body.note,
        )
        db.commit()
        return {"version_id": version.id, "status": version.status, "verified_by": version.verified_by}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    finally:
        db.close()


@app.post("/api/catalog/parse")
def catalog(body: CatalogIn):
    rows = parse_catalog(body.text)
    return {"rows": rows, "csv": catalog_csv(rows), "needs_human_review": True}


def _reviewer(given: str) -> str:
    text = (given or "").strip()
    if text:
        return text
    return str(load_local_config().get("reviewer_name") or "").strip()


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    year, month, day = (int(part) for part in value[:10].split("-"))
    return date(year, month, day)


def _report_dict(row: VerificationReport, version: LawVersion | None = None) -> dict:
    return {
        "id": row.id,
        "seed_name": row.seed_name,
        "source": row.source,
        "outcome": row.outcome,
        "fulltext": row.fulltext,
        "version_id": row.version_id,
        "version_status": version.status if version else None,
        "verified_by": version.verified_by if version else None,
        "publish_date": row.publish_date.isoformat() if row.publish_date else None,
        "effective_date": row.effective_date.isoformat() if row.effective_date else None,
        "raw_status": row.raw_status,
        "superseded_note": row.superseded_note,
        "detail": row.detail,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _touch_data_dir() -> None:
    if database_url().startswith("sqlite"):
        data_dir()
