"""按检查项的定位词取报告片段。模型只看这些片段，不看全文。"""

from __future__ import annotations

import logging

import jieba
from rank_bm25 import BM25Okapi

jieba.setLogLevel(logging.ERROR)


def retrieve_blocks(blocks, topics: list[str], limit: int = 6) -> list:
    topics = [topic.strip() for topic in topics if topic and topic.strip()]
    if not topics or limit <= 0:
        return []
    indexed = []
    tokenized = []
    for block in blocks:
        if getattr(block, "type", "") == "image" or getattr(block, "not_parsed", False):
            continue
        text = getattr(block, "text", "") or ""
        if not text.strip():
            continue
        indexed.append(block)
        tokenized.append(_tokens(text, getattr(block, "section_path", "") or "", topics))
    if not tokenized:
        return []
    query = _tokens(" ".join(topics), "", topics)
    query_set = set(query)
    scores = BM25Okapi(tokenized).get_scores(query)
    # 词只出现在一半文档里时，BM25 的 IDF 会正好是 0，不能把它当成没检索到。
    ranked = []
    for index, score in enumerate(scores):
        overlap = len(query_set & set(tokenized[index]))
        if overlap:
            ranked.append((float(score), overlap, index))
    ranked.sort(reverse=True)
    return [indexed[index] for _score, _overlap, index in ranked[:limit]]


def _tokens(text: str, section_path: str, topics: list[str]) -> list[str]:
    tokens = [token.strip() for token in jieba.lcut(f"{section_path}\n{text}") if token.strip()]
    for topic in topics:
        if topic and topic in text:
            tokens.append(topic)
        if section_path and topic == section_path:
            tokens.append(topic)
    return tokens
