"""从数据文件读取审查规则。规则原文不写在代码里。"""

from __future__ import annotations

import json
from pathlib import Path

from app.db import repo_root
from app.schematic.planner import RULE_TAG_SET


def load_rule_packs(path: Path | None = None) -> list[dict]:
    file = path or (repo_root() / "data" / "schematic_rules" / "packs.json")
    if not file.is_file():
        return []
    loaded = json.loads(file.read_text(encoding="utf-8"))
    packs: list[dict] = []
    seen: set[str] = set()
    for item in loaded.get("packs") or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        text = str(item.get("text") or "").strip()
        if tag not in RULE_TAG_SET or not text or tag in seen:
            continue
        seen.add(tag)
        apply = str(item.get("apply") or "tagged").strip()
        packs.append(
            {
                "tag": tag,
                "title": str(item.get("title") or tag).strip() or tag,
                "hint": str(item.get("hint") or tag).strip() or tag,
                "text": text,
                "apply": "always" if apply == "always" else "tagged",
            }
        )
    return packs


def select_tags(requested: list[str], packs: list[dict]) -> list[str]:
    """通用规则每次规划成功都挂上。其余标签只在规划器点名时挂上。"""
    wanted = {tag for tag in requested if isinstance(tag, str) and tag in RULE_TAG_SET}
    chosen: list[str] = []
    for pack in packs:
        tag = pack["tag"]
        if pack.get("apply") == "always" or tag in wanted:
            chosen.append(tag)
    return chosen


def append_rule_text(tag: str, sentence: str, path: Path | None = None) -> None:
    """把人工确认过的一句补进已有规则包。同一标签不另开一条，避免被去重丢掉。"""
    file = path or (repo_root() / "data" / "schematic_rules" / "packs.json")
    sentence = sentence.strip()
    if not file.is_file():
        loaded = {"packs": []}
    else:
        loaded = json.loads(file.read_text(encoding="utf-8"))
    packs = loaded.get("packs")
    if not isinstance(packs, list):
        packs = []
        loaded["packs"] = packs
    compact = "".join(sentence.split())
    for item in packs:
        if not isinstance(item, dict) or str(item.get("tag") or "").strip() != tag:
            continue
        text = str(item.get("text") or "").rstrip()
        if compact and compact in "".join(text.split()):
            return
        item["text"] = f"{text}\n{sentence}" if text else sentence
        _write_json(file, loaded)
        return
    packs.append(
        {
            "tag": tag,
            "title": tag,
            "hint": tag,
            "text": sentence,
            "apply": "tagged",
        }
    )
    _write_json(file, loaded)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def render_rules(tags: list[str], packs: list[dict]) -> tuple[str, list[str]]:
    by_tag = {pack["tag"]: pack for pack in packs}
    used: list[str] = []
    blocks: list[str] = []
    for tag in tags:
        pack = by_tag.get(tag)
        if pack is None or tag in used:
            continue
        used.append(tag)
        blocks.append(f"{pack['title']}\n{pack['text']}")
    return "\n\n".join(blocks), used
