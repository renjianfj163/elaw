"""法律按编/章/节/条/款/项切开，标准按条款号切开。"""

import re

_NUM = r"[零一二三四五六七八九十百千万0-9]+"
PART = re.compile(rf"^(第{_NUM}编)")
CHAPTER = re.compile(rf"^(第{_NUM}章)")
SECTION = re.compile(rf"^(第{_NUM}节)")
ARTICLE = re.compile(rf"^(第{_NUM}条)")
ITEM = re.compile(r"^[（(]([一二三四五六七八九十百]+)[）)]")
CLAUSE = re.compile(r"^(?P<no>(?:[A-Z]\.\d+(?:\.\d+)*)|\d+(?:\.\d+)+)\b")
CN_DIGITS = "零一二三四五六七八九"
_CN_DIGIT = {char: index for index, char in enumerate(CN_DIGITS)}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(text: str) -> int:
    if text.isdigit():
        return int(text)
    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _CN_DIGIT:
            number = _CN_DIGIT[char]
            continue
        if char in _CN_UNIT:
            if number == 0:
                number = 1
            section += number * _CN_UNIT[char]
            number = 0
            continue
        if char == "万":
            section += number
            if section == 0:
                section = 1
            total += section * 10000
            section = 0
            number = 0
            continue
        raise ValueError(f"无法识别编号：{text}")
    return total + section + number


def clause_number(line: str) -> str | None:
    matched = CLAUSE.match(line.strip())
    if not matched:
        return None
    number = matched.group("no")
    parts = number.split(".")
    numeric = parts[1:] if parts[0].isalpha() else parts
    if any(int(part) == 0 for part in numeric):
        return None
    return number


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
    part = ""
    chapter = ""
    section = ""
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
        part_match = PART.match(line)
        chapter_match = CHAPTER.match(line)
        section_match = SECTION.match(line)
        article_match = ARTICLE.match(line)
        if part_match and not article_match:
            flush()
            part = part_match.group(1)
            chapter = ""
            section = ""
            continue
        if chapter_match and not article_match:
            flush()
            chapter = chapter_match.group(1)
            section = ""
            continue
        if section_match and not article_match:
            flush()
            section = section_match.group(1)
            continue
        if article_match:
            flush()
            current = {
                "part": part,
                "chapter": chapter,
                "section": section,
                "article": article_match.group(1),
                "lines": [line],
            }
            continue
        if current is None:
            continue
        current["lines"].append(line)
    flush()

    pieces: list[dict] = []
    for article in articles:
        pieces.extend(
            _article_pieces(
                article["part"],
                article["chapter"],
                article["section"],
                article["article"],
                article["lines"],
            )
        )
    return pieces


def _article_pieces(part: str, chapter: str, section: str, article: str, lines: list[str]) -> list[dict]:
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

    prefix = "/".join(piece for piece in (part, chapter, section, article) if piece)
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
        number = clause_number(line)
        if number:
            flush()
            current_no = number
            bucket = [line]
            continue
        if current_no:
            bucket.append(line)
    flush()
    return pieces
