"""上传原理图 PDF。产品定义和 Datasheet 可以不提供。审查项仍是草稿。"""

from __future__ import annotations

import tempfile
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
from app.schematic.review import (
    MAX_BASIS_CHARS,
    MAX_PDF_BYTES,
    MAX_SCHEMATIC_FILES,
    MAX_TOTAL_PDF_BYTES,
    SchematicInputError,
)

router = APIRouter(prefix="/api/schematic", tags=["schematic"])

DISCLAIMER = "辅助预审，不构成设计签核。审查项为草稿，需人工复核。"


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
    enabled = read_figures.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return extract_material(name, payload, role, read_figures=enabled and role == "datasheet")
    except MaterialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/review")
async def review(
    schematic: list[UploadFile] = File(...),
    product_spec_text: str = Form(""),
    datasheet_text: str = Form(""),
):
    if not schematic:
        raise HTTPException(status_code=400, detail="请上传原理图 PDF")
    if len(schematic) > MAX_SCHEMATIC_FILES:
        raise HTTPException(status_code=400, detail=f"原理图一次最多 {MAX_SCHEMATIC_FILES} 份")
    names: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="schematic-") as folder:
            root = Path(folder)
            paths: list[Path] = []
            total = 0
            for index, item in enumerate(schematic):
                name = Path(item.filename or "").name or f"schematic-{index}.pdf"
                if Path(name).suffix.lower() != ".pdf":
                    raise HTTPException(status_code=400, detail="原理图需要是 PDF")
                payload = await item.read()
                if not payload:
                    raise HTTPException(status_code=400, detail="原理图 PDF 是空的")
                if len(payload) > MAX_PDF_BYTES:
                    raise HTTPException(status_code=400, detail="有一份原理图超过 40MB")
                total += len(payload)
                if total > MAX_TOTAL_PDF_BYTES:
                    raise HTTPException(status_code=400, detail="原理图合计超过 500MB")
                safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip()[:80]
                if not safe.lower().endswith(".pdf"):
                    safe = f"{safe or 'schematic'}.pdf"
                dest = root / f"{index:03d}-{safe}"
                dest.write_bytes(payload)
                paths.append(dest)
                names.append(name)
            result = review_planned(paths, product_spec_text, datasheet_text)
            try:
                found = discover_missing_rules(paths)
            except Exception:
                found = DiscoveryOutcome(note="规则建议没有生成")
    except SchematicInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GeminiCallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = result.model_dump()
    body["filename"] = "、".join(names)
    note = " ".join(part for part in (result.basis_note.strip(), result.coverage_note.strip()) if part)
    body["disclaimer"] = f"{note}{DISCLAIMER}" if note else DISCLAIMER
    body["pending_rules"] = [item.model_dump() for item in found.rules]
    body["discovery_note"] = found.note
    return body


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
