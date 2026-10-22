from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.citation.check import BasisClauseView, admissible_clauses, check_report
from app.citation.limits import DoseLimit, load_verified_limits, subject_of_quantity
from app.citation.place import describe_place
from app.citation.gate import accept_quote
from app.citation.numeric import compare_values
from app.citation.quantities import Quantity, extract_quantities
from app.citation.timeliness import load_repeal_map, matching_repeal
from app.hashing import sha256_text
from app.parse.convert import ConvertError, find_soffice
from app.parse.docx_walk import parse_docx
from app.parse.cli import parse_report

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOC = REPO_ROOT / "data" / "samples" / "20241114-changgeng-radiotherapy.doc"
SAMPLE_DOCX = REPO_ROOT / "data" / "samples" / "m2"


def test_engine_converts_dose_units_and_rejects_bad_quotes():
    left = Quantity(1000, "uSv", "1000uSv", frozenset(), "1000uSv", 0, 7)
    right = Quantity(2, "mSv", "2mSv", frozenset(), "2mSv", 0, 4)
    compared = compare_values("<=", left, right)
    assert compared["result"] == "true"
    assert compared["trace"]

    reason = accept_quote(
        quote="并不存在的句子",
        source_text="职业工作人员连续五年平均有效剂量3mSv",
        report_text="报告写了3mSv",
        evidence_span="3mSv",
        char_start=3,
        char_end=7,
        in_force=True,
    )
    assert reason == "quote 不是依据原文的子串"


def test_short_environment_law_does_not_swallow_marine_law():
    rows = load_repeal_map()
    marine = matching_repeal("中华人民共和国海洋环境保护法", rows)
    plain = matching_repeal("中华人民共和国环境保护法", rows)
    assert marine is not None and marine.name == "海洋环境保护法"
    assert plain is not None and plain.name == "环境保护法"
    assert marine.replaced_by == ""
    assert plain.replaced_by == ""


def test_numeric_quote_constraint_and_prediction_use_the_engine():
    parsed = _document(
        [
            ("表7-2 剂量限值（GB18871-2002）", [(1, 1, 1)]),
            ("职业工作人员", [(1, 1, 1), (1, 1, 1)]),
            ("公众", [(1, 1, 1), (1, 1, 2)]),
            ("连续五年平均有效剂量3mSv", [(1, 1, 1), (1, 2, 1)]),
            ("年有效剂量1mSv", [(1, 1, 1), (1, 2, 2)]),
            ("辐射工作人员年受照剂量约束值取2mSv", []),
            ("对公众，本项目取0.5mSv/a作为剂量约束值。", []),
            ("职业人员和公众年附加有效剂量值分别为1.70E-1mSv、1.03E-3mSv，满足年剂量约束值（2mSv/a，0.1mSv/a）的要求。", []),
            ("表6评价依据", []),
            ("《中华人民共和国环境保护法》，2015年1月1日起实施。", [(2, 1, 2)]),
            ("《放射治疗辐射安全与防护要求》（HJ1198-2021）。", [(2, 1, 2)]),
        ]
    )
    clauses = [
        _clause("职业工作人员连续五年平均有效剂量3mSv", "B.1.1"),
        _clause("公众年有效剂量1mSv", "B.1.2"),
    ]
    limits = [
        _limit("B.1.1", "职业", "五年", 3, "连续五年平均有效剂量3mSv"),
        _limit("B.1.2", "公众", "年有效", 1, "年有效剂量1mSv"),
    ]
    first = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=load_repeal_map(), dose_limits=limits)
    second = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=load_repeal_map(), dose_limits=limits)
    assert first.result_hash == second.result_hash
    assert first.scope_note == "本次仅做引用核验，未对依据逐条审查"
    assert first.uncovered == ["屏蔽计算", "联锁", "监测", "管理制度"]
    assert first.coverage["checked"] == 2
    assert first.coverage["unchecked"] == 0

    quotes = [item for item in first.findings if item.kind == "quote_numeric"]
    assert [item.verdict for item in quotes] == ["与依据一致", "与依据一致"]
    assert all(item.trace and item.trace[-1]["op"] == "==" for item in quotes)
    assert all(item.quote in clauses[0].text or item.quote in clauses[1].text for item in quotes)

    changed = [_clause("职业工作人员连续五年平均有效剂量4mSv", "B.1.1"), clauses[1]]
    changed_limits = [_limit("B.1.1", "职业", "五年", 4, "连续五年平均有效剂量4mSv"), limits[1]]
    mismatched = check_report(
        parsed, as_of=date(2026, 10, 21), clauses=changed, repeal_rows=load_repeal_map(), dose_limits=changed_limits
    )
    occupational = next(item for item in mismatched.findings if item.kind == "quote_numeric" and "3mSv" in item.evidence_span)
    assert occupational.verdict == "不一致"

    constraints = [item for item in first.findings if item.kind == "constraint_limit"]
    assert constraints
    assert {item.verdict for item in constraints} == {"未超限值"}
    assert {item.source_label for item in constraints} == {"合理性检查"}
    assert any("工作人员" in item.evidence_span for item in constraints)
    assert any("公众" in item.evidence_span for item in constraints)

    predictions = [item for item in first.findings if item.kind == "prediction_constraint"]
    assert len(predictions) == 2
    assert {item.verdict for item in predictions} == {"内部自洽"}
    assert {item.source_label for item in predictions} == {"报告自设约束"}

    early = check_report(
        parsed, as_of=date(2024, 11, 14), clauses=clauses, repeal_rows=load_repeal_map(), dose_limits=limits
    )
    dated = check_report(
        parsed,
        as_of=date(2026, 10, 21),
        clauses=clauses,
        repeal_rows=load_repeal_map(),
        dose_limits=limits,
        compiled_on=date(2024, 11, 14),
    )
    assert _law(first.findings, "环境保护法").verdict == "基准日已废止"
    assert _law(early.findings, "环境保护法").verdict == "基准日有效"
    assert _law(dated.findings, "环境保护法").verdict == "编制时有效 / 基准日已废止"
    assert _law(first.findings, "环境保护法").replaced_by is None
    assert next(item for item in first.findings if "HJ1198" in item.evidence_span).verdict == "超出依据范围"

    stale = [_clause("职业工作人员连续五年平均有效剂量3mSv", "B.1.1", when=date(2027, 1, 1))]
    not_yet = check_report(parsed, as_of=date(2026, 10, 21), clauses=stale, repeal_rows=load_repeal_map(), dose_limits=limits)
    assert all(item.verdict == "无法核对" for item in not_yet.findings if item.kind == "quote_numeric")


