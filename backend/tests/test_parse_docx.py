import io
import zipfile
from pathlib import Path

import pytest

from app.parse.convert import ConvertError, find_soffice
from app.parse.docx_walk import heading_number, parse_docx_bytes
from app.parse.cli import parse_report

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOC = REPO_ROOT / "data" / "samples" / "20241114-changgeng-radiotherapy.doc"


def _docx(document_xml: str, rels_xml: str | None = None, media: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("word/document.xml", document_xml)
        if rels_xml is not None:
            package.writestr("word/_rels/document.xml.rels", rels_xml)
        for name, payload in (media or {}).items():
            package.writestr(name, payload)
    return buffer.getvalue()


def _nested_fixture() -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A}">
  <w:body>
    <w:p><w:r><w:t>封面说明</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>10.1.2 分区管理</w:t></w:r></w:p>
          <w:p><w:r><w:t>表10-1 屏蔽设计</w:t></w:r></w:p>
          <w:tbl>
            <w:tr>
              <w:tc>
                <w:p><w:r><w:t>初级屏蔽混凝土</w:t></w:r></w:p>
              </w:tc>
              <w:tc>
                <w:p>
                  <w:r>
                    <w:drawing>
                      <a:blip r:embed="rIdImg"/>
                    </w:drawing>
                  </w:r>
                  <w:r><w:t>见公式图</w:t></w:r>
                </w:p>
              </w:tc>
            </w:tr>
          </w:tbl>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>2.5</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>表7-2 剂量限值</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdImg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.wmf"/>
</Relationships>
"""
    return _docx(document, rels, {"word/media/image1.wmf": b"wmf"})


def test_heading_number_ignores_plain_decimals():
    assert heading_number("10.1.2 分区管理") == "10.1.2"
    assert heading_number("7.3.1") == "7.3.1"
    assert heading_number("2.5") is None
    assert heading_number("1.03E-3 mSv") is None
    assert heading_number("\t\t74.0天") is None
    assert heading_number("2.5倍") is None


def test_walk_reads_nested_table_and_marks_image_not_parsed():
    result = parse_docx_bytes(_nested_fixture(), source_name="fixture.docx")
    assert result.stats.table_count == 3
    assert result.stats.top_level_table_count == 2
    assert result.stats.nested_table_count == 1
    assert result.stats.media_file_count == 1

    texts = [block.text for block in result.blocks]
    assert "初级屏蔽混凝土" in texts
    assert "表7-2 剂量限值" in texts
    assert "表10-1 屏蔽设计" in texts

    nested = next(block for block in result.blocks if block.text == "初级屏蔽混凝土")
    assert nested.type == "cell"
    assert nested.section_path == "10.1.2"
    assert [(step.table, step.row, step.col) for step in nested.table_path] == [(1, 1, 1), (1, 1, 1)]

    image = next(block for block in result.blocks if block.type == "image")
    assert image.not_parsed is True
    assert image.text == ""
    assert image.has_image is True
    assert image.media_file == "word/media/image1.wmf"
    assert image.char_start == image.char_end

    formula_note = next(block for block in result.blocks if block.text == "见公式图")
    assert formula_note.has_image is True
    assert formula_note.not_parsed is False

    for block in result.blocks:
        assert result.full_text[block.char_start:block.char_end] == block.text
    joined = "\n".join(block.text for block in result.blocks if block.text)
    assert result.full_text == joined

    decimal = next(block for block in result.blocks if block.text == "2.5")
    assert decimal.section_path == "10.1.2"


def test_shallow_cell_text_misses_nested_table():
    """只走顶层表、只读单元格直接段落时，嵌套表里的字不会出现。"""
    from xml.etree import ElementTree

    root = ElementTree.fromstring(zipfile.ZipFile(io.BytesIO(_nested_fixture())).read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = root.find(ns + "body")
    shallow: list[str] = []
    for table in list(body):
        if table.tag != ns + "tbl":
            continue
        for row in list(table):
            if row.tag != ns + "tr":
                continue
            for cell in list(row):
                if cell.tag != ns + "tc":
                    continue
                for child in list(cell):
                    if child.tag != ns + "p":
                        continue
                    shallow.append("".join((node.text or "") for node in child.iter(ns + "t")))
    assert "初级屏蔽混凝土" not in shallow

    parsed = parse_docx_bytes(_nested_fixture(), source_name="fixture.docx")
    assert "初级屏蔽混凝土" in parsed.full_text


def test_bundled_soffice_comes_before_a_system_install(monkeypatch, tmp_path):
    program = tmp_path / "libreoffice" / "program"
    program.mkdir(parents=True)
    bundled = program / "soffice.com"
    bundled.write_bytes(b"bundle")
    monkeypatch.delenv("SOFFICE_PATH", raising=False)
    monkeypatch.setattr("app.parse.convert.repo_root", lambda: tmp_path)
    assert find_soffice() == bundled


def test_soffice_path_overrides_bundle(monkeypatch, tmp_path):
    program = tmp_path / "libreoffice" / "program"
    program.mkdir(parents=True)
    (program / "soffice.com").write_bytes(b"bundle")
    override = tmp_path / "custom.com"
    override.write_bytes(b"custom")
    monkeypatch.setenv("SOFFICE_PATH", str(override))
    monkeypatch.setattr("app.parse.convert.repo_root", lambda: tmp_path)
    assert find_soffice() == override


def test_sample_report_tables_and_images():
    if not SAMPLE_DOC.is_file():
        pytest.skip(f"样例不在 {SAMPLE_DOC}")
    try:
        find_soffice()
    except ConvertError as exc:
        pytest.skip(str(exc))

    result = parse_report(SAMPLE_DOC)
    assert result.stats.table_count == 50
    assert result.stats.top_level_table_count == 16
    assert result.stats.nested_table_count == 34

    texts = [block.text for block in result.blocks if block.text]
    assert any("表7-2" in text for text in texts)
    assert any("表10-1" in text for text in texts)
    dose_rows = [
        block
        for block in result.blocks
        if "连续五年平均有效剂量" in block.text and len(block.table_path) >= 2
    ]
    assert dose_rows
    assert dose_rows[0].section_path == "7.3.1"
    shield_rows = [
        block
        for block in result.blocks
        if "屏蔽材料及厚度" in block.text and len(block.table_path) >= 2
    ]
    assert shield_rows
    assert any(block.section_path == "10.1.2" and "分区" in block.text for block in result.blocks)
    assert not any(block.section_path == "74.0" for block in result.blocks)

    images = [block for block in result.blocks if block.type == "image"]
    assert images
    assert all(block.not_parsed and block.text == "" and block.has_image and block.media_file for block in images)
    assert result.stats.image_block_count == len(images)
    suffixes = {(block.media_file or "").rsplit(".", 1)[-1].lower() for block in images}
    assert suffixes <= {"png", "emf", "jpeg", "jpg", "wmf"}

    for block in result.blocks:
        assert result.full_text[block.char_start:block.char_end] == block.text
