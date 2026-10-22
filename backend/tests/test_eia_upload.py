import io
import zipfile
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.citation.check import BasisClauseView
from app.hashing import sha256_text
from app.main import SessionFactory, app
from app.models import ReviewLog

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(name: str = "环境保护法") -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}"><w:body>
  <w:p><w:r><w:t>表6 评价依据</w:t></w:r></w:p>
  <w:tbl><w:tr><w:tc><w:p><w:r><w:t>法规</w:t></w:r></w:p></w:tc></w:tr>
  <w:tr><w:tc><w:p><w:r><w:t>《中华人民共和国{name}》</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
</w:body></w:document>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_report_upload_accepts_many_files_and_keeps_each_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(app)
    first_doc = _docx("环境保护法")
    second_doc = _docx("环境影响评价法")
    uploaded = client.post(
        "/api/eia/check",
        files=[
            ("files", ("甲.docx", first_doc, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ("files", ("乙.docx", second_doc, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ("files", ("说明.txt", b"hello", "text/plain")),
        ],
        data={"as_of": "2026-10-21"},
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["disclaimer"] == "辅助预审，不构成法律意见。"
    assert [item["filename"] for item in body["items"]] == ["甲.docx", "乙.docx", "说明.txt"]
    assert body["items"][0]["ok"] is True
    assert body["items"][1]["ok"] is True
    assert body["items"][0]["result_hash"] != body["items"][1]["result_hash"]
    assert body["items"][2] == {"ok": False, "filename": "说明.txt", "error": "只接受 .doc、.docx 或 .pdf"}
    again = client.post(
        "/api/eia/check",
        files=[("files", ("甲-副本.docx", first_doc, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"as_of": "2026-10-21"},
    )
    reused = again.json()["items"][0]
    assert reused["ok"] is True
    assert reused["reused"] is True
    assert reused["filename"] == "甲-副本.docx"
    assert reused["result_hash"] == body["items"][0]["result_hash"]
    assert body["items"][0]["scope_note"] == "本次仅做引用核验，未对依据逐条审查"
    assert body["items"][0]["uncovered"] == ["屏蔽计算", "联锁", "监测", "管理制度"]
    assert "checked" in body["items"][0]["coverage"]
    listed = client.get("/api/eia/reports")
    names = [item["filename"] for item in listed.json()["items"]]
    assert names == ["乙.docx", "甲.docx"]


def test_delete_checked_report_lets_the_same_file_run_again(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(app)
    document = _docx("固体废物污染环境防治法")
    uploaded = client.post(
        "/api/eia/check",
        files=[("files", ("甲.docx", document, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"as_of": "2026-10-21"},
    )
    item = uploaded.json()["items"][0]
    assert item["ok"] is True
    finding = item["findings"][0]
    reviewed = client.post(
        f"/api/eia/reports/{item['id']}/review",
        json={"key": finding["key"], "action": "confirm", "by": "测试", "note": "先记下"},
    )
    assert reviewed.status_code == 200
    stored = list((tmp_path / "reports").glob("*.docx"))
    assert len(stored) == 1
    removed = client.delete(f"/api/eia/reports/{item['id']}")
    assert removed.status_code == 200
    assert removed.json()["id"] == item["id"]
    listed = client.get("/api/eia/reports").json()["items"]
    assert item["id"] not in [row["id"] for row in listed]
    assert list((tmp_path / "reports").glob("*.docx")) == []
    db = SessionFactory()
    try:
        left = db.scalar(select(func.count()).select_from(ReviewLog).where(ReviewLog.target_id == item["id"]))
    finally:
        db.close()
    assert left == 0
    assert client.delete(f"/api/eia/reports/{item['id']}").status_code == 404
    again = client.post(
        "/api/eia/check",
        files=[("files", ("甲.docx", document, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"as_of": "2026-10-21"},
    )
    second = again.json()["items"][0]
    assert second["ok"] is True
    assert second["reused"] is False
    assert second["id"] != item["id"]
    assert second["reviews"] == []


def test_review_logs_override_without_touching_result_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(app)
    uploaded = client.post(
        "/api/eia/check",
        files=[("files", ("甲.docx", _docx("环境保护法"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"as_of": "2026-10-21"},
    )
    item = uploaded.json()["items"][0]
    assert item["ok"] is True
    assert item["id"]
    assert item["reviews"] == []
    assert item["findings"], "样例应抽出至少一条引用"
    finding = item["findings"][0]
    saved_hash = item["result_hash"]
    original = finding["verdict"]
    reviewed = client.post(
        f"/api/eia/reports/{item['id']}/review",
        json={
            "key": finding["key"],
            "action": "override",
            "by": "测试",
            "verdict": "不符合",
            "note": "对照原文后改判",
        },
    )
    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["result_hash"] == saved_hash
    assert body["reviews"][-1]["action"] == "override"
    assert body["reviews"][-1]["verdict"] == "不符合"
    assert body["reviews"][-1]["note"] == "对照原文后改判"
    again = next(row for row in client.get("/api/eia/reports").json()["items"] if row["id"] == item["id"])
    assert again["result_hash"] == saved_hash
    assert again["findings"][0]["verdict"] == original
    refused = client.post(
        f"/api/eia/reports/{item['id']}/review",
        json={"key": finding["key"], "action": "override", "by": "测试", "verdict": "不符合", "note": " "},
    )
    assert refused.status_code == 400


def test_reupload_runs_again_when_the_basis_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    holder = {"rows": []}
    monkeypatch.setattr("app.report.api._load_clauses", lambda as_of: holder["rows"])
    client = TestClient(app)
    document = _docx("噪声污染防治法")
    first = client.post(
        "/api/eia/check",
        files=[("files", ("甲.docx", document, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"as_of": "2026-10-21"},
    ).json()["items"][0]
    assert first["reused"] is False
    text = "公众年有效剂量1mSv"
    holder["rows"] = [
        BasisClauseView(
            doc_id="GB18871-2002",
            title="电离辐射防护与辐射源安全基本标准 GB18871-2002",
            path="B.1.2",
            text=text,
            text_sha256=sha256_text(text),
            effective_date=date(2003, 4, 1),
            verified=True,
        )
    ]
    second = client.post(
        "/api/eia/check",
        files=[("files", ("甲.docx", document, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))],
        data={"as_of": "2026-10-21"},
    ).json()["items"][0]
    assert second["ok"] is True
    assert second["reused"] is False
    assert second["result_hash"] != first["result_hash"]
    assert second["id"] != first["id"]
    assert second["basis_in_force"] == 1


def test_filename_date_shows_both_sides_of_a_repeal(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(app)
    uploaded = client.post(
        "/api/eia/check",
        files=[
            (
                "files",
                ("20241114长庚医院.docx", _docx("环境保护法"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            )
        ],
        data={"as_of": "2026-10-21"},
    )
    item = uploaded.json()["items"][0]
    assert item["ok"] is True
    assert item["compiled_on"] == "2024-11-14"
    assert item["compiled_from"] == "filename"
    law = next(row for row in item["findings"] if "环境保护法" in row["evidence_span"])
    assert law["verdict"] == "编制时有效 / 基准日已废止"
    assert "不是报告写错了" in law["reason"]
    assert item["coverage"]["checked"] == 0
    assert item["coverage"]["unchecked"] == 0