def test_constraint_anchors_on_the_sentence_that_sets_the_value():
    setting = (
        "\t\t设备的操作人员相对固定，本项目所有从事放射治疗的辐射工作人员（放射治疗科）"
        "年受照剂量约束值取2mSv；对公众，本项目取0.1mSv/a作为剂量约束值。"
    )
    later_worker = (
        "因此对本项目辐射工作人员可不考虑剂量叠加的影响，即使叠加上医院放射治疗科2023年度的"
        "个人剂量值（低于1mSv/a），也是满足年剂量约束值的要求"
    )
    later_public = "在加速器机房周围50m评价范围内的公众的年剂量远小于4.14μSv，也低于设定的剂量约束目标值0.1mSv/a"
    restated = "公众、职业照射剂量约束值执行0.1mSv/a和2mSv/a。"
    same_cell = setting + "参照《放射治疗辐射安全与防护要求》（HJ1198-2021），边界外不大于2.5μSv/h。"
    parsed = _document(
        [
            (same_cell, []),
            (later_worker, []),
            (later_public, []),
            (restated, []),
            ("表6评价依据", []),
        ]
    )
    clauses = [
        _clause("职业工作人员连续五年平均有效剂量20mSv", "B.1.1"),
        _clause("公众年有效剂量1mSv", "B.1.2"),
    ]
    limits = [
        _limit("B.1.1", "职业", "五年", 20, "连续五年平均有效剂量20mSv"),
        _limit("B.1.2", "公众", "年有效", 1, "年有效剂量1mSv"),
    ]
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=[], dose_limits=limits)
    constraints = [item for item in checked.findings if item.kind == "constraint_limit"]
    spans = [item.evidence_span for item in constraints]
    assert len(spans) == 2
    assert any("约束值取2mSv" in span for span in spans)
    assert any("作为剂量约束值" in span for span in spans)
    assert all("低于1mSv" not in span and "4.14" not in span and "执行" not in span for span in spans)
    assert {item.verdict for item in constraints} == {"未超限值"}
    for item in constraints:
        assert parsed.full_text[item.char_start : item.char_end] == item.evidence_span


