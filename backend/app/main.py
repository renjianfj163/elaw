from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.db import database_url, ensure_runtime_schema, make_engine
from app.llm.api import router as llm_router
from app.local_guard import install_guard
from app.local_guard import router as guard_router
from app.models import Base
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
install_guard(app)


@app.get("/")
def workbench():
    return FileResponse(
        Path(__file__).resolve().parent / "static" / "workbench.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
def health():
    return {"ok": True, "disclaimer": DISCLAIMER}
