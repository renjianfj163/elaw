import json
import time

from app.local_launch import (
    IpcServer,
    invocation_ok,
    launch_script_text,
    protocol_command,
    signal_existing,
    workbench_url,
    write_instance,
)
from pathlib import Path


def test_protocol_invocation_rejects_extra_arguments(capsys):
    assert invocation_ok([])
    assert invocation_ok(["--install"])
    assert invocation_ok(["elaw://launch"])
    assert invocation_ok(["ELAW://launch/"])
    assert not invocation_ok(["elaw://launch", "calc.exe"])
    assert not invocation_ok(["elaw://launch&calc"])
    from app.local_launch import main

    assert main(["elaw://launch", "C:\\Windows\\System32\\calc.exe"]) == 2
    assert "calc" not in capsys.readouterr().out


def test_launch_script_and_protocol_command_take_no_url_parameter(tmp_path: Path):
    text = launch_script_text(tmp_path / "python.exe", tmp_path / "backend", tmp_path / "config")
    assert "%1" not in text
    assert "%*" not in text
    assert "ELAW_GUARD=1" in text
    bat = tmp_path / "launch.bat"
    bat.write_text(text, encoding="utf-8")
    assert "%1" not in protocol_command(bat)
    url = workbench_url(8000, "abcDEF_09-token")
    assert url == "http://127.0.0.1:8000/auth/launch?token=abcDEF_09-token"


def test_ipc_only_accepts_the_secret_and_launch(tmp_path: Path):
    calls = []
    server = IpcServer("s" * 16, lambda: calls.append("open"))
    server.start()
    try:
        write_instance(tmp_path, 8000, server.port, "s" * 16)
        assert signal_existing(tmp_path) is True
        payload = json.loads((tmp_path / "instance.json").read_text(encoding="utf-8"))
        payload["ipc_secret"] = "wrong-secret-value"
        (tmp_path / "instance.json").write_text(json.dumps(payload), encoding="utf-8")
        assert signal_existing(tmp_path) is False
        deadline = time.time() + 1
        while time.time() < deadline and not calls:
            time.sleep(0.02)
        assert calls == ["open"]
    finally:
        server.stop()


def test_ipc_ignores_http_noise():
    calls = []
    server = IpcServer("s" * 16, lambda: calls.append("open"))
    server.start()
    try:
        import socket

        with socket.create_connection(("127.0.0.1", server.port), timeout=0.5) as sock:
            sock.sendall(b"GET /elaw://launch?x=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            sock.settimeout(0.5)
            reply = sock.recv(16)
        assert reply.startswith(b"no")
        time.sleep(0.05)
        assert calls == []
    finally:
        server.stop()
