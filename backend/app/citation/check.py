"""引用核验与引用时效。本阶段不调用模型。"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field

from app.citation.gate import accept_quote, citation_result_hash
from app.citation.limits import DoseLimit, load_verified_limits, population_name, quantity_key
from app.citation.numeric import RULE_VERSION, compare_values
from app.citation.place import describe_place
from app.citation.quantities import Quantity, extract_quantities, roles_of
from app.parse.docx_walk import PARSER_VERSION
from app.citation.timeliness import (
    BibliographyEntry,
    RepealRow,
    evaluation_basis_entries,
    designated_basis,
    is_non_normative,
    load_repeal_map,
    matching_repeal,
)
from app.hashing import sha256_text, stable_hash
from app.verify.citation import normalize_quote, quote_matches

SCOPE_NOTE = "本次仅做引用核验，未对依据逐条审查"
UNCOVERED = ["屏蔽计算", "联锁", "监测", "管理制度"]
_CHECKED = {"与依据一致", "不一致", "未超限值", "超过限值"}
_QUOTE_VERDICT = {"true": "与依据一致", "false": "不一致", "unknown": "无法核对"}
_LIMIT_VERDICT = {"true": "未超限值", "false": "超过限值", "unknown": "无法核对"}
_PREDICTION_VERDICT = {"true": "内部自洽", "false": "内部不自洽", "unknown": "无法核对"}

_CODE = re.compile(
    r"(?:GB|HJ|GBZ)\s*/?\s*T?\s*\d+(?:\.\d+)?\s*[-—]\s*\d{4}",
    re.IGNORECASE,
)
_PREDICTION = re.compile(
    r"分别为\s*(?P<a>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<ua>mSv/a|mSv)"
    r"\s*[、,，]\s*(?P<b>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<ub>mSv/a|mSv)"
    r".{0,80}?约束值\s*[（(]\s*(?P<c>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<uc>mSv/a|mSv)"
    r"\s*[、,，]\s*(?P<d>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<ud>mSv/a|mSv)"
)


class BasisClauseView(BaseModel):
    doc_id: str
    title: str
    path: str
    text: str
    text_sha256: str
    effective_date: date
    verified: bool
    status: str = "active"


class Finding(BaseModel):
    kind: str
    question: str
    verdict: str
    evidence_span: str
    char_start: int
    char_end: int
    quote: str = ""
    basis_doc_id: str | None = None
    basis_path: str | None = None
    source_label: str = ""
    section: str = ""
    reason: str = ""
    replaced_by: str | None = None
    trace: list = Field(default_factory=list)
    as_of: str
    place: str = ""


class RejectedFinding(BaseModel):
    reason: str
    payload: dict


class CheckOutput(BaseModel):
    findings: list[Finding]
    rejected: list[RejectedFinding]
    result_hash: str
    scope_note: str = SCOPE_NOTE
    coverage: dict = Field(default_factory=dict)
    uncovered: list[str] = Field(default_factory=lambda: list(UNCOVERED))


def review_input_key(
    *,
    report_sha: str,
    basis_sha: str,
    repeal_sha: str,
    limits_sha: str,
    as_of: date,
    compiled_on: date | None,
    parser_version: str = PARSER_VERSION,
) -> str:
    return stable_hash(
        {
            "report_sha256": report_sha,
            "basis_sha256": basis_sha,
            "repeal_sha256": repeal_sha,
            "limits_sha256": limits_sha,
            "rule_version": RULE_VERSION,
            "parser_version": parser_version,
            "as_of": as_of.isoformat(),
            "compiled_on": compiled_on.isoformat() if compiled_on else "",
        }
    )


def admissible_clauses(clauses: list[BasisClauseView], as_of: date, repeal_rows: list[RepealRow]) -> list[BasisClauseView]:
    kept = []
    for clause in clauses:
        if not _in_force(clause, as_of):
            continue
        repealed = matching_repeal(clause.title, repeal_rows)
        if repealed is not None and as_of >= repealed.repealed_on:
            continue
        kept.append(clause)
    return kept


def basis_fingerprint(clauses: list[BasisClauseView]) -> str:
    return sha256_text("\n".join(sorted(clause.text_sha256 for clause in clauses)))


def check_report(
    parsed,
    *,
    as_of: date,
    clauses: list[BasisClauseView],
    repeal_rows: list[RepealRow] | None = None,
    dose_limits: list[DoseLimit] | None = None,
    compiled_on: date | None = None,
) -> CheckOutput:
    rows = repeal_rows if repeal_rows is not None else load_repeal_map()
    limits = dose_limits if dose_limits is not None else load_verified_limits()
    clauses = admissible_clauses(clauses, as_of, rows)
    report_text = parsed.full_text
    accepted: list[Finding] = []
    rejected: list[RejectedFinding] = []

    def keep(finding: Finding, *, source_text: str, in_force: bool, require_quote: bool) -> None:
        if require_quote:
            reason = accept_quote(
                quote=finding.quote,
                source_text=source_text,
                report_text=report_text,
                evidence_span=finding.evidence_span,
                char_start=finding.char_start,
                char_end=finding.char_end,
                in_force=in_force,
            )
        else:
            reason = None if _evidence_ok(report_text, finding) else "evidence_span 与报告原文对不上"
        if reason:
            rejected.append(RejectedFinding(reason=reason, payload=finding.model_dump(mode="json")))
            return
        accepted.append(finding)

    for entry in evaluation_basis_entries(parsed.blocks):
        keep(_timeliness(entry, rows, clauses, as_of, compiled_on), source_text="", in_force=True, require_quote=False)

    for finding, clause in _limit_quotes(parsed.blocks, clauses, limits, as_of, report_text):
        if clause is None:
            keep(finding, source_text="", in_force=True, require_quote=False)
            continue
        keep(
            finding,
            source_text=clause.text,
            in_force=_in_force(clause, as_of),
            require_quote=True,
        )

    for finding, clause in _constraints(parsed.blocks, clauses, limits, as_of):
        if clause is None:
            keep(finding, source_text="", in_force=True, require_quote=False)
            continue
        keep(finding, source_text=clause.text, in_force=_in_force(clause, as_of), require_quote=True)

    for finding, clause in _copied_quotes(parsed.blocks, clauses, as_of):
        keep(finding, source_text=clause.text, in_force=_in_force(clause, as_of), require_quote=True)

    for finding, source in _predictions(parsed.blocks, as_of):
        keep(finding, source_text=source, in_force=True, require_quote=True)

    payloads = [item.model_dump(mode="json") for item in accepted]
    for payload in payloads:
        payload.pop("place", None)
    basis_sha = _basis_sha(clauses, as_of)
    report_sha = sha256_text(report_text)
    placed = [_with_place(item, parsed.blocks) for item in accepted]
    parser_version = getattr(parsed, "parser_version", None) or PARSER_VERSION
    return CheckOutput(
        findings=placed,
        rejected=rejected,
        result_hash=citation_result_hash(
            report_sha=report_sha,
            basis_sha=basis_sha,
            as_of=as_of,
            findings=payloads,
            parser_version=parser_version,
        ),
        coverage=_coverage(clauses, placed),
    )


def _with_place(finding: Finding, blocks) -> Finding:
    place = describe_place(blocks, finding.char_start)
    if not place:
        return finding
    return finding.model_copy(update={"place": place})


def _timeliness(
    entry: BibliographyEntry,
    rows: list[RepealRow],
    clauses: list[BasisClauseView],
    as_of: date,
    compiled_on: date | None,
) -> Finding:
    matched = matching_repeal(entry.name, rows)
    replaced_by = None
    reason = ""
    own = designated_basis(entry.text) or designated_basis(entry.name)
    if matched is not None:
        verdict = _timing_verdict(matched.repealed_on, as_of, compiled_on)
        replaced_by = matched.replaced_by or None
        question = "评价依据所列法律在编制日和基准日是否仍有效" if compiled_on else "评价依据所列法律在基准日是否仍有效"
        if compiled_on and as_of >= matched.repealed_on and compiled_on < matched.repealed_on:
            reason = "编制时这部法律还有效，基准日已经废止。不是报告写错了。"
    elif _basis_title_in_force(entry.text, clauses, as_of):
        verdict = "在依据库中"
        question = "评价依据所列文件是否属于已核验且已施行的依据"
    elif own:
        verdict = "尚未核验"
        question = f"{own} 是否已有核验过的条款"
        reason = f"依据库中无已核验的 {own} 条款"
    elif is_non_normative(entry.name, entry.text, entry.section):
        verdict = "非规范性文件，跳过"
        question = "评价依据里的这条是不是规范性文件"
        reason = "没有文号或标准号，按技术资料或教材跳过，不计入超出依据。"
    else:
        verdict = "超出依据范围"
        question = "评价依据所列文件是否属于依据库或失效映射"
        reason = "不在已核验依据或失效映射中。下一步是人工确认要不要补进依据库。"
    return Finding(
        kind="timeliness",
        question=question,
        verdict=verdict,
        evidence_span=entry.text,
        char_start=entry.char_start,
        char_end=entry.char_end,
        source_label="失效映射" if matched else "",
        section=entry.section,
        reason=reason,
        replaced_by=replaced_by,
        as_of=as_of.isoformat(),
    )


def _limit_quotes(blocks, clauses, limits, as_of: date, report_text: str):
    caption_at = _caption_index(blocks, "表7-2")
    if caption_at is None:
        return []
    caption = blocks[caption_at].text
    linked = _linked_limits(limits, clauses, caption, as_of)
    paired = []
    for item in _table_quantities(blocks, caption_at, report_text):
        key = quantity_key(item["quantity"])
        matches = [row for row in linked if key is not None and _limit_key(row[0]) == key]
        if len(matches) != 1:
            paired.append(
                (
                    _open_finding(
                        "quote_numeric",
                        "引用的剂量数值是否与已核验限值表一致",
                        "无法核对",
                        item,
                        as_of,
                        reason=_unmatched_quote_reason(caption, linked, key, len(matches)),
                    ),
                    None,
                )
            )
            continue
        limit, clause = matches[0]
        compared = compare_values("==", item["quantity"], _as_quantity(limit))
        paired.append(
            (
                _measured_finding(
                    "quote_numeric",
                    "引用的剂量数值是否与已核验限值表一致",
                    _QUOTE_VERDICT[compared["result"]],
                    item,
                    {"quantity": _as_quantity(limit), "clause": clause, "quote": limit.quote},
                    compared["trace"],
                    "已核验限值表",
                    as_of,
                ),
                clause,
            )
        )
    return paired


def _constraints(blocks, clauses, limits, as_of: date):
    ceilings = _ceilings(limits, clauses, as_of)
    paired = []
    for block in blocks:
        text = block.text or ""
        for quantity in extract_quantities(text):
            # 只认写下约束值的分句。后文「满足约束」「低于约束目标值」是在用这个数。
            clause, start, end = _constraint_clause(text, quantity)
            if not clause or _mentions_hj(clause) or not _assigns_constraint(clause, quantity):
                continue
            roles = roles_of(clause)
            population = population_name(roles)
            organs = {role for role in roles if role in {"眼晶体", "皮肤", "四肢"}}
            located = {
                "quantity": quantity,
                "text": clause,
                "char_start": block.char_start + start,
                "char_end": block.char_start + end,
                "clause": None,
            }
            if population is None or organs or quantity.unit != "mSv/a":
                continue
            chosen = ceilings.get(population)
            question = "合理性检查：剂量约束是否不超过已核验的最低限值"
            if chosen is None:
                paired.append(
                    (
                        _open_finding(
                            "constraint_limit",
                            question,
                            "无法核对",
                            located,
                            as_of,
                            reason="依据库中无已核验的剂量限值，不能判断约束是否超限",
                        ),
                        None,
                    )
                )
                continue
            limit, basis = chosen
            compared = compare_values("<=", quantity, _as_quantity(limit))
            paired.append(
                (
                    _measured_finding(
                        "constraint_limit",
                        question,
                        _LIMIT_VERDICT[compared["result"]],
                        located,
                        {"quantity": _as_quantity(limit), "clause": basis, "quote": limit.quote},
                        compared["trace"],
                        "合理性检查",
                        as_of,
                    ),
                    basis,
                )
            )
    return paired


_CLAUSE_BREAKS = "。；;\n"


def _constraint_clause(text: str, quantity: Quantity) -> tuple[str, int, int]:
    start = max((text.rfind(char, 0, quantity.local_start) for char in _CLAUSE_BREAKS), default=-1) + 1
    ends = [text.find(char, quantity.local_end) for char in _CLAUSE_BREAKS]
    ends = [pos for pos in ends if pos >= 0]
    end = min(ends) if ends else len(text)
    clause = text[start:end]
    lead = len(clause) - len(clause.lstrip())
    trimmed = clause.strip()
    if not trimmed:
        return "", start, start
    rel = start + lead
    return trimmed, rel, rel + len(trimmed)


def _assigns_constraint(clause: str, quantity: Quantity) -> bool:
    raw = re.escape(quantity.raw)
    patterns = (
        rf"约束值取\s*{raw}",
        rf"取\s*{raw}.{{0,40}}作为.{{0,24}}约束",
        rf"约束(?:目标)?值\s*(?:为|是|：|:)\s*{raw}",
    )
    return any(re.search(pattern, clause) for pattern in patterns)


def _mentions_hj(text: str) -> bool:
    return any(code.startswith("HJ") for code in _codes(text))


def _predictions(blocks, as_of: date):
    found = []
    for block in blocks:
        text = block.text or ""
        matched = _PREDICTION.search(text)
        if not matched:
            continue
        pairs = (("a", "ua", "c", "uc"), ("b", "ub", "d", "ud"))
        for left_num, left_unit, right_num, right_unit in pairs:
            left = _annual(matched.group(left_num), matched.group(left_unit))
            right = _annual(matched.group(right_num), matched.group(right_unit))
            compared = compare_values("<=", left, right)
            evidence = matched.group(left_num) + matched.group(left_unit)
            quote = matched.group(right_num) + matched.group(right_unit)
            evidence_at = text.find(evidence)
            quote_at = text.find(quote, matched.start("c"))
            if evidence_at < 0 or quote_at < 0:
                continue
            evidence_start = block.char_start + evidence_at
            finding = Finding(
                kind="prediction_constraint",
                question="预测值是否不超过报告自设约束",
                verdict=_PREDICTION_VERDICT[compared["result"]],
                evidence_span=evidence,
                char_start=evidence_start,
                char_end=evidence_start + len(evidence),
                quote=quote,
                source_label="报告自设约束",
                trace=compared["trace"],
                as_of=as_of.isoformat(),
            )
            found.append((finding, text))
    return found


def _annual(number: str, unit: str) -> Quantity:
    canonical = "mSv/a" if unit in {"mSv", "mSv/a"} else unit
    raw = number + unit
    return Quantity(float(number), canonical, raw, frozenset(), raw, 0, len(raw))


def _table_quantities(blocks, caption_at: int, report_text: str) -> list[dict]:
    prefix = tuple((step.table, step.row, step.col) for step in blocks[caption_at].table_path)
    cells = []
    seen = False
    for block in blocks[caption_at + 1 :]:
        key = tuple((step.table, step.row, step.col) for step in block.table_path)
        if len(key) > len(prefix) and key[: len(prefix)] == prefix:
            cells.append((key[len(prefix)], block))
            seen = True
            continue
        if seen:
            break
    if not cells:
        return []
    header_row = min(step[1] for step, _block in cells)
    headers: dict[int, str] = {}
    for step, block in cells:
        _nested, row, col = step
        if row == header_row and not extract_quantities(block.text or ""):
            headers[col] = (headers.get(col, "") + " " + (block.text or "")).strip()
    located = []
    for step, block in cells:
        _nested, row, col = step
        if row == header_row:
            continue
        extra = tuple(
            word for word in ("职业", "工作人员", "公众", "眼晶体", "皮肤", "四肢") if word in headers.get(col, "")
        )
        for quantity in extract_quantities(block.text or "", extra):
            located.append(_locate(block, quantity, report_text))
    return located


def _clause_quantities(clauses: list[BasisClauseView]) -> list[dict]:
    located = []
    for clause in clauses:
        for quantity in extract_quantities(clause.text):
            located.append(
                {
                    "quantity": quantity,
                    "text": quantity.window,
                    "char_start": 0,
                    "char_end": 0,
                    "clause": clause,
                }
            )
    return located


def _locate(block, quantity: Quantity, report_text: str) -> dict:
    start = block.char_start + quantity.local_start
    end = block.char_start + quantity.local_end
    window = report_text[start:end]
    return {"quantity": quantity, "text": window, "char_start": start, "char_end": end, "clause": None}


def _open_finding(kind: str, question: str, verdict: str, located: dict, as_of: date, reason: str = "") -> Finding:
    return Finding(
        kind=kind,
        question=question,
        verdict=verdict,
        evidence_span=located["text"],
        char_start=located["char_start"],
        char_end=located["char_end"],
        reason=reason,
        as_of=as_of.isoformat(),
    )


def _unmatched_quote_reason(caption: str, linked, key, count: int) -> str:
    if not linked:
        named = designated_basis(caption) or "GB 18871"
        return f"依据库中无已核验的 {named} 条款"
    if key is None:
        return "报告里这个数字没有分清对象、器官或时段"
    if count > 1:
        return "已核验限值表里对上了不止一行"
    return "已核验限值表里没有同一对象的一行"


def _measured_finding(kind, question, verdict, located, chosen, trace, source_label, as_of: date) -> Finding:
    clause = chosen["clause"]
    return Finding(
        kind=kind,
        question=question,
        verdict=verdict,
        evidence_span=located["text"],
        char_start=located["char_start"],
        char_end=located["char_end"],
        quote=chosen.get("quote") or chosen["quantity"].window,
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        source_label=source_label,
        trace=trace,
        as_of=as_of.isoformat(),
    )


_MIN_COPY = 12


def _copied_quotes(blocks, clauses, as_of: date):
    """报告里连续抄下的已核验条款原文。转述对不上连续原文时不另造一条无法核对。"""
    usable = [clause for clause in clauses if _in_force(clause, as_of)]
    paired = []
    for block in blocks:
        text = block.text or ""
        named = [clause for clause in usable if _block_cites(text, clause)]
        if not named:
            continue
        for sentence, start, end in _sentences(text):
            hits = []
            for clause in named:
                span = _common_span(sentence, clause.text)
                if len(span) < _MIN_COPY or _span_is_title(span, clause):
                    continue
                hits.append((span, clause))
            for span, clause in _prefer_longer_paths(hits):
                paired.append((_copy_finding(block, sentence, start, end, span, clause, as_of), clause))
    return paired


def _block_cites(text: str, clause: BasisClauseView) -> bool:
    if _same_standard(text, clause):
        return True
    title = (clause.title or "").strip()
    return bool(title and title in text)


def _sentences(text: str):
    found = []
    start = 0
    for index, char in enumerate(text):
        if char not in "。\n":
            continue
        end = index if char == "\n" else index + 1
        chunk = text[start:end]
        lead = len(chunk) - len(chunk.lstrip())
        span = chunk.strip()
        if span:
            found.append((span, start + lead, start + lead + len(span)))
        start = index + 1
    if start < len(text):
        chunk = text[start:]
        lead = len(chunk) - len(chunk.lstrip())
        span = chunk.strip()
        if span:
            found.append((span, start + lead, start + lead + len(span)))
    return found


def _common_span(report: str, clause: str) -> str:
    left, _ = _fold_index(report)
    right, origins = _fold_index(clause)
    if not left or not right:
        return ""
    best_len = 0
    best_end = 0
    previous = [0] * (len(right) + 1)
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, start=1):
            if char != other:
                continue
            current[index] = previous[index - 1] + 1
            if current[index] > best_len:
                best_len = current[index]
                best_end = index
        previous = current
    if best_len == 0:
        return ""
    start = origins[best_end - best_len]
    end = origins[best_end - 1] + 1
    return clause[start:end]


def _fold_index(text: str) -> tuple[str, list[int]]:
    folded: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        for piece in normalize_quote(char):
            folded.append(piece)
            origins.append(index)
    return "".join(folded), origins


def _span_is_title(span: str, clause: BasisClauseView) -> bool:
    title = normalize_quote(clause.title or "")
    folded = normalize_quote(span)
    return bool(title and folded in title)


def _prefer_longer_paths(hits: list[tuple[str, BasisClauseView]]):
    kept: dict[str, BasisClauseView] = {}
    for span, clause in hits:
        current = kept.get(span)
        if current is None or len(clause.path) > len(current.path):
            kept[span] = clause
    return [(span, clause) for span, clause in kept.items()]


def _copy_finding(block, sentence: str, start: int, end: int, span: str, clause: BasisClauseView, as_of: date) -> Finding:
    return Finding(
        kind="quote_text",
        question="这句转述是否连续出现在已核验条款的原文里",
        verdict="与依据一致",
        evidence_span=sentence,
        char_start=block.char_start + start,
        char_end=block.char_start + end,
        quote=span,
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        source_label="已核验条款",
        as_of=as_of.isoformat(),
    )


def _caption_index(blocks, marker: str) -> int | None:
    compact = marker.replace(" ", "")
    for index, block in enumerate(blocks):
        if compact in (block.text or "").replace(" ", ""):
            return index
    return None


def _same_standard(caption: str, clause: BasisClauseView) -> bool:
    return bool(_codes(caption) & _codes(clause.doc_id + clause.title))


def _codes(text: str) -> set[str]:
    return {re.sub(r"[^A-Z0-9]", "", match.group(0).upper()) for match in _CODE.finditer(text or "")}


def _basis_title_in_force(name: str, clauses: list[BasisClauseView], as_of: date) -> bool:
    for clause in clauses:
        if not _in_force(clause, as_of):
            continue
        if _codes(name) & _codes(clause.doc_id + clause.title):
            return True
        if clause.title and clause.title in name:
            return True
    return False


def _in_force(clause: BasisClauseView, as_of: date) -> bool:
    return bool(
        clause.verified
        and clause.status == "active"
        and clause.effective_date <= as_of
        and sha256_text(clause.text) == clause.text_sha256
    )


def _timing_verdict(repealed_on: date, as_of: date, compiled_on: date | None) -> str:
    as_of_status = "基准日已废止" if as_of >= repealed_on else "基准日有效"
    if compiled_on is None:
        return as_of_status
    compiled_status = "编制时有效" if compiled_on < repealed_on else "编制时已废止"
    return f"{compiled_status} / {as_of_status}"


def _linked_limits(limits: list[DoseLimit], clauses, caption: str, as_of: date):
    codes = _codes(caption)
    linked = []
    for limit in limits:
        if not limit.verified:
            continue
        clause = next(
            (
                item
                for item in clauses
                if item.doc_id == limit.doc_id and item.path == limit.clause_path and _in_force(item, as_of)
            ),
            None,
        )
        if clause is None or not quote_matches(limit.quote, clause.text):
            continue
        if codes and not (_codes(f"{limit.doc_id} {clause.title}") & codes):
            continue
        linked.append((limit, clause))
    return linked


def _limit_key(limit: DoseLimit) -> tuple[str, str, str, str]:
    return (limit.population, limit.organ, limit.period, limit.unit)


def _as_quantity(limit: DoseLimit) -> Quantity:
    return Quantity(limit.value, limit.unit, limit.quote, frozenset(), limit.quote, 0, len(limit.quote))


def _ceilings(limits: list[DoseLimit], clauses, as_of: date) -> dict[str, tuple[DoseLimit, BasisClauseView]]:
    chosen: dict[str, tuple[DoseLimit, BasisClauseView]] = {}
    for limit, clause in _linked_limits(limits, clauses, "", as_of):
        if limit.organ or limit.unit != "mSv/a":
            continue
        current = chosen.get(limit.population)
        if current is None or limit.value < current[0].value:
            chosen[limit.population] = (limit, clause)
    return chosen


def _coverage(clauses: list[BasisClauseView], findings: list[Finding]) -> dict:
    present = {(clause.doc_id, clause.path) for clause in clauses}
    used = {
        (item.basis_doc_id, item.basis_path)
        for item in findings
        if item.basis_path and item.verdict in _CHECKED and (item.basis_doc_id, item.basis_path) in present
    }
    return {"in_force": len(present), "checked": len(used), "unchecked": len(present - used)}


def _basis_sha(clauses: list[BasisClauseView], as_of: date) -> str:
    parts = [clause.text_sha256 for clause in clauses if _in_force(clause, as_of)]
    return sha256_text("\n".join(sorted(parts)))


def _evidence_ok(report_text: str, finding: Finding) -> bool:
    if finding.char_start < 0 or finding.char_end > len(report_text) or finding.char_start >= finding.char_end:
        return False
    return report_text[finding.char_start : finding.char_end] == finding.evidence_span
