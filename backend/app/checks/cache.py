"""把模型输出按 cache_key 落在 llm_cache。思考模式的请求本身不带 temperature。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.hashing import utc_now
from app.models import LlmCache


class DbCache:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.model = ""
        self.prompt_version = ""

    def get(self, key: str):
        row = self.db.get(LlmCache, key)
        if row is None:
            return None
        return row.response_json

    def put(self, key: str, value: dict) -> None:
        if self.db.get(LlmCache, key) is not None:
            return
        self.db.add(
            LlmCache(
                cache_key=key,
                model=self.model,
                prompt_version=self.prompt_version,
                response_json=value,
                created_at=utc_now(),
            )
        )
        self.db.flush()