def test_dose_limit_phrase_keeps_the_subject_before_the_comma():
    occupational = (
        "B1.1.1.1 应对任何工作人员的职业照射水平进行控制，使之不超过下述限值：\n"
        "a）由审管部门决定的连续5年的年平均有效剂量（但不可作任何追溯性平均），20mSv；\n"
        "b）任何一年中的有效剂量，50mSv；\n"
        "c）眼晶体的年当量剂量，150mSv；\n"
        "d）四肢（手和足）或皮肤的年当量剂量，500mSv。"
    )
    public = (
        "B1.2.1 剂量限值\n"
        "实践使公众中有关关键人群组的成员所受到的平均剂量估计值不应超过下述限值：\n"
        "a）年有效剂量，1mSv；\n"
        "b）特殊情况下，如果5个连续年的年平均剂量不超过1mSv，则某一单一年份的有效剂量可提高到\n"
        "5mSv；\n"
        "c）眼晶体的年当量剂量，15mSv；\n"
        "d）皮肤的年当量剂量，50mSv。"
    )
    parsed = _document(
        [
            ("表7-2 剂量限值（GB18871-2002）", [(1, 1, 1)]),
            ("职业照射", [(1, 1, 1), (1, 1, 1)]),
            ("公众照射", [(1, 1, 1), (1, 1, 2)]),
            ("连续五年平均有效剂量20mSv", [(1, 1, 1), (1, 2, 1)]),
            ("且任何一年有效剂量50mSv", [(1, 1, 1), (1, 3, 1)]),
            ("眼晶体的当量剂量150mSv/a", [(1, 1, 1), (1, 4, 1)]),
            ("四肢或皮肤的当量剂量500mSv/a", [(1, 1, 1), (1, 5, 1)]),
            ("年有效剂量1mSv", [(1, 1, 1), (1, 2, 2)]),
            ("但连续五年平均值不超过1mSv时", [(1, 1, 1), (1, 3, 2)]),
            ("某一单一年可为5mSv", [(1, 1, 1), (1, 4, 2)]),
            ("眼晶体的当量剂量15mSv/a", [(1, 1, 1), (1, 5, 2)]),
            ("皮肤的当量剂量50mSv/a", [(1, 1, 1), (1, 6, 2)]),
        ]
    )
    clauses = [_clause(occupational, "B1.1.1.1"), _clause(public, "B1.2.1")]
    limits = _limits_from_clauses(clauses)
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=[], dose_limits=limits)
    quotes = [item for item in checked.findings if item.kind == "quote_numeric"]
    assert len(quotes) == 9
    assert {item.verdict for item in quotes} == {"与依据一致"}
    assert all(item.quote for item in quotes)
    fifties = [
        item
        for item in quotes
        if "50mSv" in item.evidence_span.replace("150", "").replace("500", "")
    ]
    assert len(fifties) == 2
    assert {item.basis_path for item in fifties} == {"B1.1.1.1", "B1.2.1"}
    bare = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=[], dose_limits=[])
    quotes = [item for item in bare.findings if item.kind == "quote_numeric"]
    assert {item.verdict for item in quotes} == {"无法核对"}
    assert quotes and all(item.reason == "依据库中无已核验的 GB 18871 条款" for item in quotes)


def test_verified_standard_matches_code_outside_the_book_title():
    parsed = _document(
        [
            ("表6评价依据", []),
            (" 《中华人民共和国环境保护法》，2015年1月1日起实施。", [(2, 1, 2)]),
            (" 《电离辐射防护与辐射源安全基本标准》（GB18871-2002）。", [(2, 1, 2)]),
            (" 《放射治疗辐射安全与防护要求》（HJ1198-2021）。", [(2, 1, 2)]),
        ]
    )
    rows = load_repeal_map()
    bare = check_report(parsed, as_of=date(2026, 10, 21), clauses=[], repeal_rows=rows)
    listed = [item for item in bare.findings if item.kind == "timeliness"]
    assert listed
    assert all(not item.evidence_span.startswith(" ") and not item.evidence_span.endswith(" ") for item in listed)
    for item in listed:
        assert parsed.full_text[item.char_start : item.char_end] == item.evidence_span
    assert _law(bare.findings, "环境保护法").verdict == "基准日已废止"
    standard = next(item for item in listed if "GB18871" in item.evidence_span)
    assert standard.verdict == "尚未核验"
    assert "GB 18871" in standard.reason
    verified = check_report(
        parsed,
        as_of=date(2026, 10, 21),
        clauses=[_clause("公众年有效剂量1mSv", "B.1.2")],
        repeal_rows=rows,
    )
    matched = next(item for item in verified.findings if item.kind == "timeliness" and "GB18871" in item.evidence_span)
    assert matched.verdict == "在依据库中"
    still_out = next(item for item in verified.findings if "HJ1198" in item.evidence_span)
    assert still_out.verdict == "超出依据范围"


