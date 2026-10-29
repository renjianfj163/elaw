"""按章节裁剪 Datasheet。

固定留下绝对最大额定值、推荐工作条件、引脚说明、典型应用和电气特性。
封装、订购信息和寄存器表丢掉。器件型号只用来补召回这些标题之外的相关节。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import jieba
from rank_bm25 import BM25Okapi

from app.schematic.review import MAX_BASIS_CHARS

jieba.setLogLevel(logging.ERROR)

_KEEP = re.compile(
    r"absolute maximum|recommended operating|pin description|pin functions|pin configuration|"
    r"typical application|electrical characteristics|"
    r"绝对最大|推荐工作|推荐操作|引脚说明|引脚功能|引脚配置|管脚说明|"
    r"典型应用|电气特性",
    re.I,
)
_DROP = re.compile(
    r"package|ordering information|mechanical|soldering|tape and reel|land pattern|"
    r"register map|寄存器|封装|订购|外形尺寸|焊接",
    re.I,
)
_FALLBACK = (
    "引脚说明",
    "引脚功能",
    "典型应用",
    "绝对最大",
    "推荐工作",
    "电气特性",
    "pin description",
    "typical application",
    "absolute maximum",
    "recommended operating",
    "electrical characteristics",
)


@dataclass
class Section:
    index: int
    title: str
    body: str

    @property
    def role(self) -> str:
        if _KEEP.search(self.title):
            return "keep"
        if _DROP.search(self.title):
            return "drop"
        return "other"


def crop_datasheet(
    text: str,
    components: list[str],
    *,
    limit: int = MAX_BASIS_CHARS,
    conservative: bool = False,
) -> str:
    raw = text.strip()
    if not raw or limit <= 0:
        return ""
    sections = _split(raw)
    if len(raw) <= limit and not any(section.role == "drop" for section in sections):
        return raw
    if len(raw) <= limit:
        kept = [section for section in sections if section.role != "drop"]
        rendered = _join(kept)
        return rendered or raw
    if not _has_headings(sections):
        return _fit_blocks(_windows(raw), _query(components, conservative), limit)

    chosen = [section for section in sections if section.role == "keep"]
    if not conservative:
        chosen_ids = {section.index for section in chosen}
        for index in _rank(sections, component_terms(components), limit=6):
            section = sections[index]
            if section.index in chosen_ids or section.role == "drop":
                continue
            chosen.append(section)
            chosen_ids.add(section.index)
    if chosen:
        return _fit(chosen, limit)
    return _fit_blocks(_windows(raw), _query(components, conservative=True), limit)


def component_terms(components: list[str]) -> list[str]:
    terms: list[str] = []
    for raw in components:
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{1,}", raw):
            if token.isdigit() or token in terms:
                continue
            terms.append(token)
    return terms


def bind_basis(cropped: str, rules: str, *, limit: int = MAX_BASIS_CHARS) -> str:
    body = cropped.strip()
    block = rules.strip()
    if not block:
        return body[:limit]
    headed = f"【审查规则】\n{block}"
    if len(headed) >= limit:
        return headed[:limit]
    room = limit - len(headed) - 2
    if len(body) > room:
        body = body[:room].rstrip()
    if not body:
        return headed
    return f"{body}\n\n{headed}"


def _split(text: str) -> list[Section]:
    sections: list[Section] = []
    title = ""
    lines: list[str] = []

    def push() -> None:
        body = "\n".join(lines).strip()
        if not title and not body:
            return
        sections.append(Section(len(sections), title.strip(), body))

    for line in text.splitlines():
        if _is_heading(line):
            push()
            title = line.strip()
            lines = []
        else:
            lines.append(line)
    push()
    if not sections:
        sections.append(Section(0, "", text.strip()))
    return sections


def _has_headings(sections: list[Section]) -> bool:
    return any(section.title for section in sections)


def _is_heading(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 80:
        return False
    if re.match(r"^#{1,4}\s+\S", text):
        return True
    if re.match(r"^第[0-9０-９一二三四五六七八九十百千]+[章节篇部]", text):
        return True
    if re.match(r"^[一二三四五六七八九十]+、\S", text):
        return True
    if re.match(r"^\d+(?:\.\d+){0,3}[\s、.]+", text) and len(text) <= 60:
        rest = re.sub(r"^\d+(?:\.\d+){0,3}[\s、.]*", "", text)
        if re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", rest):
            return True
    if re.match(r"^【[^】]{1,40}】$", text):
        return True
    letters = re.sub(r"[^A-Za-z]", "", text)
    if not re.search(r"[\u4e00-\u9fff]", text) and text.upper() == text and 8 <= len(letters) <= 80:
        return True
    if len(text) <= 60 and _KEEP.search(text) and not re.search(r"[。；;]$", text):
        return True
    return False


def _priority(title: str) -> int:
    order = (
        r"pin description|pin functions|pin configuration|引脚|管脚",
        r"typical application|典型应用",
        r"recommended operating|推荐工作|推荐操作",
        r"absolute maximum|绝对最大",
        r"electrical characteristic|电气特性",
    )
    for index, pattern in enumerate(order):
        if re.search(pattern, title, re.I):
            return index
    return len(order)


def _join(sections: list[Section]) -> str:
    return "\n\n".join(block for block in (_render(section) for section in sections) if block).strip()


def _render(section: Section) -> str:
    if section.title and section.body:
        return f"{section.title}\n{section.body}"
    return section.title or section.body


def _fit(sections: list[Section], limit: int) -> str:
    ranked = sorted(sections, key=lambda section: (_priority(section.title), section.index))
    picked: list[Section] = []
    used = 0
    taken: set[int] = set()
    for section in ranked:
        block = _render(section)
        if not block:
            continue
        gap = 2 if picked else 0
        if used + gap + len(block) <= limit:
            picked.append(section)
            taken.add(section.index)
            used += gap + len(block)
    for section in ranked:
        if section.index in taken or not section.body:
            continue
        gap = 2 if picked else 0
        title = f"{section.title}\n" if section.title else ""
        body_room = limit - used - gap - len(title)
        if body_room >= 200:
            picked.append(Section(section.index, section.title, section.body[:body_room].rstrip()))
        break
    picked.sort(key=lambda section: section.index)
    return _join(picked)


def _windows(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(parts) > 1:
        return parts
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    return ["\n".join(lines[index : index + 30]) for index in range(0, len(lines), 30)]


def _fit_blocks(blocks: list[str], terms: list[str], limit: int) -> str:
    if not blocks:
        return ""
    order = _rank_texts(blocks, terms, limit=8)
    if not order:
        return ""
    chosen: list[str] = []
    used = 0
    for index in order:
        block = blocks[index].strip()
        if not block:
            continue
        gap = 2 if chosen else 0
        if used + gap + len(block) <= limit:
            chosen.append(block)
            used += gap + len(block)
            continue
        room = limit - used - gap
        if room >= 200 and not chosen:
            chosen.append(block[:room].rstrip())
        break
    return "\n\n".join(chosen).strip()


def _query(components: list[str], conservative: bool) -> list[str]:
    if conservative:
        return list(_FALLBACK)
    terms = component_terms(components)
    return terms or list(_FALLBACK)


def _rank(sections: list[Section], terms: list[str], limit: int) -> list[int]:
    texts = [f"{section.title}\n{section.body}" for section in sections]
    return _rank_texts(texts, terms, limit)


def _rank_texts(texts: list[str], terms: list[str], limit: int) -> list[int]:
    terms = [term.strip() for term in terms if term and term.strip()]
    if not terms or limit <= 0:
        return []
    indexed: list[int] = []
    tokenized: list[list[str]] = []
    for index, text in enumerate(texts):
        tokens = _tokens(text, terms)
        if not tokens:
            continue
        indexed.append(index)
        tokenized.append(tokens)
    if not tokenized:
        return []
    query = _tokens(" ".join(terms), terms)
    query_set = set(query)
    scores = BM25Okapi(tokenized).get_scores(query)
    ranked = []
    for pos, score in enumerate(scores):
        overlap = len(query_set & set(tokenized[pos]))
        if overlap:
            ranked.append((float(score), overlap, indexed[pos]))
    ranked.sort(reverse=True)
    return [index for _score, _overlap, index in ranked[:limit]]


def _tokens(text: str, terms: list[str]) -> list[str]:
    tokens = [token.strip().lower() for token in jieba.lcut(text) if token.strip()]
    folded = text.lower()
    for term in terms:
        needle = term.lower()
        if needle and needle in folded:
            tokens.append(needle)
    return tokens
