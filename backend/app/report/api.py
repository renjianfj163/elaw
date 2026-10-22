"""上传一份或多份环评报告并做引用核验。按顺序解析，不调用模型。"""

import json
import re
from datetime import date

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pathlib import Path
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.basis.store import evaluable_clauses
from app.citation.check import (
    SCOPE_NOTE,
    UNCOVERED,
    BasisClauseView,
    admissible_clauses,
    basis_fingerprint,
    check_report,
    review_input_key,
)
from app.citation.limits import limits_fingerprint, load_verified_limits
from app.citation.place import describe_place
from app.citation.timeliness import load_repeal_map, repeal_fingerprint
from app.db import data_dir
from app.hashing import new_id, sha256_bytes, sha256_text, utc_now
from app.models import EiaReport, ReviewLog
from app.parse.cli import parse_report
from app.parse.convert import ConvertError
from app.parse.docx_walk import PARSER_VERSION, ParseError
from app.parse.pdf_text import PDF_PARSER_VERSION

router = APIRouter(prefix="/api/eia", tags=["eia"])
DISCLAIMER = "辅助预审，不构成法律意见。"
_ALLOWED = {".doc", ".docx", ".pdf"}
_MAX_FILES = 30
_MAX_BYTES = 40 * 1024 * 1024
_PREVIEW = 480
_STAMP = re.compile(r"(20\d{2})(\d{2})(\d{2})")
_VERDICTS = {
    "符合",
    "不符合",
    "不适用",
    "无法判定",
    "无法核对",
    "超出依据范围",
    "引用已失效",
    "有效",
    "与依据一致",
    "不一致",
    "未超限值",
    "超过限值",
    "报告内部自洽",
    "报告内部不自洽",
    "内部自洽",
    "内部不自洽",
    "在依据库中",
    "尚未核验",
    "基准日有效",
    "基准日已废止",
    "编制时有效 / 基准日有效",
    "编制时有效 / 基准日已废止",
    "编制时已废止 / 基准日已废止",
    "编制时已废止 / 基准日有效",
    "非规范性文件，跳过",
}


class FindingReviewIn(BaseModel):
    key: str = Field(min_length=8, max_length=64)
    action: str
    by: str
    verdict: str = ""
    note: str = ""


@router.get("/reports")
def list_reports():
    from app.main import SessionFactory

    db = SessionFactory()
    try:
        rows = db.scalars(select(EiaReport).order_by(EiaReport.created_at.desc()).limit(50)).all()
        for row in rows:
            _fill_places(db, row)
        grouped = _reviews_for(db, [row.id for row in rows])
        return {"disclaimer": DISCLAIMER, "items": [_public(row, grouped.get(row.id, [])) for row in rows]}
    finally:
        db.close()


@router.delete("/reports/{report_id}")
def delete_report(report_id: str):
    """删掉一份已核查结果。同一份原文若还有别的结果，文件先留着。"""
    from app.main import SessionFactory

    db = SessionFactory()
    try:
        row = db.get(EiaReport, report_id)
        if row is None:
            raise HTTPException(status_code=404, detail="找不到这份核查结果")
        digest = row.file_sha256
        db.execute(
            delete(ReviewLog).where(
                ReviewLog.target_type == "eia_finding",
                ReviewLog.target_id == row.id,
            )
        )
        db.delete(row)
        db.commit()
        still_used = db.scalar(select(EiaReport.id).where(EiaReport.file_sha256 == digest))
    finally:
        db.close()
    if still_used is None:
        _remove_stored_file(digest)
    return {"ok": True, "id": report_id}


