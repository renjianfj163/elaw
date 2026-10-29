"""上传原理图 PDF。产品定义和 Datasheet 可以不提供。审查项仍是草稿。"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.db import data_dir
from app.schematic.datasheet_fetch import (
    DatasheetFetchError,
    DatasheetStore,
    resolve_datasheets,
    store_manual,
)
from app.schematic.discovery import DiscoveryOutcome, discover_missing_rules
from app.schematic.gemini_client import GeminiCallError
from app.schematic.materials import MaterialError, extract_material
from app.schematic.pipeline import review_planned
from app.schematic.engine.model import Circuit
from app.schematic.knowledge.model import DocumentKind, KnowledgeError
from app.schematic.knowledge.review import review_with_knowledge
from app.schematic.knowledge.store import open_default_store
from app.public_trial import enabled as trial_mode
from app.public_trial import oversized_detail, release_review_slot, take_review_slot, trial_caps
from app.schematic.review import (
    MAX_BASIS_CHARS,
    MAX_PDF_BYTES,
    MAX_SCHEMATIC_FILES,
    MAX_TOTAL_PDF_BYTES,
    SchematicInputError,
)

router = APIRouter(prefix="/api/schematic", tags=["schematic"])

DISCLAIMER = "辅助预审，不构成设计签核。审查项为草稿，需人工复核。"


def _upload_caps() -> tuple[int, int, int]:
    found = trial_caps()
    if found is None:
        return MAX_SCHEMATIC_FILES, MAX_PDF_BYTES, MAX_TOTAL_PDF_BYTES
    return found


def _reject_oversized(size: int) -> None:
    detail = oversized_detail(size)
    if detail:
        raise HTTPException(status_code=400, detail=detail)


class DatasheetQuery(BaseModel):
    mpns: list[str] = Field(min_length=1, max_length=120)
    refresh: bool = False


@router.post("/extract")
async def extract(
    file: UploadFile = File(...),
    role: str = Form(...),
    read_figures: str = Form(""),
):
    name = Path(file.filename or "").name or "upload"
    payload = await file.read()
    _reject_oversized(len(payload))
    enabled = read_figures.strip().lower() in {"1", "true", "yes", "on"}
    try:
        if enabled and role == "datasheet":
            body = extract_material(name, payload, role, read_figures=False)
            import secrets

            job_id = secrets.token_urlsafe(18)
            _remember_figure(job_id, {"status": "running"})
            threading.Thread(target=_figure_worker, args=(job_id, payload), daemon=True).start()
            body["figure_job"] = job_id
            body["figure_status"] = "running"
            body["figure_note"] = "文字已经抽出。图还在识别，完成后会补进摘录。"
            return body
        return extract_material(name, payload, role, read_figures=enabled and role == "datasheet")
    except MaterialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_figure_jobs: dict[str, dict] = {}


def _remember_figure(job_id: str, payload: dict) -> None:
    with _jobs_lock:
        _figure_jobs[job_id] = payload


def _figure_worker(job_id: str, data: bytes) -> None:
    try:
        from app.schematic.datasheet_figures import quotable_figure_block
        from app.schematic.materials import _figures

        figures = _figures(data)
        _remember_figure(
            job_id,
            {
                "status": "done",
                "text_block": quotable_figure_block(figures),
                "figures": [item.model_dump() for item in figures.pages if item.text.strip()],
                "figure_pages": [item.page for item in figures.pages],
                "figure_status": figures.status,
                "figure_note": figures.note,
            },
        )
    except Exception:
        logging.exception("公开试用识图失败")
        _remember_figure(job_id, {"status": "error", "detail": "识图没有完成。文字层已经留下，可以继续审查。"})


@router.get("/extract/{job_id}")
def extract_status(job_id: str):
    with _jobs_lock:
        job = dict(_figure_jobs.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="这次识图已经不在了。文字层还在，可以继续。")
    return job


def _stage_schematics(schematic: list[UploadFile], max_files: int, max_pdf: int, max_total: int) -> tuple[Path, list[Path], list[str]]:
    if not schematic:
        raise HTTPException(status_code=400, detail="请上传原理图 PDF")
    if len(schematic) > max_files:
        raise HTTPException(status_code=400, detail=f"原理图一次最多 {max_files} 份")
    folder = Path(tempfile.mkdtemp(prefix="schematic-"))
    paths: list[Path] = []
    names: list[str] = []
    try:
        total = 0
        for index, item in enumerate(schematic):
            name = Path(item.filename or "").name or f"schematic-{index}.pdf"
            if Path(name).suffix.lower() != ".pdf":
                raise HTTPException(status_code=400, detail="原理图需要是 PDF")
            payload = item.file.read()
            if not payload:
                raise HTTPException(status_code=400, detail="原理图 PDF 是空的")
            if len(payload) > max_pdf:
                raise HTTPException(status_code=400, detail=f"有一份原理图超过 {max_pdf // (1024 * 1024)}MB")
            total += len(payload)
            if total > max_total:
                raise HTTPException(status_code=400, detail=f"原理图合计超过 {max_total // (1024 * 1024)}MB")
            safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip()[:80]
            if not safe.lower().endswith(".pdf"):
                safe = f"{safe or 'schematic'}.pdf"
            dest = folder / f"{index:03d}-{safe}"
            dest.write_bytes(payload)
            paths.append(dest)
            names.append(name)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder, paths, names


def finish_review(paths: list[Path], names: list[str], product_spec_text: str, datasheet_text: str) -> dict:
    result = review_planned(paths, product_spec_text, datasheet_text)
    try:
        found = discover_missing_rules(paths)
    except Exception:
        found = DiscoveryOutcome(note="规则建议没有生成")
    body = result.model_dump()
    body["filename"] = "、".join(names)
    note = " ".join(part for part in (result.basis_note.strip(), result.coverage_note.strip()) if part)
    body["disclaimer"] = f"{note}{DISCLAIMER}" if note else DISCLAIMER
    body["pending_rules"] = [item.model_dump() for item in found.rules]
    body["discovery_note"] = found.note
    return body


_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _remember_job(job_id: str, payload: dict) -> None:
    with _jobs_lock:
        _jobs[job_id] = payload


def _public_review_job(job_id: str, folder: Path, paths: list[Path], names: list[str], product_spec_text: str, datasheet_text: str) -> None:
    try:
        body = finish_review(paths, names, product_spec_text, datasheet_text)
        _remember_job(job_id, {"status": "done", "result": body})
    except SchematicInputError as exc:
        _remember_job(job_id, {"status": "error", "detail": str(exc)})
    except GeminiCallError as exc:
        _remember_job(job_id, {"status": "error", "detail": str(exc)})
    except Exception:
        logging.exception("公开试用审查失败")
        _remember_job(job_id, {"status": "error", "detail": "审查没有完成。"})
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        release_review_slot()


@router.post("/review")
async def review(
    schematic: list[UploadFile] = File(...),
    product_spec_text: str = Form(""),
    datasheet_text: str = Form(""),
):
    max_files, max_pdf, max_total = _upload_caps()
    folder, paths, names = _stage_schematics(list(schematic), max_files, max_pdf, max_total)
    if trial_mode():
        if not take_review_slot():
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(status_code=429, detail="正在给别的试用做审查，请过几分钟再试。")
        import secrets

        job_id = secrets.token_urlsafe(24)
        _remember_job(job_id, {"status": "running"})
        threading.Thread(
            target=_public_review_job,
            args=(job_id, folder, paths, names, product_spec_text, datasheet_text),
            daemon=True,
        ).start()
        return {"job_id": job_id, "status": "running"}
    try:
        return finish_review(paths, names, product_spec_text, datasheet_text)
    except SchematicInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GeminiCallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.get("/review/{job_id}")
def review_status(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id) or {})
    if not job:
        raise HTTPException(status_code=404, detail="这次审查已经不在了。请重新生成。")
    return job


def _sheet_store() -> DatasheetStore:
    return DatasheetStore(data_dir() / "datasheets")


@router.post("/datasheets/resolve")
def resolve_sheet(query: DatasheetQuery):
    items = resolve_datasheets(query.mpns, refresh=query.refresh)
    return {"items": [item.model_dump() for item in items]}


@router.get("/datasheets/file/{digest}")
def datasheet_file(digest: str):
    path = _sheet_store().file_for(digest)
    if path is None:
        raise HTTPException(status_code=404, detail="没有这份手册")
    return FileResponse(path, media_type="application/pdf", filename=f"{digest}.pdf", content_disposition_type="inline")


@router.get("/datasheets/text/{digest}")
def datasheet_text(digest: str):
    try:
        text = _sheet_store().text_for(digest)
    except DatasheetFetchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"text": text, "chars": len(text), "limit": MAX_BASIS_CHARS}


@router.post("/datasheets/upload")
async def upload_datasheet(mpn: str = Form(...), file: UploadFile = File(...)):
    from app.llm.provider import load_llm_config

    data = await file.read()
    _reject_oversized(len(data))
    allow = bool(load_llm_config().get("outbound_allowed"))
    verifier = None
    if allow:
        from app.schematic.datasheet_fetch import gemini_datasheet_verifier

        verifier = gemini_datasheet_verifier()
    try:
        match = store_manual(mpn, data, verifier=verifier, allow_verify=allow)
    except DatasheetFetchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return match.model_dump()


@router.post("/knowledge/import")
async def import_knowledge(
    title: str = Form(...),
    kind: str = Form("datasheet"),
    revision: str = Form(""),
    project_id: str = Form(""),
    product_id: str = Form(""),
    text: str = Form(""),
    file: UploadFile | None = File(None),
):
    from typing import get_args

    if kind not in get_args(DocumentKind):
        raise HTTPException(status_code=400, detail="资料类型无法识别")
    store = open_default_store()
    filename = Path(file.filename or "").name if file is not None else ""
    payload = await file.read() if file is not None else b""
    _reject_oversized(len(payload))
    try:
        if payload and filename.lower().endswith(".pdf"):
            from app.schematic.datasheet_figures import (
                figure_reader_or_none,
                quotable_figure_block,
                read_datasheet_figures,
            )

            reader, reason = figure_reader_or_none()
            figures = read_datasheet_figures(payload, reader=reader, skip_reason=reason)
            document = store.import_pdf(
                title=title,
                payload=payload,
                kind=kind,  # type: ignore[arg-type]
                revision=revision,
                project_id=project_id,
                product_id=product_id,
                filename=filename,
                ocr_text=quotable_figure_block(figures),
                ocr_note=figures.note,
                ocr_pages=[item.page for item in figures.pages],
            )
        else:
            body = text
            if payload:
                body = payload.decode("utf-8")
            if not body.strip():
                raise HTTPException(status_code=400, detail="没有可读取的正文")
            document = store.import_text(
                title=title,
                text=body,
                kind=kind,  # type: ignore[arg-type]
                revision=revision,
                project_id=project_id,
                product_id=product_id,
                filename=filename,
            )
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    constraints = store.query(document_id=document.document_id)
    return {
        "document": document.model_dump(),
        "constraints": [item.model_dump() for item in constraints],
    }


@router.get("/knowledge/constraints")
def list_knowledge_constraints(mpn: str = "", category: str = "", verified_only: bool = False):
    store = open_default_store()
    constraints = store.query(mpn=mpn, category=category, verified_only=verified_only)
    return {
        "documents": [item.model_dump() for item in store.documents.values()],
        "constraints": [item.model_dump() for item in constraints],
        "conflicts": [item.model_dump() for item in store.open_conflicts()],
    }


@router.post("/knowledge/constraints/{constraint_id}/verify")
def verify_knowledge_constraint(constraint_id: str):
    try:
        item = open_default_store().verify(constraint_id)
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return item.model_dump()


@router.post("/knowledge/review")
def review_knowledge(circuit: Circuit):
    report = review_with_knowledge(circuit, open_default_store())
    return report.model_dump()
