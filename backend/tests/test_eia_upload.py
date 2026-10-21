import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app

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
    assert body["items"][2] == {"ok": False, "filename": "说明.txt", "error": "只接受 .doc 或 .docx"}
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
    listed = client.get("/api/eia/reports")
    names = [item["filename"] for item in listed.json()["items"]]
    assert names == ["乙.docx", "甲.docx"]


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