@router.post("/reports/{report_id}/review")
def review_finding(report_id: str, body: FindingReviewIn):
    """记下对一条结论的确认或改判。不改原结论，也不改 result_hash。"""
    action = body.action.strip()
    if action not in {"confirm", "override"}:
        raise HTTPException(status_code=400, detail="操作只能是确认或改判")
    reviewer = body.by.strip()
    if not reviewer:
        raise HTTPException(status_code=400, detail="请填写审核人姓名")
    verdict = body.verdict.strip()
    reason = body.note.strip()
    if action == "override":
        if verdict not in _VERDICTS:
            raise HTTPException(status_code=400, detail="改判后的结论不在允许的范围内")
        if not reason:
            raise HTTPException(status_code=400, detail="改判要写明理由")
    from app.main import SessionFactory

    db = SessionFactory()
    try:
        row = db.get(EiaReport, report_id)
        if row is None:
            raise HTTPException(status_code=404, detail="找不到这份核查结果")
        if not any(_finding_key(item) == body.key for item in (row.findings or [])):
            raise HTTPException(status_code=404, detail="找不到这条结论")
        db.add(
            ReviewLog(
                id=new_id(),
                finding_id=body.key,
                target_type="eia_finding",
                target_id=row.id,
                action=action,
                by=reviewer,
                at=utc_now(),
                note=json.dumps(
                    {"verdict": verdict if action == "override" else "", "reason": reason},
                    ensure_ascii=False,
                ),
            )
        )
        db.commit()
        reviews = _reviews_for(db, [row.id]).get(row.id, [])
        return {"id": row.id, "result_hash": row.result_hash, "reviews": reviews}
    finally:
        db.close()


@router.post("/check")
async def check_uploaded_reports(
    files: list[UploadFile] = File(...),
    as_of: str = Form("2026-10-21"),
    compiled_on: str = Form(""),
):
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一份报告")
    if len(files) > _MAX_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多 {_MAX_FILES} 份，可以分几次上传")
    day = _parse_day(as_of, "基准日")
    compiled = _parse_day(compiled_on, "编制日") if compiled_on.strip() else None
    clauses = _load_clauses(day)
    repeal_rows = load_repeal_map()
    limits = load_verified_limits()
    active = admissible_clauses(clauses, day, repeal_rows)
    items = []
    for upload in files:
        items.append(await _one(upload, day, compiled, active, repeal_rows, limits))
    return {
        "disclaimer": DISCLAIMER,
        "as_of": day.isoformat(),
        "compiled_on": compiled.isoformat() if compiled else "",
        "basis_in_force": len(active),
        "scope_note": SCOPE_NOTE,
        "items": items,
    }


async def _one(upload, day, compiled, clauses, repeal_rows, limits) -> dict:
    name = Path(upload.filename or "").name or "未命名"
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED:
        return {"ok": False, "filename": name, "error": "只接受 .doc、.docx 或 .pdf"}
    payload = await upload.read()
    if not payload:
        return {"ok": False, "filename": name, "error": "文件是空的"}
    if len(payload) > _MAX_BYTES:
        return {"ok": False, "filename": name, "error": "文件超过 40MB"}
    digest = sha256_bytes(payload)
    parser_version = PDF_PARSER_VERSION if suffix == ".pdf" else PARSER_VERSION
    used_compiled = compiled or compiled_from_filename(name)
    compiled_from = "form" if compiled else ("filename" if used_compiled else "")
    key = review_input_key(
        report_sha=digest,
        basis_sha=basis_fingerprint(clauses),
        repeal_sha=repeal_fingerprint(repeal_rows),
        limits_sha=limits_fingerprint(limits),
        as_of=day,
        compiled_on=used_compiled,
        parser_version=parser_version,
    )
    cached = _stored(key)
    if cached is not None:
        cached["filename"] = name
        cached["reused"] = True
        return cached
    folder = data_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{digest}{suffix}"
    stored.write_bytes(payload)
    try:
        parsed = parse_report(stored)
    except (ConvertError, ParseError) as exc:
        return {"ok": False, "filename": name, "error": str(exc)}
    result = check_report(
        parsed,
        as_of=day,
        clauses=clauses,
        repeal_rows=repeal_rows,
        dose_limits=limits,
        compiled_on=used_compiled,
    )
    body = _result_body(name, day, len(clauses), parsed, result, used_compiled, compiled_from)
    body["reused"] = False
    body["input_key"] = key
    body["id"] = _save(digest, day, key, body)
    body["reviews"] = []
    return body


