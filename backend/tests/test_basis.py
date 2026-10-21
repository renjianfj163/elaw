from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.basis.cli import main
from app.basis.search import search_basis
from app.basis.store import (
    BasisError,
    add_document,
    disable_document,
    evaluable_clauses,
    open_session,
    verify_clause,
)
from app.basis.validate import BasisValidationError, attach_hashes, validate_pieces
from app.laws.splitter import cn_to_int, split_legal, split_standard
from app.models import ReviewLog


LEGAL = "\n".join(
    [
        "第一编 总则",
        "第一章 基本规定",
        "第一节 范围",
        "第一条 这里写入 ALPHAONLY 供检索。",
        "",
        "本条第二款单独成段。",
        "第二条 这里写入 BETAONLY 但先不核验。",
    ]
)


def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return "sqlite:///" + (tmp_path / "basis.db").as_posix()


def _write_basis(tmp_path: Path, name: str, text: str) -> Path:
    folder = tmp_path / "basis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_cn_to_int_and_structure_split():
    assert cn_to_int("十二") == 12
    assert cn_to_int("二十") == 20
    assert cn_to_int("一千二百四十二") == 1242
    pieces = split_legal(LEGAL)
    paths = [item["path"] for item in pieces]
    assert paths[0] == "第一编/第一章/第一节/第一条/第一款"
    assert "第一编/第一章/第一节/第一条/第二款" in paths
    assert split_standard("4.1 总要求\n应当写明。\n74.0 天\nB.1.1 附录条款\n说明。") == [
        {"path": "4.1", "text": "4.1 总要求\n应当写明。\n74.0 天"},
        {"path": "B.1.1", "text": "B.1.1 附录条款\n说明。"},
    ]
    standard = "\n".join(
        [
            "1 范围",
            "适用于示例。",
            "1.5",
            "2.22×10",
            "4.1 总要求",
            "应当写明。",
            "4.2.2的要求见下款。",
            "A1 附录标题",
            "A1.1 附录条款",
            "正文。",
            "2 这是脚注，不是章标题。",
        ]
    )
    assert [item["path"] for item in split_standard(standard)] == ["1", "4.1", "A1", "A1.1"]
    assert validate_pieces("国家标准", attach_hashes(split_standard(standard)))["clause_count"] == 4


def test_reversed_order_and_hash_are_rejected():
    backwards = attach_hashes(
        [
            {"path": "第一章/第二条/第一款", "text": "后条在前。"},
            {"path": "第一章/第一条/第一款", "text": "前条在后。"},
        ]
    )
    with pytest.raises(BasisValidationError, match="顺序颠倒"):
        validate_pieces("法律", backwards)
    tampered = attach_hashes([{"path": "4.2", "text": "甲"}])
    tampered[0]["text_sha256"] = "0" * 64
    with pytest.raises(BasisValidationError, match="sha256"):
        validate_pieces("国家标准", tampered)


def test_unverified_clause_is_not_searchable(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    source = _write_basis(tmp_path, "demo.txt", LEGAL)
    db = open_session(url)
    document = add_document(
        db,
        doc_id="demo-law",
        title="切分样例",
        kind="法律",
        effective_date=date(2026, 8, 15),
        publish_date=date(2026, 3, 12),
        source_note="人工上传",
        file_path=source,
        by="核验人",
        expect_count=2,
    )
    db.commit()
    assert document.clause_count >= 2
    assert evaluable_clauses(db, date(2026, 10, 21)) == []
    assert search_basis(db, "ALPHAONLY", date(2026, 10, 21)) == []

    verify_clause(db, doc_id="demo-law", path="第一编/第一章/第一节/第一条/第一款", by="核验人", note="已对照原文")
    db.commit()
    too_early = search_basis(db, "ALPHAONLY", date(2026, 8, 14))
    ready = search_basis(db, "ALPHAONLY", date(2026, 8, 15))
    hidden = search_basis(db, "BETAONLY", date(2026, 10, 21))
    assert too_early == []
    assert len(ready) == 1
    assert ready[0]["path"].endswith("第一条/第一款")
    assert ready[0]["text_sha256"]
    assert hidden == []
    db.close()


def test_disable_keeps_file_and_writes_review_log(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    source = _write_basis(tmp_path, "demo.txt", "4.1 总要求\n应当保留原文。\n")
    db = open_session(url)
    add_document(
        db,
        doc_id="GB18871-2002",
        title="标准样例",
        kind="国家标准",
        effective_date=date(2003, 4, 1),
        source_note="人工上传",
        file_path=source,
        by="核验人",
    )
    verify_clause(db, doc_id="GB18871-2002", path="4.1", by="核验人")
    db.commit()
    assert search_basis(db, "应当保留原文", date(2026, 10, 21))
    disable_document(db, doc_id="GB18871-2002", by="核验人", reason="被后续依据替代")
    db.commit()
    assert source.is_file()
    assert search_basis(db, "应当保留原文", date(2026, 10, 21)) == []
    actions = list(db.scalars(select(ReviewLog.action)))
    assert actions == ["add", "verify", "disable"]
    with pytest.raises(BasisError, match="已在库中"):
        add_document(
            db,
            doc_id="GB18871-2002",
            title="标准样例",
            kind="国家标准",
            effective_date=date(2003, 4, 1),
            source_note="人工上传",
            file_path=source,
            by="核验人",
        )
    db.close()


def test_rejects_files_outside_basis_and_pending_pdf(tmp_path, monkeypatch):
    url = _db(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("第一条 不应入库。\n", encoding="utf-8")
    pending = _write_basis(tmp_path, "1739780846955_94511.pdf", "")
    db = open_session(url)
    with pytest.raises(BasisError, match="data/basis"):
        add_document(
            db,
            doc_id="outside",
            title="范围外",
            kind="法律",
            effective_date=date(2026, 8, 15),
            source_note="人工上传",
            file_path=outside,
            by="核验人",
        )
    with pytest.raises(BasisError, match="尚未确认标题"):
        add_document(
            db,
            doc_id="pending-pdf",
            title="待确认",
            kind="其他",
            effective_date=date(2003, 4, 1),
            source_note="人工上传",
            file_path=pending,
            by="核验人",
        )
    db.close()


def test_cli_add_list(tmp_path, monkeypatch, capsys):
    url = _db(tmp_path, monkeypatch)
    source = _write_basis(tmp_path, "demo.txt", LEGAL)
    main(
        [
            "--database-url",
            url,
            "add",
            "--doc-id",
            "demo-law",
            "--title",
            "切分样例",
            "--kind",
            "法律",
            "--effective",
            "2026-08-15",
            "--source",
            "人工上传",
            "--file",
            str(source),
            "--by",
            "核验人",
            "--expect-count",
            "2",
        ]
    )
    main(["--database-url", url, "list"])
    output = capsys.readouterr().out
    assert "已入库 demo-law" in output
    assert "demo-law\tactive\t法律\t2026-08-15" in output
