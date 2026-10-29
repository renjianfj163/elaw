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