def test_sample_report_cases():
    parsed = _load_sample()
    if parsed is None:
        pytest.skip("样例报告或 LibreOffice 不可用")
    rows = load_repeal_map()
    bare = check_report(parsed, as_of=date(2026, 10, 21), clauses=[], repeal_rows=rows)
    early = check_report(parsed, as_of=date(2024, 11, 14), clauses=[], repeal_rows=rows)
    for name in ("环境保护法", "环境影响评价法", "放射性污染防治法"):
        assert _law(bare.findings, name).verdict == "基准日已废止"
        assert _law(early.findings, name).verdict == "基准日有效"
        assert _law(bare.findings, name).replaced_by in (None, "")
        assert rows[[row.name for row in rows].index(name)].replaced_by == ""
    assert next(item for item in bare.findings if "HJ1198" in item.evidence_span).verdict == "超出依据范围"
    assert all(item.verdict == "无法核对" for item in bare.findings if item.kind == "quote_numeric")

    clauses = _clauses_from_sample_table(parsed)
    limits = _limits_from_clauses(clauses)
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=rows, dose_limits=limits)
    quotes = [item for item in checked.findings if item.kind == "quote_numeric"]
    assert len(quotes) >= 6
    assert {item.verdict for item in quotes} == {"与依据一致"}
    assert all(item.source_label == "已核验限值表" and item.trace[-1]["op"] == "==" for item in quotes)

    mutated = list(clauses)
    mutated[0] = _shift_first_number(mutated[0])
    shifted = check_report(
        parsed,
        as_of=date(2026, 10, 21),
        clauses=mutated,
        repeal_rows=rows,
        dose_limits=_limits_from_clauses(mutated),
    )
    assert any(item.verdict == "不一致" for item in shifted.findings if item.kind == "quote_numeric")

    constraints = [item for item in checked.findings if item.kind == "constraint_limit"]
    assert constraints and {item.verdict for item in constraints} == {"未超限值"}
    joined = " ".join(item.evidence_span for item in constraints)
    assert "约束值取2mSv" in joined
    assert "作为剂量约束值" in joined
    assert "低于1mSv" not in joined
    assert "约束目标值0.1" not in joined
    predictions = [item for item in checked.findings if item.kind == "prediction_constraint"]
    assert len(predictions) == 2
    assert {item.source_label for item in predictions} == {"报告自设约束"}
    assert {item.verdict for item in predictions} == {"内部自洽"}
    again = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=rows, dose_limits=limits)
    assert checked.result_hash == again.result_hash


