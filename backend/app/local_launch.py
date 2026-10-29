"""启动本机工作台。

协议入口不接收参数。已经在运行时只打开浏览器，不再起一个进程。
数据放在 %LOCALAPPDATA%\\elaw。服务只监听 127.0.0.1。
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

_ALLOWED_URLS = {"elaw://launch", "elaw://launch/"}
_FLAGS = {"--install", "--uninstall"}
_UNSAFE = set('%"&\n\r^|<>')


def invocation_ok(args: list[str]) -> bool:
    for raw in args:
        arg = raw.strip()
        if arg in _FLAGS or arg.lower() in _ALLOWED_URLS:
            continue
        return False
    return True


def state_dir() -> Path:
    raw = os.environ.get("ELAW_STATE_DIR")
    if raw:
        path = Path(raw)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "elaw"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_path(path: Path) -> str:
    text = str(path)
    if any(char in text for char in _UNSAFE):
        raise RuntimeError("安装路径包含无法安全写入启动脚本的字符。")
    return text


def launch_script_text(python_exe: Path, backend_dir: Path, config_dir: Path) -> str:
    python_text = _safe_path(python_exe)
    backend_text = _safe_path(backend_dir)
    config_text = _safe_path(config_dir)
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "PYTHONPATH={backend_text}"\r\n'
        f'set "ELAW_CONFIG_DIR={config_text}"\r\n'
        'set "ELAW_GUARD=1"\r\n'
        f'"{python_text}" -m app.local_launch\r\n'
    )


def protocol_command(bat: Path) -> str:
    command = f'"{_safe_path(bat)}"'
    if "%1" in command or "%*" in command:
        raise RuntimeError("协议命令不能携带参数。")
    return command


def workbench_url(port: int, token: str) -> str:
    if not token or any(char in token for char in '/\\" \n\r?#&'):
        raise RuntimeError("启动令牌无效。")
    return f"http://127.0.0.1:{int(port)}/auth/launch?token={token}"


def signal_existing(folder: Path) -> bool:
    path = folder / "instance.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ipc_port = int(payload["ipc_port"])
        secret = str(payload["ipc_secret"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
    if not secret.isascii() or "\n" in secret:
        return False
    try:
        with socket.create_connection(("127.0.0.1", ipc_port), timeout=0.4) as sock:
            sock.settimeout(0.4)
            sock.sendall(secret.encode("ascii") + b"\nlaunch\n")
            reply = b""
            while b"\n" not in reply and len(reply) < 16:
                chunk = sock.recv(16)
                if not chunk:
                    break
                reply += chunk
    except OSError:
        return False
    return reply.startswith(b"ok")


class IpcServer:
    """只接受固定口令 launch。连接里的其他内容不会进入命令行。"""

    def __init__(self, secret: str, on_launch) -> None:
        self._secret = secret.encode("ascii")
        self._on_launch = on_launch
        self._stop = threading.Event()
        self.port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(0.3)
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name="elaw-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if addr[0] != "127.0.0.1":
                conn.close()
                continue
            self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(0.5)
            data = b""
            while len(data) < 160 and data.count(b"\n") < 2:
                chunk = conn.recv(64)
                if not chunk:
                    break
                data += chunk
            lines = data.split(b"\n", 2)
            secret = lines[0] if lines else b""
            command = lines[1] if len(lines) > 1 else b""
            matched = (
                len(secret) == len(self._secret)
                and hmac.compare_digest(secret, self._secret)
                and command == b"launch"
            )
            if matched:
                self._on_launch()
                conn.sendall(b"ok\n")
            else:
                conn.sendall(b"no\n")
        except OSError:
            pass
        finally:
            conn.close()


def claim_primary(folder: Path) -> bool:
    if signal_existing(folder):
        return False
    lock = folder / "instance.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(lock, flags)
    except FileExistsError:
        time.sleep(0.3)
        if signal_existing(folder):
            return False
        try:
            lock.unlink()
        except OSError:
            return False
        try:
            fd = os.open(lock, flags)
        except FileExistsError:
            return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def release_primary(folder: Path) -> None:
    for name in ("instance.json", "instance.lock"):
        try:
            (folder / name).unlink()
        except OSError:
            pass


def pick_port(start: int | None = None) -> int:
    if start is None:
        try:
            start = int(os.environ.get("ELAW_PORT_START", "8000"))
        except ValueError:
            start = 8000
    for port in range(start, start + 11):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError("8000 起的端口都在用。")


def write_instance(folder: Path, http_port: int, ipc_port: int, secret: str) -> None:
    payload = {"http_port": http_port, "ipc_port": ipc_port, "ipc_secret": secret}
    target = folder / "instance.json"
    temporary = folder / "instance.json.tmp"
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(target)


def configure_process(port: int, folder: Path) -> None:
    from app.db import repo_root

    os.environ["ELAW_GUARD"] = "1"
    os.environ["ELAW_PORT"] = str(port)
    os.environ["DATA_DIR"] = str(folder)
    os.environ["DATABASE_URL"] = "sqlite:///" + (folder / "elaw.db").as_posix()
    os.environ.setdefault("ELAW_CONFIG_DIR", str(repo_root() / "config"))


def install_user_entries(folder: Path) -> None:
    from app.db import repo_root

    root = repo_root()
    bat = folder / "launch.bat"
    bat.write_text(
        launch_script_text(Path(sys.executable), root / "backend", root / "config"),
        encoding="utf-8",
    )
    if sys.platform != "win32":
        return
    import winreg

    command = protocol_command(bat)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\elaw") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:elaw")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\elaw\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    _create_shortcut(bat)


def _create_shortcut(bat: Path) -> None:
    env = os.environ.copy()
    env["ELAW_SHORTCUT_TARGET"] = str(bat)
    env["ELAW_SHORTCUT_NAME"] = "原理图审核.lnk"
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        "$desk = [Environment]::GetFolderPath('Desktop'); "
        "$link = $shell.CreateShortcut((Join-Path $desk $env:ELAW_SHORTCUT_NAME)); "
        "$link.TargetPath = $env:ELAW_SHORTCUT_TARGET; "
        "$link.WorkingDirectory = Split-Path $env:ELAW_SHORTCUT_TARGET; "
        "$link.Save()"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        print("桌面快捷方式没有创建。")


def uninstall() -> None:
    if sys.platform == "win32":
        import winreg

        for subkey in (
            r"Software\Classes\elaw\shell\open\command",
            r"Software\Classes\elaw\shell\open",
            r"Software\Classes\elaw\shell",
            r"Software\Classes\elaw",
        ):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
            except OSError:
                pass
        _remove_shortcut()
    release_primary(state_dir())


def _remove_shortcut() -> None:
    script = (
        "$desk = [Environment]::GetFolderPath('Desktop'); "
        "$path = Join-Path $desk $env:ELAW_SHORTCUT_NAME; "
        "if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }"
    )
    env = os.environ.copy()
    env["ELAW_SHORTCUT_NAME"] = "原理图审核.lnk"
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _open_when_ready(port: int, token: str) -> None:
    url = workbench_url(port, token)
    deadline = time.time() + 20
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    else:
        print("工作台没有在预期时间内启动。")
        return
    if os.environ.get("ELAW_NO_BROWSER") == "1":
        print(url)
        return
    webbrowser.open(url)


def serve(folder: Path, port: int) -> int:
    import secrets

    import uvicorn

    from app import local_guard
    from app.main import app

    local_guard.before_exit(lambda: release_primary(folder))
    token = local_guard.issue_launch_token()
    secret = secrets.token_urlsafe(32)

    def on_launch() -> None:
        _open_when_ready(port, local_guard.issue_launch_token())

    ipc = IpcServer(secret, on_launch)
    ipc.start()
    write_instance(folder, port, ipc.port, secret)
    print("本机工作台正在打开。报告留在这台电脑上。")
    if port != 8000:
        print(f"8000 端口正被占用，本机工作台改用 {port}。")
    threading.Thread(target=_open_when_ready, args=(port, token), name="elaw-open", daemon=True).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="info")
    finally:
        ipc.stop()
        release_primary(folder)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not invocation_ok(args):
        print("无法启动。")
        return 2
    if "--uninstall" in args:
        uninstall()
        print("已去掉本机启动入口。保存在本机的数据还在。")
        return 0
    folder = state_dir()
    if signal_existing(folder):
        print("工作台已经在运行，正在打开。")
        return 0
    if not claim_primary(folder):
        if signal_existing(folder):
            print("工作台已经在运行，正在打开。")
            return 0
        print("启动没有成功。")
        return 1
    try:
        install_user_entries(folder)
        port = pick_port()
        configure_process(port, folder)
        return serve(folder, port)
    except (OSError, RuntimeError) as exc:
        release_primary(folder)
        print(str(exc) or "启动没有成功。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
