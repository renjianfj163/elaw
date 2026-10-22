from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.citation.check import BasisClauseView, check_report
from app.citation.gate import accept_quote, citation_result_hash
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
    first = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=load_repeal_map())
    second = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=load_repeal_map())
    assert first.result_hash == second.result_hash

    quotes = [item for item in first.findings if item.kind == "quote_numeric"]
    assert [item.verdict for item in quotes] == ["符合", "符合"]
    assert all(item.trace and item.trace[-1]["op"] == "==" for item in quotes)
    assert all(item.quote in clauses[0].text or item.quote in clauses[1].text for item in quotes)

    changed = [_clause("职业工作人员连续五年平均有效剂量4mSv", "B.1.1"), clauses[1]]
    mismatched = check_report(parsed, as_of=date(2026, 10, 21), clauses=changed, repeal_rows=load_repeal_map())
    occupational = next(item for item in mismatched.findings if item.kind == "quote_numeric" and "3mSv" in item.evidence_span)
    assert occupational.verdict == "不符合"

    constraints = [item for item in first.findings if item.kind == "constraint_limit"]
    assert constraints
    assert {item.verdict for item in constraints} == {"符合"}
    assert any("工作人员" in item.evidence_span for item in constraints)
    assert any("公众" in item.evidence_span for item in constraints)

    predictions = [item for item in first.findings if item.kind == "prediction_constraint"]
    assert len(predictions) == 2
    assert {item.verdict for item in predictions} == {"符合"}
    assert {item.source_label for item in predictions} == {"报告自设约束"}

    early = check_report(parsed, as_of=date(2024, 11, 14), clauses=clauses, repeal_rows=load_repeal_map())
    assert _law(first.findings, "环境保护法").verdict == "引用已失效"
    assert _law(early.findings, "环境保护法").verdict == "有效"
    assert _law(first.findings, "环境保护法").replaced_by is None
    assert next(item for item in first.findings if "HJ1198" in item.evidence_span).verdict == "超出依据范围"

    stale = [_clause("职业工作人员连续五年平均有效剂量3mSv", "B.1.1", when=date(2027, 1, 1))]
    not_yet = check_report(parsed, as_of=date(2026, 10, 21), clauses=stale, repeal_rows=load_repeal_map())
    assert all(item.verdict == "无法判定" for item in not_yet.findings if item.kind == "quote_numeric")


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
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=[])
    constraints = [item for item in checked.findings if item.kind == "constraint_limit"]
    spans = [item.evidence_span for item in constraints]
    assert len(spans) == 2
    assert any("约束值取2mSv" in span for span in spans)
    assert any("作为剂量约束值" in span for span in spans)
    assert all("低于1mSv" not in span and "4.14" not in span and "执行" not in span for span in spans)
    assert {item.verdict for item in constraints} == {"符合"}
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
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=[])
    quotes = [item for item in checked.findings if item.kind == "quote_numeric"]
    assert len(quotes) == 9
    assert {item.verdict for item in quotes} == {"符合"}
    assert all(item.quote for item in quotes)


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
    assert _law(bare.findings, "环境保护法").verdict == "引用已失效"
    standard = next(item for item in listed if "GB18871" in item.evidence_span)
    assert standard.verdict == "超出依据范围"
    verified = check_report(
        parsed,
        as_of=date(2026, 10, 21),
        clauses=[_clause("公众年有效剂量1mSv", "B.1.2")],
        repeal_rows=rows,
    )
    matched = next(item for item in verified.findings if item.kind == "timeliness" and "GB18871" in item.evidence_span)
    assert matched.verdict == "有效"
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
        assert _law(bare.findings, name).verdict == "引用已失效"
        assert _law(early.findings, name).verdict == "有效"
        assert _law(bare.findings, name).replaced_by in (None, "")
        assert rows[[row.name for row in rows].index(name)].replaced_by == ""
    assert next(item for item in bare.findings if "HJ1198" in item.evidence_span).verdict == "超出依据范围"
    assert all(item.verdict == "无法判定" for item in bare.findings if item.kind == "quote_numeric")

    clauses = _clauses_from_sample_table(parsed)
    checked = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=rows)
    quotes = [item for item in checked.findings if item.kind == "quote_numeric"]
    assert len(quotes) >= 6
    assert {item.verdict for item in quotes} == {"符合"}
    assert all(item.source_label == "依据原文" and item.trace[-1]["op"] == "==" for item in quotes)

    mutated = list(clauses)
    mutated[0] = _shift_first_number(mutated[0])
    shifted = check_report(parsed, as_of=date(2026, 10, 21), clauses=mutated, repeal_rows=rows)
    assert any(item.verdict == "不符合" for item in shifted.findings if item.kind == "quote_numeric")

    constraints = [item for item in checked.findings if item.kind == "constraint_limit"]
    assert constraints and {item.verdict for item in constraints} == {"符合"}
    joined = " ".join(item.evidence_span for item in constraints)
    assert "约束值取2mSv" in joined
    assert "作为剂量约束值" in joined
    assert "低于1mSv" not in joined
    assert "约束目标值0.1" not in joined
    predictions = [item for item in checked.findings if item.kind == "prediction_constraint"]
    assert len(predictions) == 2
    assert {item.source_label for item in predictions} == {"报告自设约束"}
    assert {item.verdict for item in predictions} == {"符合"}
    again = check_report(parsed, as_of=date(2026, 10, 21), clauses=clauses, repeal_rows=rows)
    assert checked.result_hash == again.result_hash


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
