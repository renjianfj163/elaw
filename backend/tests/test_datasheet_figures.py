import io
import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.llm.provider import MemoryCache
from app.main import app
from app.schematic.datasheet_figures import (
    FigureLine,
    FigureReadError,
    figure_reader_or_none,
    page_reason,
    read_datasheet_figures,
    select_figure_pages,
    transcribe_figures,
)
from app.schematic.crop import crop_datasheet
from app.schematic.knowledge.model import DesignConstraint, DesignParameter
from app.schematic.knowledge.store import KnowledgeStore

client = TestClient(app)


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


def _text_with_images(text: str, count: int) -> bytes:
    reader = PdfReader(io.BytesIO(_pdf(text)))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    page = writer.pages[0]
    images = DictionaryObject()
    for index in range(count):
        stream = DecodedStreamObject()
        stream.set_data(b"\x00")
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        images[NameObject(f"/Im{index}")] = writer._add_object(stream)
    resources = page["/Resources"].get_object()
    resources[NameObject("/XObject")] = images
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _image_page(count: int) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    resources = DictionaryObject()
    images = DictionaryObject()
    for index in range(count):
        stream = DecodedStreamObject()
        stream.set_data(b"\x00")
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        images[NameObject(f"/Im{index}")] = writer._add_object(stream)
    resources[NameObject("/XObject")] = images
    page[NameObject("/Resources")] = resources
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_page_reason_skips_blank_and_keeps_thin_or_busy_pages():
    assert page_reason("", 0) == ""
    assert page_reason("PIN1", 0) == "little_text"
    assert page_reason("A" * 200, 1) == ""
    assert page_reason("A" * 200, 3) == "many_images"


def test_select_figure_pages_uses_text_and_embedded_images():
    blank = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(blank)
    assert select_figure_pages(blank.getvalue()) == []
    assert [item.reason for item in select_figure_pages(_pdf("Vout max 3.6V"))] == ["little_text"]
    assert select_figure_pages(_pdf("A" * 200)) == []
    selected = select_figure_pages(_image_page(3))
    assert [item.reason for item in selected] == ["little_text"]
    assert selected[0].images == 3
    busy = select_figure_pages(_text_with_images("A" * 200, 3))
    assert [item.reason for item in busy] == ["many_images"]
    assert busy[0].images == 3


def test_figure_text_stays_out_of_the_quotable_layer():
    def read(pdf, pages):
        assert pdf.startswith(b"%PDF")
        return [FigureLine(page=1, text="图上写着 EN 接高电平")]

    found = read_datasheet_figures(_pdf("Vout max 3.6V"), reader=read)
    assert found.status == "read"
    assert found.pages[0].text == "图上写着 EN 接高电平"
    assert "已写入摘录" in found.note


def test_figure_text_already_in_the_layer_is_not_stored():
    def read(pdf, pages):
        return [FigureLine(page=pages[0], text="Vout max 3.6V")]

    found = read_datasheet_figures(_pdf("Vout max 3.6V"), reader=read)
    assert found.pages == []
    assert found.status == "read"


def test_subset_index_maps_back_to_the_original_page():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    page = writer.add_blank_page(width=200, height=200)
    resources = DictionaryObject()
    images = DictionaryObject()
    for index in range(3):
        stream = DecodedStreamObject()
        stream.set_data(b"\xff")
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(1),
                NameObject("/Height"): NumberObject(1),
                NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                NameObject("/BitsPerComponent"): NumberObject(8),
            }
        )
        images[NameObject(f"/Im{index}")] = writer._add_object(stream)
    resources[NameObject("/XObject")] = images
    page[NameObject("/Resources")] = resources
    buffer = io.BytesIO()
    writer.write(buffer)

    def read(pdf, pages):
        return [FigureLine(page=1, text="仅在第二页的图里")]

    found = read_datasheet_figures(buffer.getvalue(), reader=read)
    assert found.pages[0].page == 2
    assert found.pages[0].text == "仅在第二页的图里"


def test_transcribe_retries_then_reads_the_cache():
    calls = {"n": 0}

    def complete(pdf, pages):
        calls["n"] += 1
        if calls["n"] == 1:
            return "not-json"
        return json.dumps({"pages": [{"page": pages[0], "text": "EN"}]})

    cache = MemoryCache()
    lines = transcribe_figures(b"%PDF", [4], complete=complete, model="gemini-test", cache=cache, retries=2)
    assert lines[0].text == "EN"
    assert calls["n"] == 2
    again = transcribe_figures(b"%PDF", [4], complete=complete, model="gemini-test", cache=cache, retries=2)
    assert again[0].text == "EN"
    assert calls["n"] == 2


def test_transcribe_stops_after_the_retry_budget():
    calls = {"n": 0}

    def complete(pdf, pages):
        calls["n"] += 1
        raise ValueError("bad")

    with pytest.raises(FigureReadError):
        transcribe_figures(b"%PDF", [1], complete=complete, model="gemini-test", cache=MemoryCache(), retries=2)
    assert calls["n"] == 3


