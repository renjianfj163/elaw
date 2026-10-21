"""法律按章/条/款/项切开，标准按条款号切开。"""

import re

CHAPTER = re.compile(r"^(第[零一二三四五六七八九十百千万0-9]+章)")
ARTICLE = re.compile(r"^(第[零一二三四五六七八九十百千万0-9]+条)")
ITEM = re.compile(r"^[（(]([一二三四五六七八九十百]+)[）)]")
CLAUSE = re.compile(r"^(\d+(?:\.\d+)+)\b")
CN_DIGITS = "零一二三四五六七八九"


def cn_index(n: int) -> str:
    if n <= 0:
        return str(n)
    if n < 10:
        return CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + CN_DIGITS[n - 10]
    tens, ones = divmod(n, 10)
    if n < 100:
        return CN_DIGITS[tens] + "十" + (CN_DIGITS[ones] if ones else "")
    return str(n)


def split_legal(text: str) -> list[dict]:
    chapter = ""
    articles: list[dict] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current and current["lines"]:
            articles.append(current)
        current = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current is not None:
                current["lines"].append("")
            continue
        chapter_match = CHAPTER.match(line)
        article_match = ARTICLE.match(line)
        if chapter_match and not article_match:
            flush()
            chapter = chapter_match.group(1)
            continue
        if article_match:
            flush()
            current = {"chapter": chapter, "article": article_match.group(1), "lines": [line]}
            continue
        if current is None:
            continue
        current["lines"].append(line)
    flush()

    pieces: list[dict] = []
    for article in articles:
        pieces.extend(_article_pieces(article["chapter"], article["article"], article["lines"]))
    return pieces


def _article_pieces(chapter: str, article: str, lines: list[str]) -> list[dict]:
    paragraphs: list[list[str]] = []
    bucket: list[str] = []
    for line in lines:
        if line == "":
            if bucket:
                paragraphs.append(bucket)
                bucket = []
            continue
        bucket.append(line)
    if bucket:
        paragraphs.append(bucket)
    if not paragraphs:
        return []

    prefix = "/".join(part for part in (chapter, article) if part)
    pieces = []
    for index, paragraph in enumerate(paragraphs, start=1):
        kuan = f"第{cn_index(index)}款"
        item_lines = [line for line in paragraph if ITEM.match(line)]
        body = "\n".join(paragraph).strip()
        pieces.append({"path": f"{prefix}/{kuan}", "text": body})
        if len(item_lines) >= 2 or (item_lines and len(paragraph) > 1):
            for item_index, line in enumerate(item_lines, start=1):
                pieces.append(
                    {
                        "path": f"{prefix}/{kuan}/第{cn_index(item_index)}项",
                        "text": line,
                    }
                )
    return pieces


def split_standard(text: str) -> list[dict]:
    pieces: list[dict] = []
    current_no = ""
    bucket: list[str] = []

    def flush() -> None:
        nonlocal bucket, current_no
        body = "\n".join(bucket).strip()
        if current_no and body:
            pieces.append({"path": current_no, "text": body})
        bucket = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = CLAUSE.match(line)
        if match:
            flush()
            current_no = match.group(1)
            bucket = [line]
            continue
        if current_no:
            bucket.append(line)
    flush()
    return pieces
