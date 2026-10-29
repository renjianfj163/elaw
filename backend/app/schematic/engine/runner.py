"""确定性规则执行器。

增加一条规则时：

1. 在 data/schematic_rules/engine_catalog.json 增加目录项，写明输入、证据和证据不足时如何降级。
2. 在 checks_*.py 写一个纯函数，只根据 Circuit 和已核验约束做判断。
3. 把函数登记到 CHECKERS。没有登记的规则留在 unimplemented，不会被当成通过。
4. 补六类测试：故障、正常、边界、资料缺失、多种解释、规则不适用。
5. 数值比较调用 app.rules.evaluator。缺的电阻、额定值或手册版本保持为空，不要填默认值。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.hashing import stable_hash
from app.schematic.engine.catalog import load_catalog
from app.schematic.engine.checks_analog import check_ana_001, check_ana_002, check_reg_001, check_reg_003
from app.schematic.engine.checks_bus import check_conn_007, check_conn_008, check_conn_009
from app.schematic.engine.checks_connectivity import (
    check_conn_001,
    check_conn_002,
    check_conn_003,
    check_conn_004,
    check_conn_005,
    check_conn_006,
)
from app.schematic.engine.checks_knowledge import check_kn_001
from app.schematic.engine.checks_power import (
    check_io_001,
    check_io_002,
    check_io_003,
    check_io_004,
    check_pwr_001,
    check_pwr_002,
    check_pwr_003,
    check_pwr_004,
    check_pwr_005,
)
from app.schematic.engine.checks_signal import (
    check_doc_001,
    check_if_001,
    check_if_002,
    check_if_003,
    check_if_004,
    check_if_006,
    check_if_010,
    check_ls_001,
    check_ls_002,
    check_ls_003,
    check_rst_004,
    check_rst_005,
    check_sys_002,
    check_usb_001,
)
from app.schematic.engine.dc import CircuitView
from app.schematic.engine.model import Circuit
from app.schematic.engine.report import CONFIDENCE, Finding, Report, Run, rollup_decision

CHECKERS = {
    "PWR-001": check_pwr_001,
    "PWR-002": check_pwr_002,
    "PWR-003": check_pwr_003,
    "PWR-004": check_pwr_004,
    "PWR-005": check_pwr_005,
    "IO-001": check_io_001,
    "IO-002": check_io_002,
    "IO-003": check_io_003,
    "IO-004": check_io_004,
    "LS-001": check_ls_001,
    "LS-002": check_ls_002,
    "LS-003": check_ls_003,
    "IF-001": check_if_001,
    "IF-002": check_if_002,
    "IF-003": check_if_003,
    "IF-004": check_if_004,
    "IF-006": check_if_006,
    "IF-010": check_if_010,
    "RST-004": check_rst_004,
    "RST-005": check_rst_005,
    "REG-001": check_reg_001,
    "REG-003": check_reg_003,
    "ANA-001": check_ana_001,
    "ANA-002": check_ana_002,
    "USB-001": check_usb_001,
    "SYS-002": check_sys_002,
    "DOC-001": check_doc_001,
    "KN-001": check_kn_001,
    "CONN-001": check_conn_001,
    "CONN-002": check_conn_002,
    "CONN-003": check_conn_003,
    "CONN-004": check_conn_004,
    "CONN-005": check_conn_005,
    "CONN-006": check_conn_006,
    "CONN-007": check_conn_007,
    "CONN-008": check_conn_008,
    "CONN-009": check_conn_009,
}

_PDF_NOTE = "连接来自图像识别，待网表确认。"


def load_circuit(path: Path | str) -> Circuit:
    file = Path(path)
    return Circuit.model_validate(json.loads(file.read_text(encoding="utf-8")))


def review_circuit(
    circuit: Circuit,
    *,
    only: list[str] | None = None,
    catalog: Path | str | None = None,
    knowledge=None,
) -> Report:
    version, rules = load_catalog(Path(catalog) if catalog else None)
    wanted = set(only or [])
    view = CircuitView(circuit, knowledge)
    produced: list[tuple[str, object]] = []
    for rule in rules:
        if rule.alias_of or rule.rule_id not in CHECKERS:
            continue
        if wanted and rule.rule_id not in wanted:
            continue
        result = CHECKERS[rule.rule_id](view, rule)
        for item in result.findings:
            _apply_source(item, circuit)
        produced.append((rule.rule_id, result))
    findings = [item for _rule_id, result in produced for item in result.findings]
    _number(findings)
    runs = [_run(rule_id, result) for rule_id, result in produced]
    by_rule = {item.rule_id: item for item in runs}
    for rule in rules:
        if not rule.alias_of:
            continue
        primary = by_rule.get(rule.alias_of)
        if primary is None:
            continue
        if wanted and rule.rule_id not in wanted and rule.alias_of not in wanted:
            continue
        runs.append(
            Run(
                rule_id=rule.rule_id,
                applicability=primary.applicability,
                outcome="alias",
                finding_ids=list(primary.finding_ids),
                note=f"由 {rule.alias_of} 执行，不另生成一条发现。",
            )
        )
    unimplemented = [rule.rule_id for rule in rules if rule.rule_id not in CHECKERS and not rule.alias_of]
    verdict = _verdict(findings, runs)
    knowledge_digest = knowledge.fingerprint() if knowledge is not None else ""
    digest = stable_hash(
        {
            "catalog": version,
            "knowledge": knowledge_digest,
            "verdict": verdict,
            "findings": [item.model_dump() for item in findings],
            "runs": [item.model_dump() for item in runs],
        }
    )
    limits = [
        "缺网表、型号或已核验手册数据时，结论是无法判定或待核实，不会记为通过。",
        "未实现的规则只出现在 unimplemented，不会被算成已检查通过。",
    ]
    if knowledge is not None:
        limits.append("确定结论只使用已复核、当前有效、且没有未消解冲突的硬性约束。推荐值不会升成强制要求。")
    if circuit.source == "pdf":
        limits.append("本次连接来自 PDF，已确认的连接类结论降为 probable。")
    return Report(
        verdict=verdict,
        findings=findings,
        runs=runs,
        unimplemented=unimplemented,
        result_hash=digest,
        catalog_version=version,
        limits=limits,
    )


def _apply_source(item: Finding, circuit: Circuit) -> None:
    if circuit.source != "pdf":
        item.confidence = CONFIDENCE[item.status]
        return
    if _PDF_NOTE not in item.assumptions:
        item.assumptions.append(_PDF_NOTE)
    if item.status == "confirmed":
        item.status = "probable"
        if item.decision == "confirmed_error":
            item.decision = "needs_review"
    item.confidence = CONFIDENCE[item.status]


def _number(findings: list[Finding]) -> None:
    seen: dict[str, int] = {}
    for item in findings:
        seen[item.rule_id] = seen.get(item.rule_id, 0) + 1
        item.finding_id = f"{item.rule_id}-{seen[item.rule_id]:03d}"


def _run(rule_id: str, result) -> Run:
    statuses = [item.status for item in result.findings]
    if result.applicability == "not_applicable":
        outcome = "not_applicable"
    elif statuses and all(item == "not_assessable" for item in statuses):
        outcome = "not_assessable"
    elif statuses:
        outcome = "findings"
    else:
        outcome = "clear"
    decision = rollup_decision(result.findings) or result.decision
    return Run(
        rule_id=rule_id,
        applicability=result.applicability,
        outcome=outcome,
        finding_ids=[item.finding_id for item in result.findings],
        note=result.note,
        decision=decision,
    )


def _verdict(findings: list[Finding], runs: list[Run]) -> str:
    if any(item.status in {"confirmed", "probable", "needs_review"} for item in findings):
        return "findings"
    if any(item.status == "not_assessable" for item in findings):
        return "not_assessable"
    executed = [item for item in runs if item.outcome != "alias"]
    if not executed or all(item.outcome == "not_applicable" for item in executed):
        return "no_applicable_rules"
    return "clear"
