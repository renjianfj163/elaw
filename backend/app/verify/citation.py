"""引用必须是原文子串。对不上就失败，没有降级。"""

import re
import unicodedata
from datetime import date, datetime


def normalize_quote(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", folded)


def quote_matches(quote: str, article_text: str) -> bool:
    needle = normalize_quote(quote)
    if not needle:
        return False
    return needle in normalize_quote(article_text)


def evidence_matches(parsed_text: str, span: str, char_start: int, char_end: int) -> bool:
    if span is None or parsed_text is None:
        return False
    if char_start is None or char_end is None:
        return False
    if char_start < 0 or char_end > len(parsed_text) or char_start >= char_end:
        return False
    return parsed_text[char_start:char_end] == span


def version_in_force(
    *,
    effective_date: date | None,
    repeal_date: date | None,
    status: str,
    reference_date: date,
    verified_at: datetime | None,
) -> bool:
    if verified_at is None or status != "有效":
        return False
    if effective_date is None or effective_date > reference_date:
        return False
    if repeal_date is not None and repeal_date <= reference_date:
        return False
    return True


def explain_rejection(
    *,
    quote: str,
    article_text: str,
    evidence: list[dict],
    in_force: bool,
) -> str | None:
    if not in_force:
        return "引用的版本未核验，或在基准日不是有效版本"
    if not quote_matches(quote, article_text):
        return "quote 不是条文原文的子串"
    for item in evidence:
        if not evidence_matches(
            item.get("parsed_text", ""),
            item.get("evidence_span", ""),
            item.get("char_start"),
            item.get("char_end"),
        ):
            return "evidence_span 与材料原文对不上"
    return None
