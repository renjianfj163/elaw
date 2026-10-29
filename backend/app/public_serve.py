"""启动在线试用。只监听对外端口，密钥从 GEMINI_API_KEY 读取。"""

from __future__ import annotations

import os


def main() -> None:
    os.environ["ELAW_PUBLIC"] = "1"
    os.environ["ELAW_GUARD"] = "0"
    from app.llm.runtime_env import runtime_env

    if not runtime_env("GEMINI_API_KEY"):
        raise SystemExit("在线试用需要环境变量 GEMINI_API_KEY。密钥只留在服务器上，不会写进网页。")
    try:
        port = int(os.environ.get("ELAW_PORT", "8000"))
    except ValueError:
        port = 8000
    if port < 1 or port > 65535:
        port = 8000
    import uvicorn
    from pathlib import Path

    reload = os.environ.get("ELAW_RELOAD", "") == "1"
    root = Path(__file__).resolve().parents[2]
    print(f"在线试用已启动：http://127.0.0.1:{port}/")
    print("同一端口也对这台电脑所在网络开放。密钥留在服务器上，页面里看不到。")
    print("上传的资料会送去 Gemini。请不要用保密图纸试用。")
    if reload:
        print("这台电脑上的代码一保存，试用服务会自己重新读入。浏览器里再刷新一次即可。")
    options = {"host": "0.0.0.0", "port": port, "access_log": False}
    if reload:
        options["reload"] = True
        options["reload_dirs"] = [str(root / "backend" / "app"), str(root / "data")]
    uvicorn.run("app.main:app", **options)


if __name__ == "__main__":
    main()
