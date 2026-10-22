from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_launcher_prefers_bundled_python():
    text = (ROOT / "启动（无需Docker）.bat").read_text(encoding="utf-8")
    bundled = text.find("python\\python.exe")
    venv = text.find("backend\\.venv\\Scripts\\python.exe")
    assert bundled != -1 and venv != -1
    assert bundled < venv


def test_package_script_pins_embeddable_python():
    script = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    runtime = (ROOT / "packaging" / "requirements-runtime.txt").read_text(encoding="utf-8")
    assert '$pyVersion = "3.11.9"' in script
    assert "embed-amd64.zip" in script
    assert "009D6BF7E3B2DDCA3D784FA09F90FE54336D5B60F0E0F305C37F400BF83CFD3B" in script
    assert "requirements-runtime.txt" in script
    assert "repealed_map.csv" in script
    assert '-Directory' in script
    assert "pytest" not in runtime
    assert "psycopg" not in runtime
    assert "pgvector" not in runtime
    assert "fastapi" in runtime
