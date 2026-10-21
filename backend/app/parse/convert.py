"""把 .doc 交给 LibreOffice headless，转成 .docx。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

class ConvertError(Exception):
    pass


def find_soffice() -> Path:
    env_path = os.environ.get("SOFFICE_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate
        raise ConvertError(f"SOFFICE_PATH 指向的文件不存在：{candidate}")

    which = shutil.which("soffice")
    if which:
        return Path(which)

    names = ("soffice.com", "soffice.exe", "soffice")
    roots = (
        Path(r"C:\Program Files\LibreOffice\program"),
        Path(r"C:\Program Files (x86)\LibreOffice\program"),
    )
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate
    raise ConvertError("没有找到 LibreOffice。请安装后重试，或设置 SOFFICE_PATH。")


def _soffice_env(soffice: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONNOUSERSITE", "VIRTUAL_ENV"):
        env.pop(key, None)
    program = str(soffice.parent)
    env["PATH"] = program + os.pathsep + env.get("PATH", "")
    return env


def convert_doc_to_docx(source: Path, out_dir: Path, timeout_s: int = 300) -> Path:
    source = source.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix == ".docx":
        return source
    if suffix != ".doc":
        raise ConvertError("只接受 .doc 或 .docx")

    soffice = find_soffice()
    expected = out_dir / f"{source.stem}.docx"
    last_detail = ""
    for attempt in (1, 2):
        if expected.exists():
            expected.unlink()
        profile = out_dir / f"lo-profile-{attempt}"
        profile.mkdir(parents=True, exist_ok=True)
        command = [
            str(soffice),
            "--headless",
            "--nologo",
            "--nofirststartwizard",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "docx:Office Open XML Text",
            "--outdir",
            str(out_dir),
            str(source),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout_s,
                check=False,
                cwd=soffice.parent,
                env=_soffice_env(soffice),
            )
        except subprocess.TimeoutExpired as exc:
            raise ConvertError(f"LibreOffice 转换超过 {timeout_s} 秒") from exc
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
        if expected.is_file() and expected.stat().st_size > 0 and completed.returncode == 0:
            return expected
        last_detail = detail or f"exit {completed.returncode}"
    raise ConvertError(f"LibreOffice 没有生成 docx。{last_detail}".strip())