def _result_body(name: str, day: date, basis_count: int, parsed, result, compiled: date | None = None, compiled_from: str = "") -> dict:
    findings = _preview(result.findings)
    counts: dict[str, int] = {}
    for item in result.findings:
        key = f"{item.kind}:{item.verdict}"
        counts[key] = counts.get(key, 0) + 1
    stats = parsed.stats
    return {
        "ok": True,
        "filename": name,
        "as_of": day.isoformat(),
        "basis_in_force": basis_count,
        "result_hash": result.result_hash,
        "rejected": len(result.rejected),
        "scope_note": result.scope_note or SCOPE_NOTE,
        "coverage": result.coverage,
        "uncovered": list(result.uncovered or UNCOVERED),
        "compiled_on": compiled.isoformat() if compiled else "",
        "compiled_from": compiled_from,
        "stats": {
            "blocks": len(parsed.blocks),
            "tables": stats.table_count,
            "top_level_tables": stats.top_level_table_count,
            "nested_tables": stats.nested_table_count,
            "images": stats.image_block_count,
            "parse_note": parsed.parse_note,
            "scope_note": result.scope_note or SCOPE_NOTE,
            "coverage": result.coverage,
            "uncovered": list(result.uncovered or UNCOVERED),
            "compiled_on": compiled.isoformat() if compiled else "",
            "compiled_from": compiled_from,
        },
        "counts": counts,
        "shown": len(findings),
        "total": len(result.findings),
        "findings": findings,
    }


def _save(digest: str, day: date, key: str, body: dict) -> str:
    from app.main import SessionFactory

    db = SessionFactory()
    try:
        existing = _find_key(db, key)
        if existing is not None:
            return existing.id
        report_id = new_id()
        db.add(
            EiaReport(
                id=report_id,
                filename=body["filename"],
                file_sha256=digest,
                as_of=day,
                input_key=key,
                result_hash=body["result_hash"],
                basis_in_force=body["basis_in_force"],
                rejected=body["rejected"],
                total=body["total"],
                shown=body["shown"],
                stats=body["stats"],
                counts=body["counts"],
                findings=body["findings"],
                created_at=utc_now(),
            )
        )
        db.commit()
        return report_id
    finally:
        db.close()


def _stored(key: str) -> dict | None:
    from app.main import SessionFactory

    db = SessionFactory()
    try:
        row = _find_key(db, key)
        if row is None:
            return None
        _fill_places(db, row)
        reviews = _reviews_for(db, [row.id]).get(row.id, [])
        return _public(row, reviews)
    finally:
        db.close()


def _report_file(digest: str) -> Path | None:
    folder = data_dir() / "reports"
    for suffix in _ALLOWED:
        path = folder / f"{digest}{suffix}"
        if path.is_file():
            return path
    return None


def _fill_places(db, row: EiaReport) -> None:
    """旧结果只有字符偏移。按原文补上章节或表题，不改 result_hash。"""
    findings = list(row.findings or [])
    if not findings or all("place" in (item or {}) for item in findings):
        return
    path = _report_file(row.file_sha256)
    if path is None:
        return
    try:
        parsed = parse_report(path)
    except (ConvertError, ParseError, OSError):
        return
    updated = []
    changed = False
    for item in findings:
        item = dict(item or {})
        if "place" not in item:
            item["place"] = describe_place(parsed.blocks, int(item.get("char_start") or 0))
            changed = True
        updated.append(item)
    if changed:
        row.findings = updated
        db.commit()


def _remove_stored_file(digest: str) -> None:
    folder = data_dir() / "reports"
    for suffix in _ALLOWED:
        path = folder / f"{digest}{suffix}"
        if path.is_file():
            path.unlink()


def _find_key(db, key: str) -> EiaReport | None:
    if not key:
        return None
    return db.scalar(select(EiaReport).where(EiaReport.input_key == key))


