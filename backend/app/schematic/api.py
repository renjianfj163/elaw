"""上传原理图 PDF、产品定义和 Datasheet，先规划裁剪再返回 draft 审查项。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.schematic.gemini_client import GeminiCallError
from app.schematic.materials import MaterialError, extract_material
from app.schematic.pipeline import review_planned
from app.schematic.review import SchematicInputError

router = APIRouter(prefix="/api/schematic", tags=["schematic"])

DISCLAIMER = "辅助预审，不构成设计签核。审查项为草稿，需人工复核。"


@router.post("/extract")
async def extract(file: UploadFile = File(...), role: str = Form(...)):
    name = Path(file.filename or "").name or "upload"
    payload = await file.read()
    try:
        return extract_material(name, payload, role)
    except MaterialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/review")
async def review(
    schematic: UploadFile = File(...),
    product_spec_text: str = Form(...),
    datasheet_text: str = Form(...),
):
    name = Path(schematic.filename or "").name or "schematic.pdf"
    if Path(name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="原理图需要是 PDF")
    payload = await schematic.read()
    if not payload:
        raise HTTPException(status_code=400, detail="原理图 PDF 是空的")
    try:
        with tempfile.TemporaryDirectory(prefix="schematic-") as folder:
            path = Path(folder) / "schematic.pdf"
            path.write_bytes(payload)
            result = review_planned(path, product_spec_text, datasheet_text)
    except SchematicInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GeminiCallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    body = result.model_dump()
    body["filename"] = name
    body["disclaimer"] = DISCLAIMER
    return body
