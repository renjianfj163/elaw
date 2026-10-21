import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault(
    "ELAW_CONFIG_DIR",
    str(Path(__file__).resolve().parents[2] / "config"),
)
