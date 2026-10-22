"""引用核验与引用时效。本阶段不调用模型。"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field

from app.citation.gate import accept_quote, citation_result_hash
from app.citation.numeric import compare_values, same_subject, verdict_of
from app.citation.place import describe_place
from app.citation.quantities import Quantity, extract_quantities, roles_of
from app.citation.timeliness import (
    BibliographyEntry,
    RepealRow,
    evaluation_basis_entries,
    load_repeal_map,
    matching_repeal,
)
from app.hashing import sha256_text

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


def check_report(parsed, *, as_of: date, clauses: list[BasisClauseView], repeal_rows: list[RepealRow] | None = None) -> CheckOutput:
    rows = repeal_rows if repeal_rows is not None else load_repeal_map()
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
        keep(_timeliness(entry, rows, clauses, as_of), source_text="", in_force=True, require_quote=False)

    for finding, clause in _limit_quotes(parsed.blocks, clauses, as_of, report_text):
        if clause is None:
            keep(finding, source_text="", in_force=True, require_quote=False)
            continue
        keep(
            finding,
            source_text=clause.text,
            in_force=_in_force(clause, as_of),
            require_quote=True,
        )

    for finding, clause in _constraints(parsed.blocks, clauses, as_of):
        if clause is None:
            keep(finding, source_text="", in_force=True, require_quote=False)
            continue
        keep(finding, source_text=clause.text, in_force=_in_force(clause, as_of), require_quote=True)

    for finding, source in _predictions(parsed.blocks, as_of):
        keep(finding, source_text=source, in_force=True, require_quote=True)

    payloads = [item.model_dump(mode="json") for item in accepted]
    for payload in payloads:
        payload.pop("place", None)
    basis_sha = _basis_sha(clauses, as_of)
    report_sha = sha256_text(report_text)
    placed = [_with_place(item, parsed.blocks) for item in accepted]
    return CheckOutput(
        findings=placed,
        rejected=rejected,
        result_hash=citation_result_hash(
            report_sha=report_sha,
            basis_sha=basis_sha,
            as_of=as_of,
            findings=payloads,
        ),
    )


def _with_place(finding: Finding, blocks) -> Finding:
    place = describe_place(blocks, finding.char_start)
    if not place:
        return finding
    return finding.model_copy(update={"place": place})


def _timeliness(entry: BibliographyEntry, rows: list[RepealRow], clauses: list[BasisClauseView], as_of: date) -> Finding:
    matched = matching_repeal(entry.name, rows)
    replaced_by = None
    if matched is not None:
        verdict = "引用已失效" if as_of >= matched.repealed_on else "有效"
        replaced_by = matched.replaced_by or None
        question = "评价依据所列法律在基准日是否仍有效"
    elif _basis_title_in_force(entry.text, clauses, as_of):
        verdict = "有效"
        question = "评价依据所列文件是否属于已核验且已施行的依据"
    else:
        verdict = "超出依据范围"
        question = "评价依据所列文件是否属于依据库或失效映射"
    return Finding(
        kind="timeliness",
        question=question,
        verdict=verdict,
        evidence_span=entry.text,
        char_start=entry.char_start,
        char_end=entry.char_end,
        source_label="失效映射" if matched else "",
        replaced_by=replaced_by,
        as_of=as_of.isoformat(),
    )


def _limit_quotes(blocks, clauses, as_of: date, report_text: str):
    caption_at = _caption_index(blocks, "表7-2")
    if caption_at is None:
        return []
    caption = blocks[caption_at].text
    basis = [clause for clause in clauses if _same_standard(caption, clause) and _in_force(clause, as_of)]
    basis_quantities = _clause_quantities(basis)
    report_quantities = _table_quantities(blocks, caption_at, report_text)
    paired = []
    for item in report_quantities:
        matches = [other for other in basis_quantities if same_subject(item["quantity"], other["quantity"])]
        if len(matches) != 1:
            paired.append(
                (
                    _open_finding(
                        "quote_numeric",
                        "引用的剂量数值是否与依据原文一致",
                        "无法判定",
                        item,
                        as_of,
                    ),
                    None,
                )
            )
            continue
        chosen = matches[0]
        compared = compare_values("==", item["quantity"], chosen["quantity"])
        paired.append(
            (
                _measured_finding(
                    "quote_numeric",
                    "引用的剂量数值是否与依据原文一致",
                    verdict_of(compared["result"]),
                    item,
                    chosen,
                    compared["trace"],
                    "依据原文",
                    as_of,
                ),
                chosen["clause"],
            )
        )
    return paired


def _constraints(blocks, clauses, as_of: date):
    limits = _clause_quantities(
        [clause for clause in clauses if _in_force(clause, as_of) and "约束" not in clause.text]
    )
    paired = []
    for block in blocks:
        text = block.text or ""
        for quantity in extract_quantities(text):
            # 只认写下约束值的分句。后文「满足约束」「低于约束目标值」是在用这个数。
            clause, start, end = _constraint_clause(text, quantity)
            if not clause or _mentions_hj(clause) or not _assigns_constraint(clause, quantity):
                continue
            roles = roles_of(clause)
            population = {role for role in roles if role in {"工作人员", "职业", "公众"}}
            organs = {role for role in roles if role in {"眼晶体", "皮肤", "四肢"}}
            if len(population) != 1 or organs or quantity.unit != "mSv/a":
                continue
            located = {
                "quantity": quantity,
                "text": clause,
                "char_start": block.char_start + start,
                "char_end": block.char_start + end,
                "clause": None,
            }
            matches = [
                other
                for other in limits
                if other["quantity"].unit == quantity.unit
                and other["quantity"].population & population
                and not other["quantity"].organs
            ]
            if not matches:
                paired.append(
                    (
                        _open_finding("constraint_limit", "剂量约束是否不超过依据限值", "无法判定", located, as_of),
                        None,
                    )
                )
                continue
            for chosen in matches:
                compared = compare_values("<=", quantity, chosen["quantity"])
                paired.append(
                    (
                        _measured_finding(
                            "constraint_limit",
                            "剂量约束是否不超过依据限值",
                            verdict_of(compared["result"]),
                            located,
                            chosen,
                            compared["trace"],
                            "依据原文",
                            as_of,
                        ),
                        chosen["clause"],
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
                verdict=verdict_of(compared["result"]),
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


def _open_finding(kind: str, question: str, verdict: str, located: dict, as_of: date) -> Finding:
    return Finding(
        kind=kind,
        question=question,
        verdict=verdict,
        evidence_span=located["text"],
        char_start=located["char_start"],
        char_end=located["char_end"],
        as_of=as_of.isoformat(),
    )


def _measured_finding(kind, question, verdict, located, chosen, trace, source_label, as_of: date) -> Finding:
    clause = chosen["clause"]
    return Finding(
        kind=kind,
        question=question,
        verdict=verdict,
        evidence_span=located["text"],
        char_start=located["char_start"],
        char_end=located["char_end"],
        quote=chosen["quantity"].window,
        basis_doc_id=clause.doc_id,
        basis_path=clause.path,
        source_label=source_label,
        trace=trace,
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


def _basis_sha(clauses: list[BasisClauseView], as_of: date) -> str:
    parts = [clause.text_sha256 for clause in clauses if _in_force(clause, as_of)]
    return sha256_text("\n".join(sorted(parts)))


def _evidence_ok(report_text: str, finding: Finding) -> bool:
    if finding.char_start < 0 or finding.char_end > len(report_text) or finding.char_start >= finding.char_end:
        return False
    return report_text[finding.char_start : finding.char_end] == finding.evidence_span
