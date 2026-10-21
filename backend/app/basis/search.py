"""只在已核验、已施行且未停用的条款上做 BM25。"""

from __future__ import annotations

import logging
from datetime import date

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session

from app.basis.store import evaluable_clauses

jieba.setLogLevel(logging.ERROR)


def search_basis(db: Session, query: str, as_of: date, limit: int = 8) -> list[dict]:
    query = query.strip()
    if not query or limit <= 0:
        return []
    rows = evaluable_clauses(db, as_of)
    if not rows:
        return []
    tokenized = [_tokens(clause.text) for _, clause in rows]
    if not any(tokenized):
        return []
    scores = BM25Okapi(tokenized).get_scores(_tokens(query))
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    hits = []
    for index, score in ranked:
        if score == 0:
            continue
        document, clause = rows[index]
        hits.append(
            {
                "doc_id": document.doc_id,
                "title": document.title,
                "kind": document.kind,
                "path": clause.path,
                "text": clause.text,
                "text_sha256": clause.text_sha256,
                "score": float(score),
            }
        )
        if len(hits) >= limit:
            break
    return hits


def _tokens(text: str) -> list[str]:
    return [token.strip() for token in jieba.lcut(text) if token.strip()]