def test_place_names_the_section_or_table():
    parsed = _document(
        [
            ("1.3.9本项目放射源倒装、运输和返回", []),
            ("表6  评价依据", []),
            ("法规", [(8, 1, 1)]),
            ("《中华人民共和国环境保护法》，2015年1月1日起实施。", [(8, 1, 2)]),
            ("7.3.1剂量限值", [(9, 1, 1)]),
            ("表7-2  工作人员职业照射和公众照射剂量限值", [(9, 1, 1)]),
            ("职业工作人员", [(9, 1, 1), (2, 1, 1)]),
            ("公众", [(9, 1, 1), (2, 1, 2)]),
            ("连续五年平均有效剂量20mSv", [(9, 1, 1), (2, 2, 1)]),
            ("眼晶体的当量剂量150mSv/a", [(9, 1, 1), (2, 3, 1)]),
            ("眼晶体的当量剂量15mSv/a", [(9, 1, 1), (2, 3, 2)]),
            ("7.3.2剂量约束值", [(9, 1, 1)]),
            ("年受照剂量约束值取2mSv", [(9, 1, 1)]),
            ("11.2.5工作人员和公众受照剂量估计", [(13, 1, 1)]),
            ("表11-12  公众的年附加有效剂量", [(13, 1, 1)]),
            ("7.76E-2", [(13, 1, 1)]),
            ("1.03", [(13, 1, 1)]),
            ("分别为1.70E-1mSv、1.03E-3mSv，满足年剂量约束值（2mSv/a，0.1mSv/a）", [(13, 1, 1)]),
        ]
    )
    for block in parsed.blocks:
        if block.text.startswith("《中华人民共和国环境保护法》"):
            block.section_path = "1.3.9"
        elif "20mSv" in block.text or block.text.startswith("眼晶体的当量剂量15"):
            block.section_path = "7.3.1"
        elif block.text.startswith("年受照剂量约束值"):
            block.section_path = "7.3.2"
        elif block.text.startswith("分别为1.70E-1mSv"):
            block.section_path = "11.2.5"

    def placed(prefix: str) -> str:
        block = next(item for item in parsed.blocks if item.text.startswith(prefix))
        return describe_place(parsed.blocks, block.char_start)

    law = placed("《中华人民共和国环境保护法》")
    assert law == "表6 评价依据 · 法规"
    assert "1.3.9" not in law
    assert placed("连续五年平均有效剂量20mSv") == "表7-2 · 职业工作人员 · 7.3.1"
    assert placed("眼晶体的当量剂量15mSv/a") == "表7-2 · 公众 · 7.3.1"
    assert placed("年受照剂量约束值取2mSv") == "7.3.2 剂量约束值"
    assert placed("分别为1.70E-1mSv") == "表11-12 · 11.2.5"


def _load_sample():
    if SAMPLE_DOCX.is_dir():
        found = next(SAMPLE_DOCX.glob("*.docx"), None)
        if found is not None:
            return parse_docx(found)
    if not SAMPLE_DOC.is_file():
        return None
    try:
        find_soffice()
    except ConvertError:
        return None
    return parse_report(SAMPLE_DOC)


def _clauses_from_sample_table(parsed) -> list[BasisClauseView]:
    caption = next(index for index, block in enumerate(parsed.blocks) if "表7-2" in (block.text or "").replace(" ", ""))
    prefix = tuple((step.table, step.row, step.col) for step in parsed.blocks[caption].table_path)
    cells = []
    seen = False
    for block in parsed.blocks[caption + 1 :]:
        key = tuple((step.table, step.row, step.col) for step in block.table_path)
        if len(key) > len(prefix) and key[: len(prefix)] == prefix:
            cells.append((key[len(prefix)], block.text or ""))
            seen = True
            continue
        if seen:
            break
    header_row = min(step[1] for step, _text in cells)
    headers: dict[int, str] = {}
    for step, text in cells:
        if step[1] == header_row and not extract_quantities(text):
            headers[step[2]] = headers.get(step[2], "") + text
    clauses = []
    number = 1
    for step, text in cells:
        if step[1] == header_row or not text.strip():
            continue
        header = headers.get(step[2], "")
        body = text
        for sep in ("，", "；", "、"):
            body = body.replace(sep, sep + header)
        clause_text = header + body
        clauses.append(_clause(clause_text, f"T.{number}"))
        number += 1
    return clauses


def _shift_first_number(clause: BasisClauseView) -> BasisClauseView:
    quantities = extract_quantities(clause.text)
    assert quantities
    raw = quantities[0].raw
    shifted = raw.replace(str(quantities[0].value).rstrip("0").rstrip("."), "9", 1)
    if shifted == raw:
        shifted = "9" + raw
    text = clause.text.replace(raw, shifted, 1)
    return clause.model_copy(update={"text": text, "text_sha256": sha256_text(text)})


def _law(findings, name: str):
    hits = [
        item
        for item in findings
        if item.kind == "timeliness" and name in item.evidence_span and "海洋环境保护法" not in item.evidence_span
    ]
    if name != "环境保护法":
        hits = [item for item in findings if item.kind == "timeliness" and name in item.evidence_span]
    assert len(hits) == 1
    return hits[0]


