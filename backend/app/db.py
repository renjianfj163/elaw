import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    raw = os.environ.get("DATA_DIR")
    path = Path(raw) if raw else repo_root() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_url() -> str:
    return os.environ.get("DATABASE_URL", f"sqlite:///{(data_dir() / 'elaw.db').as_posix()}")


def make_engine(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def open_session(url: str | None = None):
    engine = make_engine(url)
    if (url or database_url()).startswith("sqlite"):
        from app.models import Base

        Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def ensure_runtime_schema(engine) -> None:
    """已有的 sqlite 库补上后加的列。新库由 create_all 建齐。"""
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(eia_report)").fetchall()
        if not rows:
            return
        names = {row[1] for row in rows}
        if "input_key" not in names:
            conn.exec_driver_sql("ALTER TABLE eia_report ADD COLUMN input_key VARCHAR(64)")
