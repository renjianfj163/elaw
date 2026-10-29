"""读取本进程环境变量；没有时再读当前用户的环境变量。

密钥和代理地址都只放在环境里，不写入仓库。Windows 上用户变量存在注册表，
已经打开的进程看不到后来写入的值，所以这里直接读注册表。
"""

from __future__ import annotations

import os


def runtime_env(name: str) -> str:
    current = os.environ.get(name, "").strip()
    if current:
        return current
    found = read_user_env(name).strip()
    if found:
        os.environ[name] = found
    return found


def read_user_env(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(raw).strip()
