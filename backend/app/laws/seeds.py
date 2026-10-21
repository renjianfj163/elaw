from pathlib import Path

import yaml

from app.db import repo_root


def load_seeds() -> list[dict]:
    path = Path(__file__).resolve().parents[2] / "seeds" / "instruments.yaml"
    if not path.exists():
        path = repo_root() / "backend" / "seeds" / "instruments.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("items") or [])