def test_constraint_evidence_stays_on_the_setting_section():
    parsed = _document(
        [
            ("7.3.2剂量约束值", []),
            ("本项目辐射工作人员年受照剂量约束值取2mSv。", []),
            ("11.2.5工作人员和公众受照剂量估计", []),
            (
                "职业人员和公众年附加有效剂量值分别为1.70E-1mSv、1.03E-3mSv，满足年剂量约束值（2mSv/a，0.1mSv/a）的要求。",
                [],
            ),
        ]
    )
    clause = _clause("职业工作人员连续五年平均有效剂量20mSv", "B.1.1")
    limits = [_limit("B.1.1", "职业", "五年", 20, "连续五年平均有效剂量20mSv")]
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=[clause], repeal_rows=[], dose_limits=limits)
    constraints = [item for item in checked.findings if item.kind == "constraint_limit"]
    assert len(constraints) == 1
    assert "7.3.2" in constraints[0].place
    assert "11.2" not in constraints[0].place
    assert "分别为" not in constraints[0].evidence_span
    assert constraints[0].verdict == "未超限值"
    predictions = [item for item in checked.findings if item.kind == "prediction_constraint"]
    assert predictions
    assert all("11.2.5" in item.place for item in predictions)
    assert all("7.3.2" not in item.place for item in predictions)


def test_textbooks_are_skipped_and_ordinances_stay_out_of_scope():
    parsed = _document(
        [
            ("表6评价依据", []),
            ("《中华人民共和国环境保护法》", [(2, 1, 2)]),
            ("《辐射防护培训教材》", [(2, 1, 2)]),
            ("《放射治疗辐射安全与防护要求》（HJ1198-2021）", [(2, 1, 2)]),
            ("《建设项目环境保护管理条例》", [(2, 1, 2)]),
        ]
    )
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=[], repeal_rows=load_repeal_map(), dose_limits=[])
    assert _law(checked.findings, "环境保护法").verdict == "基准日已废止"
    textbook = next(item for item in checked.findings if "培训教材" in item.evidence_span)
    assert textbook.verdict == "非规范性文件，跳过"
    assert next(item for item in checked.findings if "HJ1198" in item.evidence_span).verdict == "超出依据范围"
    ordinance = next(item for item in checked.findings if "建设项目环境保护管理条例" in item.evidence_span)
    assert ordinance.verdict == "超出依据范围"


def test_reference_material_and_ncrp_are_not_out_of_scope():
    parsed = _document(
        [
            ("表6评价依据", []),
            ("法规", [(2, 1, 1)]),
            ("《中华人民共和国环境保护法》", [(2, 1, 2)]),
            ("技术标准", [(2, 2, 1)]),
            ("《电离辐射防护与辐射源安全基本标准》（GB18871-2002）", [(2, 2, 2)]),
            ("技术资料", [(2, 3, 1)]),
            ("NCRP第151号报告", [(2, 3, 2)]),
            ("《辐射防护培训教材》", [(2, 3, 2)]),
        ]
    )
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=[], repeal_rows=load_repeal_map(), dose_limits=[])
    assert not any(item.evidence_span.strip() == "技术资料" for item in checked.findings)
    ncrp = next(item for item in checked.findings if "NCRP" in item.evidence_span)
    assert ncrp.verdict == "非规范性文件，跳过"
    assert ncrp.section == "技术资料"
    textbook = next(item for item in checked.findings if "培训教材" in item.evidence_span)
    assert textbook.verdict == "非规范性文件，跳过"
    standard = next(item for item in checked.findings if "GB18871" in item.evidence_span)
    assert standard.verdict == "尚未核验"
    assert standard.section == "技术标准"
    assert "依据库中无已核验的 GB 18871 条款" == standard.reason
    outside = [item for item in checked.findings if item.verdict == "超出依据范围"]
    assert outside == []


def test_repealed_law_is_not_an_admissible_basis():
    text = "这里只是一条已核验正文。"
    clause = BasisClauseView(
        doc_id="env-law",
        title="中华人民共和国环境保护法",
        path="1",
        text=text,
        text_sha256=sha256_text(text),
        effective_date=date(2015, 1, 1),
        verified=True,
    )
    rows = load_repeal_map()
    assert admissible_clauses([clause], date(2026, 10, 21), rows) == []
    assert len(admissible_clauses([clause], date(2024, 11, 14), rows)) == 1


def test_unverified_dose_limit_is_ignored(tmp_path):
    path = tmp_path / "dose_limits.csv"
    path.write_text(
        "doc_id,clause_path,population,organ,period,value,unit,quote,verified\n"
        "GB18871-2002,B.1.1,职业,,五年,20,mSv/a,20mSv,false\n"
        "GB18871-2002,B.1.2,公众,,年有效,1,mSv/a,1mSv,true\n",
        encoding="utf-8",
    )
    rows = load_verified_limits(path)
    assert len(rows) == 1
    assert rows[0].clause_path == "B.1.2"


