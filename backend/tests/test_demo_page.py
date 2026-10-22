from pathlib import Path


def test_public_demo_page_stays_local_and_fictional():
    page = Path(__file__).resolve().parents[2] / "docs" / "index.html"
    text = page.read_text(encoding="utf-8")
    assert "elaw://launch" in text
    assert "示意" in text
    assert "没反应" in text
    assert "暂仅支持 Windows" in text
    assert "始终允许" in text
    assert "已自带 Python" in text
    assert "LibreOffice" in text
    assert "长庚" not in text
    assert "127.0.0.1" not in text
    assert "fetch(" not in text