def _public(row: EiaReport, reviews: list | None = None) -> dict:
    return {
        "ok": True,
        "id": row.id,
        "filename": row.filename,
        "as_of": row.as_of.isoformat(),
        "basis_in_force": row.basis_in_force,
        "scope_note": (row.stats or {}).get("scope_note") or SCOPE_NOTE,
        "coverage": (row.stats or {}).get("coverage") or {},
        "uncovered": (row.stats or {}).get("uncovered") or list(UNCOVERED),
        "compiled_on": (row.stats or {}).get("compiled_on") or "",
        "compiled_from": (row.stats or {}).get("compiled_from") or "",
        "result_hash": row.result_hash,
        "rejected": row.rejected,
        "stats": row.stats,
        "counts": row.counts,
        "shown": row.shown,
        "total": row.total,
        "findings": [_with_key(item) for item in (row.findings or [])],
        "reviews": reviews or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _reviews_for(db, report_ids: list[str]) -> dict[str, list]:
    if not report_ids:
        return {}
    rows = db.scalars(
        select(ReviewLog)
        .where(ReviewLog.target_type == "eia_finding", ReviewLog.target_id.in_(report_ids))
        .order_by(ReviewLog.at.asc())
    ).all()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.target_id, []).append(_review_public(row))
    return grouped


def _review_public(row: ReviewLog) -> dict:
    verdict = ""
    reason = row.note or ""
    if reason.startswith("{"):
        try:
            parsed = json.loads(reason)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            verdict = str(parsed.get("verdict") or "")
            reason = str(parsed.get("reason") or "")
    return {
        "key": row.finding_id or "",
        "action": row.action,
        "by": row.by,
        "at": row.at.isoformat() if row.at else None,
        "verdict": verdict,
        "note": reason,
    }


def _with_key(item: dict) -> dict:
    row = dict(item)
    row["key"] = _finding_key(row)
    return row


def _finding_key(item) -> str:
    if isinstance(item, dict):
        parts = (
            item.get("kind") or "",
            item.get("char_start") or 0,
            item.get("char_end") or 0,
            item.get("verdict") or "",
            item.get("question") or "",
        )
    else:
        parts = (item.kind, item.char_start, item.char_end, item.verdict, item.question)
    return sha256_text("|".join(str(part) for part in parts))[:32]


def _load_clauses(as_of: date) -> list[BasisClauseView]:
    from app.main import SessionFactory

    db = SessionFactory()
    try:
        rows = []
        for document, clause in evaluable_clauses(db, as_of):
            rows.append(
                BasisClauseView(
                    doc_id=document.doc_id,
                    title=document.title,
                    path=clause.path,
                    text=clause.text,
                    text_sha256=clause.text_sha256,
                    effective_date=document.effective_date,
                    verified=True,
                    status=document.status,
                )
            )
        return rows
    finally:
        db.close()


def _clip(text: str) -> str:
    text = text or ""
    if len(text) <= _PREVIEW:
        return text
    return text[:_PREVIEW] + "…"


def _preview(findings) -> list[dict]:
    ordered = sorted(findings, key=lambda item: (0 if item.kind == "timeliness" else 1, item.char_start, item.verdict))
    shown = []
    for item in ordered[:120]:
        row = {
            "kind": item.kind,
            "verdict": item.verdict,
            "question": item.question,
            "evidence_span": _clip(item.evidence_span or ""),
            "quote": _clip(getattr(item, "quote", "") or ""),
            "char_start": item.char_start,
            "char_end": getattr(item, "char_end", 0) or 0,
            "source_label": item.source_label,
            "replaced_by": item.replaced_by,
            "basis_doc_id": getattr(item, "basis_doc_id", None),
            "basis_path": getattr(item, "basis_path", None) or "",
            "section_path": getattr(item, "section_path", "") or "",
            "place": getattr(item, "place", "") or "",
            "section": getattr(item, "section", "") or "",
            "reason": getattr(item, "reason", "") or "",
            "dual_review": getattr(item, "dual_review", "") or "",
            "needs_review": bool(getattr(item, "needs_review", False)),
            "review_note": getattr(item, "review_note", "") or "",
        }
        row["key"] = _finding_key(row)
        shown.append(row)
    return shown


def compiled_from_filename(name: str) -> date | None:
    match = _STAMP.search(Path(name).stem)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _parse_day(value: str, label: str) -> date:
    text = (value or "").strip()
    try:
        year, month, day = (int(part) for part in text[:10].split("-"))
        return date(year, month, day)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label}请用 YYYY-MM-DD") from exc
