from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app.db import database_url, ensure_runtime_schema, make_engine
from app.llm.api import router as llm_router
from app.local_guard import install_guard
from app.local_guard import router as guard_router
from app.models import Base
from app.public_trial import NOTICE, enabled, limits_payload, rate_detail, release_review_slot, take_review_slot
from app.schematic.api import router as schematic_router

DISCLAIMER = "辅助预审，不构成设计签核。审查项为草稿，需人工复核。"

engine = make_engine()
if database_url().startswith("sqlite"):
    Base.metadata.create_all(engine)
    ensure_runtime_schema(engine)

app = FastAPI(title="原理图审核")
app.include_router(llm_router)
app.include_router(schematic_router)
app.include_router(guard_router)


async def trial_middleware(request, call_next):
    if not enabled():
        return await call_next(request)
    detail = rate_detail(request)
    if detail:
        return JSONResponse({"detail": detail}, status_code=429)
    held = False
    if request.method == "POST" and request.url.path == "/api/schematic/review":
        held = take_review_slot()
        if not held:
            return JSONResponse({"detail": "正在给别的试用做审查，请过几分钟再试。"}, status_code=429)
    try:
        return await call_next(request)
    finally:
        if held:
            release_review_slot()


app.middleware("http")(trial_middleware)
install_guard(app)


@app.get("/")
def workbench():
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "workbench.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def health():
    body = {"ok": True, "disclaimer": DISCLAIMER, "public_trial": enabled()}
    if enabled():
        body["notice"] = NOTICE
        body["limits"] = limits_payload()
    return body
