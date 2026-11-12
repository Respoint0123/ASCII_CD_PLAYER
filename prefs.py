"""记住上次退出时选的配色（以及以后想存的界面选项）。

位置在 XDG 配置目录 `~/.config/asciicd/state.json`，不放缓存目录：
清缓存不该把用户的选择一起清掉。文件损坏就当作没有，绝不挡启动。
"""
from __future__ import annotations

import json
import os


def path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "asciicd", "state.json")


def load(p: str | None = None) -> dict:
    target = p or path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict, p: str | None = None) -> bool:
    """原子写（先写 .tmp 再 rename），失败只返回 False。"""
    target = p or path()
    tmp = target + ".tmp"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
        return True
    except OSError:
        return False
