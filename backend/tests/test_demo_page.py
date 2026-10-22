from pathlib import Path


def test_public_demo_page_stays_local_and_fictional():
    page = Path(__file__).resolve().parents[2] / "docs" / "index.html"
    text = page.read_text(encoding="utf-8")
    assert "hospital-eia-windows.zip" in text
    assert "下载安装包" in text
    assert "elaw://launch" not in text
    assert "示意" in text
    assert "暂仅支持 Windows" in text
    assert "已自带 Python 和 LibreOffice" in text
    assert "另装 LibreOffice" not in text
    assert "另存为 .docx" in text
    assert "示意综合医院" not in text
    assert "animation" not in text
    assert "长庚" not in text
    assert "127.0.0.1" not in text
    assert "fetch(" not in text
