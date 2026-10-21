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
