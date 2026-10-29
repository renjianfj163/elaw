"""摘录必须能在已提供的资料里对上。"""

from __future__ import annotations

import re
import unicodedata


def normalize_quote(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", folded)


def quote_matches(quote: str, article_text: str) -> bool:
    needle = normalize_quote(quote)
    if not needle:
        return False
    return needle in normalize_quote(article_text)
