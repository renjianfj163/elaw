"""对照原理图和已有规则包，起草尚未覆盖的定性规则。

通过机械筛选的句子直接写入规则包，并留下审核记录。本次审查不读取这些新句子。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import repo_root
from app.hashing import new_id, sha256_text, utc_now
from app.llm.provider import MemoryCache, cache_key
from app.models import ReviewLog
from app.schematic.planner import RULE_TAG_SET
from app.schematic.review import PROMPT_VERSION, schematic_digest, schematic_paths
from app.schematic.rules import append_rule_text, load_rule_packs, render_rules

_DIGIT = re.compile(r"\d")
_CLAUSE = re.compile(r"第[一二三四五六七八九十百千]+条")

DISCOVERY_SYSTEM = """你负责为通用电路规则库查漏。不要做审查，不要判断图纸符合或不符合。
只起草规则库里没有的定性规则。规则必须是陈述句。
不要写具体数值、单位限值或法规条号。不要复述规则库里已有的句子。
标签只能从用户给出的列表里选择，不能自造。
看不清的电路不要编造。没有缺口时 new_rules 用空数组。
只输出一个 JSON 对象，不要输出解释。"""


class DiscoveryError(Exception):
    pass


class ProposedRule(BaseModel):
    suggested_tag: str
    draft_rule_text: str
    trigger_context: str = ""
    engineering_rationale: str = ""


class DiscoveryBatch(BaseModel):
    new_rules: list[ProposedRule] = Field(default_factory=list)


class StoredRule(BaseModel):
    id: str
    suggested_tag: str
    text: str
    trigger_context: str = ""
    rationale: str = ""
    status: str = "accepted"


class DiscoveryOutcome(BaseModel):
    rules: list[StoredRule] = Field(default_factory=list)
    note: str = ""


def discover_missing_rules(
    schematic_pdf_path: Path | str,
    *,
    transport=None,
    cache: MemoryCache | None = None,
    config: dict | None = None,
    rules_path: Path | None = None,
    pending_path: Path | None = None,
    db: Session | None = None,
) -> DiscoveryOutcome:
    paths = schematic_paths(schematic_pdf_path)
    packs = load_rule_packs(rules_path)
    owns_transport = transport is None
    sender = transport if transport is not None else _default_transport(paths)
    try:
        return _discover(paths, sender, cache, config, packs, rules_path, pending_path, db)
    except DiscoveryError:
        raise
    except Exception:
        return DiscoveryOutcome(note="规则建议没有生成")
    finally:
        if owns_transport:
            closer = getattr(sender, "close", None)
            if callable(closer):
                closer()


def rule_text_problem(text: str) -> str:
    body = text.strip()
    if len(body) < 8:
        return "规则原文过短"
    if _DIGIT.search(body) or _CLAUSE.search(body):
        return "规则原文含有具体数值或条号"
    return ""


def _discover(paths, sender, cache, config, packs, rules_path, pending_path, db) -> DiscoveryOutcome:
    from app.schematic.review import load_gemini_config

    cfg = dict(config or load_gemini_config())
    store = cache if cache is not None else MemoryCache()
    rules_text, _ = render_rules([pack["tag"] for pack in packs], packs)
    catalog = "\n".join(f"- {pack['tag']}：{pack['hint']}" for pack in packs) or "（当前没有规则标签）"
    user = (
        "请对比本次上传的全部原理图和下面的规则库，只起草规则库没有覆盖的定性规则。\n"
        f"【可选标签】只能从下面选择：\n{catalog}\n\n"
        f"【现有规则库】\n{rules_text or '（当前规则库是空的）'}\n\n"
        "没有缺口就返回空的 new_rules。不要写数值和条号。"
    )
    raw = _ask(sender, store, cfg, pdf_sha=schematic_digest(paths), rules_text=rules_text, user=user)
    if raw is None:
        return DiscoveryOutcome(note="规则建议没有生成")
    waiting = _load_pending(_pending_file(pending_path))
    known = list(packs)
    for item in waiting["rules"]:
        if isinstance(item, dict) and item.get("text"):
            known.append({"text": item["text"]})
    kept, dropped = _keep(raw, known)
    if not kept:
        note = "没有新的规则建议。"
        if dropped:
            note = f"有 {dropped} 条草稿含有数值、未知标签或已有条文，没有写入规则库。"
        return DiscoveryOutcome(note=note)
    stored = _save(kept, sha256_text(rules_text), pending_path, rules_path, db)
    note = "已写入规则库。本次检查单未引用，下次审查才会用到。"
    if dropped:
        note = f"{note}另有 {dropped} 条草稿含有数值、未知标签或已有条文，没有写入规则库。"
    return DiscoveryOutcome(rules=stored, note=note)


def _ask(sender, cache: MemoryCache, cfg: dict, *, pdf_sha: str, rules_text: str, user: str) -> str | None:
    model = str(cfg.get("model") or "gemini-3.5-flash")
    prompt_version = f"{cfg.get('prompt_version') or PROMPT_VERSION}#discover"
    key = cache_key(model, prompt_version, {"pdf_sha256": pdf_sha, "rules": sha256_text(rules_text), "user": user})
    if cfg.get("cache", True):
        hit = cache.get(key)
        if hit is not None:
            if hit.get("verdict") == "无法判定":
                return None
            return json.dumps(hit, ensure_ascii=False)
    body = {
        "model": model,
        "system": DISCOVERY_SYSTEM,
        "user": user,
        "max_output_tokens": int(cfg.get("max_output_tokens") or 65536),
        "schema": "discover",
    }
    attempts = int(cfg.get("retries", 2)) + 1
    for _ in range(attempts):
        try:
            content = sender(body)
            parsed = _parse(content)
        except Exception:
            continue
        if cfg.get("cache", True):
            cache.put(key, {"new_rules": [item.model_dump() for item in parsed]})
        return content
    if cfg.get("cache", True):
        cache.put(key, {"verdict": "无法判定", "reason": "规则建议没有生成"})
    return None


def _parse(content: str) -> list[ProposedRule]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    rows = data.get("new_rules") or []
    if not isinstance(rows, list):
        raise ValueError("new_rules 不是数组")
    found: list[ProposedRule] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        found.append(
            ProposedRule(
                suggested_tag=str(item.get("suggested_tag") or "").strip(),
                draft_rule_text=str(item.get("draft_rule_text") or "").strip(),
                trigger_context=str(item.get("trigger_context") or "").strip(),
                engineering_rationale=str(item.get("engineering_rationale") or "").strip(),
            )
        )
        if len(found) >= 8:
            break
    return found


def _keep(content: str, packs: list[dict]) -> tuple[list[ProposedRule], int]:
    try:
        rows = _parse(content)
    except Exception:
        return [], 0
    kept: list[ProposedRule] = []
    dropped = 0
    seen: set[str] = set()
    for item in rows:
        compact = "".join(item.draft_rule_text.split())
        if (
            item.suggested_tag not in RULE_TAG_SET
            or rule_text_problem(item.draft_rule_text)
            or not compact
            or compact in seen
            or _covered(item.draft_rule_text, packs)
        ):
            dropped += 1
            continue
        seen.add(compact)
        kept.append(item)
    return kept, dropped


def _covered(text: str, packs: list[dict]) -> bool:
    compact = "".join(text.split())
    if not compact:
        return False
    for pack in packs:
        hay = "".join(str(pack.get("text") or "").split())
        if compact in hay:
            return True
        for line in str(pack.get("text") or "").splitlines():
            piece = "".join(line.split())
            if len(piece) >= 8 and piece in compact:
                return True
    return False


def _save(
    rows: list[ProposedRule],
    rules_sha: str,
    pending_path: Path | None,
    rules_path: Path | None,
    db: Session | None,
) -> list[StoredRule]:
    file = _pending_file(pending_path)
    payload = _load_pending(file)
    stored: list[StoredRule] = []
    for item in rows:
        sentence = item.draft_rule_text.strip()
        append_rule_text(item.suggested_tag, sentence, rules_path)
        rule = StoredRule(
            id=new_id(),
            suggested_tag=item.suggested_tag,
            text=sentence,
            trigger_context=item.trigger_context,
            rationale=item.engineering_rationale,
            status="accepted",
        )
        row = rule.model_dump()
        row["rules_sha256"] = rules_sha
        payload["rules"].append(row)
        stored.append(rule)
        try:
            _log(db, target_id=rule.id, action="verify", by="系统", note=sentence)
        except Exception:
            pass
    _write_pending(file, payload)
    return stored


def _pending_file(path: Path | None) -> Path:
    return path or (repo_root() / "data" / "schematic_rules" / "pending.json")


def _load_pending(path: Path) -> dict:
    if not path.is_file():
        return {"rules": []}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("rules"), list):
        return {"rules": []}
    return loaded


def _write_pending(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _log(db: Session | None, *, target_id: str, action: str, by: str, note: str) -> None:
    own = db is None
    session = db
    if session is None:
        from app.db import open_session

        session = open_session()
    try:
        session.add(
            ReviewLog(
                id=new_id(),
                finding_id=None,
                target_type="schematic_rule",
                target_id=target_id,
                action=action,
                by=by,
                at=utc_now(),
                note=note[:500],
            )
        )
        session.flush()
        if own:
            session.commit()
    finally:
        if own:
            session.close()


def _default_transport(paths: list[Path]):
    from app.schematic.gemini_client import gemini_transport

    return gemini_transport(paths)
