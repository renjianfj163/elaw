"""从版本化的 JSON 目录加载规则。规则原文和阈值不写在检查器里。"""

from __future__ import annotations

import json
from pathlib import Path

from app.db import repo_root
from app.schematic.engine.report import Rule


def catalog_path() -> Path:
    return repo_root() / "data" / "schematic_rules" / "engine_catalog.json"


def load_catalog(path: Path | None = None) -> tuple[str, list[Rule]]:
    file = path or catalog_path()
    payload = json.loads(file.read_text(encoding="utf-8"))
    rules = [Rule.model_validate(item) for item in payload.get("rules") or []]
    return str(payload.get("version") or ""), rules
