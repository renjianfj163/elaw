import io

from fastapi.testclient import TestClient

from app.main import app
from app.parse.docx_walk import ParseError
from app.parse.pdf_text import PDF_PARSER_VERSION, _lines, parse_pdf_bytes


def _pdf(pages: list[list[str]]) -> bytes:
    """最小文字层 PDF。每页若干行，只含 Latin-1。"""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    streams = []
    for lines in pages:
        commands = ["BT", "/F1 12 Tf", "72 700 Td"]
        for index, line in enumerate(lines):
            if index:
                commands.append("0 -16 Td")
            commands.append(f"({escape(line)}) Tj")
        if not lines:
            commands = ["BT", "ET"]
        else:
            commands.append("ET")
        streams.append("\n".join(commands).encode("latin-1"))

    page_count = len(streams)
    content_ids = [4 + index * 2 for index in range(page_count)]
    page_ids = [5 + index * 2 for index in range(page_count)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    bodies: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {page_count} /Kids [{kids}] >>".encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, stream in enumerate(streams):
        bodies.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        bodies.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_ids[index]} 0 R "
                f"/Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode("latin-1")
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(bodies, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode("latin-1"))
        out.write(body)
        out.write(b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(bodies) + 1}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
    out.write(
        (
            f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("latin-1")
    )
    return out.getvalue()


def test_pdf_text_becomes_paragraphs_and_marks_blank_pages():
    parsed = parse_pdf_bytes(_pdf([["10.1.2", "GB 18871-2002"], []]), source_name="报告.pdf")
    assert parsed.parser_version == PDF_PARSER_VERSION
    assert parsed.stats.table_count == 0
    assert parsed.stats.image_block_count == 1
    texts = [block.text for block in parsed.blocks if block.type == "para"]
    assert texts == ["10.1.2", "GB 18871-2002"]
    assert parsed.blocks[0].section_path == "10.1.2"
    assert parsed.blocks[1].section_path == "10.1.2"
    assert parsed.full_text == "10.1.2\nGB 18871-2002"
    assert parsed.blocks[0].char_start == 0
    assert parsed.blocks[1].char_start == len("10.1.2") + 1
    image = parsed.blocks[-1]
    assert image.type == "image"
    assert image.not_parsed is True
    assert "表格没有按单元格拆开" in parsed.parse_note


def test_pdf_without_text_is_refused():
    try:
        parse_pdf_bytes(_pdf([[]]), source_name="扫描.pdf")
    except ParseError as exc:
        assert "没有可复制的文字" in str(exc)
    else:
        raise AssertionError("扫描件应被拒绝")
    try:
        parse_pdf_bytes(b"not a pdf", source_name="坏.pdf")
    except ParseError as exc:
        assert str(exc) == "PDF 无法读取"
    else:
        raise AssertionError("坏文件应被拒绝")


def test_cjk_spaces_inside_a_line_are_joined():
    assert _lines("中 华 人 民 共 和 国\n表 7-2") == ["中华人民共和国", "表 7-2"]


def test_report_upload_accepts_text_pdf_and_rejects_a_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    client = TestClient(app)
    uploaded = client.post(
        "/api/eia/check",
        files=[("files", ("报告.pdf", _pdf([["10.1.2", "GB 18871-2002"], []]), "application/pdf"))],
        data={"as_of": "2026-10-21"},
    )
    assert uploaded.status_code == 200
    item = uploaded.json()["items"][0]
    assert item["ok"] is True
    assert item["filename"] == "报告.pdf"
    assert item["stats"]["images"] == 1
    assert item["stats"]["tables"] == 0
    assert "表格没有按单元格拆开" in item["stats"]["parse_note"]
    assert list((tmp_path / "reports").glob("*.pdf"))
    removed = client.delete(f"/api/eia/reports/{item['id']}")
    assert removed.status_code == 200
    scanned = client.post(
        "/api/eia/check",
        files=[("files", ("扫描.pdf", _pdf([[]]), "application/pdf"))],
        data={"as_of": "2026-10-21"},
    )
    refused = scanned.json()["items"][0]
    assert refused["ok"] is False
    assert "没有可复制的文字" in refused["error"]
