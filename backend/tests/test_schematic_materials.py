import io
import zipfile

from fastapi.testclient import TestClient

from app.main import app

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
client = TestClient(app)


def _docx(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W}"><w:body>'
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 36 100 Td ({text}) Tj ET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def test_product_docx_extracts_text():
    response = client.post(
        "/api/schematic/extract",
        data={"role": "product"},
        files={"file": ("接口要求.docx", _docx("3.2 接口要求：需要一路 I2C 接口。"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "product"
    assert body["filename"] == "接口要求.docx"
    assert "需要一路 I2C 接口" in body["text"]
    assert body["too_long"] is False
    assert body["chars"] == len(body["text"])


def test_datasheet_pdf_extracts_text():
    response = client.post(
        "/api/schematic/extract",
        data={"role": "datasheet"},
        files={"file": ("lm1117.pdf", _pdf("Vout max 3.6V"), "application/pdf")},
    )
    assert response.status_code == 200
    body = response.json()
    assert "3.6V" in body["text"]
    assert body["role"] == "datasheet"


def test_product_rejects_pdf_and_datasheet_rejects_docx():
    pdf = client.post(
        "/api/schematic/extract",
        data={"role": "product"},
        files={"file": ("spec.pdf", _pdf("I2C"), "application/pdf")},
    )
    assert pdf.status_code == 400
    assert "Word" in pdf.json()["detail"]
    docx = client.post(
        "/api/schematic/extract",
        data={"role": "datasheet"},
        files={"file": ("sheet.docx", _docx("3.6V"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert docx.status_code == 400
    assert "PDF" in docx.json()["detail"]


def test_blank_docx_is_refused():
    response = client.post(
        "/api/schematic/extract",
        data={"role": "product"},
        files={"file": ("empty.docx", _docx("   "), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 400
    assert "没有抽出正文" in response.json()["detail"]


def test_doc_product_uses_converted_docx(monkeypatch):
    def fake_convert(source, out_dir, timeout_s=300):
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / "spec.docx"
        target.write_bytes(_docx("3.1 电源：输入 5V。"))
        return target

    monkeypatch.setattr("app.schematic.materials.convert_doc_to_docx", fake_convert)
    response = client.post(
        "/api/schematic/extract",
        data={"role": "product"},
        files={"file": ("spec.doc", b"not a real doc", "application/msword")},
    )
    assert response.status_code == 200
    assert "输入 5V" in response.json()["text"]


def test_overlong_extract_is_flagged_without_truncation():
    from app.schematic.review import MAX_BASIS_CHARS

    text = "甲" * (MAX_BASIS_CHARS + 1)
    response = client.post(
        "/api/schematic/extract",
        data={"role": "product"},
        files={"file": ("long.docx", _docx(text), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["too_long"] is True
    assert body["chars"] == MAX_BASIS_CHARS + 1
    assert body["text"] == text