def test_reader_stays_off_until_outbound_gemini_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "app.llm.provider.load_llm_config",
        lambda: {"outbound_allowed": False, "provider": "gemini"},
    )
    reader, reason = figure_reader_or_none()
    assert reader is None
    assert "外发" in reason
    monkeypatch.setattr(
        "app.llm.provider.load_llm_config",
        lambda: {"outbound_allowed": True, "provider": "deepseek"},
    )
    reader, reason = figure_reader_or_none()
    assert reader is None
    assert "Gemini" in reason


def test_figure_heading_is_kept_when_the_datasheet_is_cropped():
    text = "8.1 封装\n外形尺寸不进入审查。\n\n引脚说明与典型应用（图中文字）\n图上写着 EN 接高电平"
    cropped = crop_datasheet(text, ["EN"])
    assert "图上写着 EN 接高电平" in cropped
    assert "外形尺寸" not in cropped


def test_extract_puts_figure_text_into_the_excerpt(monkeypatch):
    def fake():
        def read(pdf, pages):
            return [FigureLine(page=pages[0], text="图上写着 EN 接高电平")]

        return read, ""

    monkeypatch.setattr("app.schematic.datasheet_figures.figure_reader_or_none", fake)
    response = client.post(
        "/api/schematic/extract",
        data={"role": "datasheet", "read_figures": "1"},
        files={"file": ("lm1117.pdf", _pdf("Vout max 3.6V"), "application/pdf")},
    )
    assert response.status_code == 200
    body = response.json()
    assert "3.6V" in body["text"]
    assert body["figure_job"]
    import time

    finished = None
    for _ in range(40):
        finished = client.get(f"/api/schematic/extract/{body['figure_job']}")
        if finished.json()["status"] != "running":
            break
        time.sleep(0.05)
    assert finished is not None
    done = finished.json()
    assert done["status"] == "done"
    assert "图上写着 EN 接高电平" in done["text_block"]
    assert "引脚说明与典型应用（图中文字）" in done["text_block"]
    assert done["figures"][0]["text"] == "图上写着 EN 接高电平"
    assert done["figure_status"] == "read"


def test_extract_without_the_flag_does_not_attach_figures():
    response = client.post(
        "/api/schematic/extract",
        data={"role": "datasheet"},
        files={"file": ("lm1117.pdf", _pdf("Vout max 3.6V"), "application/pdf")},
    )
    assert response.status_code == 200
    body = response.json()
    assert "figures" not in body
    assert "3.6V" in body["text"]


def test_ocr_excerpt_can_be_verified_once_it_is_in_the_source(tmp_path):
    store = KnowledgeStore(tmp_path)
    document = store.import_pdf(
        title="带图手册（合成）",
        payload=_pdf("Vout max 3.6V"),
        kind="datasheet",
        ocr_text="引脚说明与典型应用（图中文字）\n第 1 页（图中文字，几乎没有可复制文字）\n图上写着 EN 接高电平",
        ocr_note="识图读到 1 页图中文字，已写入摘录，审查可以引用。",
        ocr_pages=[1],
    )
    assert "图上写着 EN 接高电平" in store._texts[document.document_id]
    item = DesignConstraint(
        constraint_id="",
        category="electrical",
        title="EN",
        constraint_type="reference",
        source_quote="图上写着 EN 接高电平",
        extraction_method="ocr",
        source_page=1,
        parameters=[DesignParameter(name="pin_name", value_text="EN")],
    )
    stored = store._stamp(item, document)
    store._insert(stored)
    assert store.verify(stored.constraint_id).verification_status == "verified"


def test_knowledge_import_keeps_ocr_beside_the_text_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def fake():
        def read(pdf, pages):
            return [FigureLine(page=pages[0], text="图上写着 EN 接高电平")]

        return read, ""

    monkeypatch.setattr("app.schematic.datasheet_figures.figure_reader_or_none", fake)
    response = client.post(
        "/api/schematic/knowledge/import",
        data={"title": "短手册（合成）", "kind": "datasheet"},
        files={"file": ("short.pdf", _pdf("Vout max 3.6V"), "application/pdf")},
    )
    assert response.status_code == 200
    document = response.json()["document"]
    assert "图上写着" in document["ocr_text"]
    assert "已写入摘录" in document["ocr_note"]
    store = KnowledgeStore(tmp_path / "design_knowledge")
    assert "图上写着 EN 接高电平" in store._texts[document["document_id"]]


def test_workbench_asks_for_figure_reading_without_mixing_it_into_the_excerpt():
    from pathlib import Path

    html = Path(__file__).parents[1].joinpath("app", "static", "workbench.html").read_text(encoding="utf-8")
    assert 'id="datasheetFigures"' in html
    assert 'id="datasheetSpinner"' in html
    assert "正在识图，已等待" in html
    assert 'body.set("read_figures", "1")' in html
    assert "图中文字已写入摘录" in html