def test_copied_wording_matches_a_verified_clause():
    control = _clause(
        "6.4.1.1 注册者和许可证持有者应把需要和可能需要专门防护手段或安全措施的区域定为控制区。",
        "6.4.1.1",
    )
    supervised = _clause(
        "6.4.2.1 这种区域未被定为控制区，在其中通常不需要专门的防护手段或安全措施，但需要经常对职业照射条件进行监督和评价。",
        "6.4.2.1",
    )
    report = (
        "根据《电离辐射防护与辐射源安全基本标准》（GB18871-2002）的规定，"
        "控制区是指需要和可能需要专门防护手段或安全措施的区域；"
        "监督区是指通常不需要专门的防护手段或安全措施，但需要经常对职业照射条件进行监督和评价的区域。"
    )
    parsed = _document([(report, [])])
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=[control, supervised], repeal_rows=[], dose_limits=[])
    copied = [item for item in checked.findings if item.kind == "quote_text"]
    assert {item.basis_path for item in copied} == {"6.4.1.1", "6.4.2.1"}
    assert {item.verdict for item in copied} == {"与依据一致"}
    sources = {"6.4.1.1": control.text, "6.4.2.1": supervised.text}
    for item in copied:
        assert item.quote in sources[item.basis_path]
        assert normalize_space(item.quote) in normalize_space(report)
        assert parsed.full_text[item.char_start : item.char_end] == item.evidence_span
    named_only = _document([("《电离辐射防护与辐射源安全基本标准》（GB18871-2002）。", [])])
    listed = check_report(named_only, as_of=date(2026, 10, 21), clauses=[control], repeal_rows=[], dose_limits=[])
    assert [item for item in listed.findings if item.kind == "quote_text"] == []
    hidden = control.model_copy(update={"verified": False})
    blocked = check_report(parsed, as_of=date(2026, 10, 21), clauses=[hidden, supervised], repeal_rows=[], dose_limits=[])
    assert {item.basis_path for item in blocked.findings if item.kind == "quote_text"} == {"6.4.2.1"}


def normalize_space(text: str) -> str:
    return "".join(text.split())


def _limit(path: str, population: str, period: str, value: float, quote: str, organ: str = "") -> DoseLimit:
    return DoseLimit(
        doc_id="GB18871-2002",
        clause_path=path,
        population=population,
        organ=organ,
        period=period,
        value=value,
        unit="mSv/a",
        quote=quote,
        verified=True,
    )


def _limits_from_clauses(clauses: list[BasisClauseView]) -> list[DoseLimit]:
    rows = []
    for clause in clauses:
        for quantity in extract_quantities(clause.text):
            subject = subject_of_quantity(quantity)
            assert subject is not None, clause.text
            population, organ, period = subject
            rows.append(
                DoseLimit(
                    doc_id=clause.doc_id,
                    clause_path=clause.path,
                    population=population,
                    organ=organ,
                    period=period,
                    value=quantity.value,
                    unit=quantity.unit,
                    quote=quantity.window,
                    verified=True,
                )
            )
    return rows


def _clause(text: str, path: str, *, when: date = date(2003, 4, 1)) -> BasisClauseView:
    return BasisClauseView(
        doc_id="GB18871-2002",
        title="电离辐射防护与辐射源安全基本标准 GB18871-2002",
        path=path,
        text=text,
        text_sha256=sha256_text(text),
        effective_date=when,
        verified=True,
    )


def _document(parts: list[tuple[str, list[tuple[int, int, int]]]]):
    blocks = []
    chunks: list[str] = []
    cursor = 0
    for text, path in parts:
        if chunks:
            chunks.append("\n")
            cursor += 1
        start = cursor
        chunks.append(text)
        cursor += len(text)
        blocks.append(
            SimpleNamespace(
                text=text,
                char_start=start,
                char_end=cursor,
                table_path=[SimpleNamespace(table=table, row=row, col=col) for table, row, col in path],
                section_path="",
                type="cell" if path else "para",
            )
        )
    return SimpleNamespace(blocks=blocks, full_text="".join(chunks))
