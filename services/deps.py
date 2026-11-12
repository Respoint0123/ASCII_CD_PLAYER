"""运行前依赖自检：缺什么、怎么装，一次说清楚。

分发包只带 asciicd 自己的代码和字体，**不捆绑** NumPy、cdparanoia、pw-cat。
`asciicd --check-deps` 逐条列出必需/可选项；启动时缺 NumPy 也会给友好提示。
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    optional: bool = False
    warn: bool = False          # 不拦启动，但会影响播放：显示 "!" 提醒

    def line(self) -> str:
        if self.warn:
            mark, suffix = "!", ""
        elif self.ok:
            mark, suffix = "✓", ""
        elif self.optional:
            mark, suffix = "!", "（可选）"
        else:
            mark, suffix = "✗", ""
        tail = f"  {self.detail}" if self.detail else ""
        return f"  {mark} {self.name}{suffix}{tail}"


def _module_available(name: str) -> bool:
    """不真 import 的模块可用性检查（--check-deps 要在缺 NumPy 时也能跑）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def pipewire_socket() -> str | None:
    """用户 PipeWire 会话的 socket；服务没起来时它是缺的。

    pw-cat 在 PATH 里、但 socket 不在，是「装了包没起服务」的典型情况 ——
    报错要和「根本没装 pw-cat」区分开。
    """
    runtime = (os.environ.get("PIPEWIRE_RUNTIME_DIR")
               or os.environ.get("XDG_RUNTIME_DIR"))
    if not runtime:
        return None
    path = os.path.join(runtime, "pipewire-0")
    return path if os.path.exists(path) else None


def check_dependencies(fake: bool = False) -> list[Check]:
    """返回所有依赖项。fake=True 时 cdparanoia 变可选（播放仍要 pw-cat）。"""
    checks: list[Check] = [Check(f"python {platform.python_version()}", True)]

    numpy_ok = _module_available("numpy")
    checks.append(Check(
        "numpy", numpy_ok,
        "" if numpy_ok else "缺：pacman -S python-numpy（或 pip install numpy）",
    ))

    cdparanoia = shutil.which("cdparanoia")
    checks.append(Check(
        "cdparanoia", bool(cdparanoia),
        cdparanoia or ("可选：--fake 模式不需要" if fake
                       else "缺：pacman -S cdparanoia（抓轨 + TOC）"),
        optional=fake,
    ))

    pw_cat = shutil.which("pw-cat")
    checks.append(Check(
        "pw-cat（PipeWire）", bool(pw_cat),
        pw_cat or "缺：pacman -S pipewire（播放必需；--fake 也要）",
    ))

    if pw_cat:
        session = pipewire_socket()
        checks.append(Check(
            "PipeWire 会话", bool(session),
            session or "未见 socket：systemctl --user start pipewire（播放必需）",
            optional=True, warn=not session,
        ))

    kitty = shutil.which("kitty")
    checks.append(Check(
        "kitty", bool(kitty),
        kitty or "可选：独立窗口 + 点阵字体；ASCIICD_IN_PLACE=1 可跳过",
        optional=True,
    ))
    return checks


def required_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if not c.optional)


def format_report(checks: list[Check], fake: bool = False) -> str:
    lines = ["asciicd 依赖自检" + ("（--fake 模式）" if fake else "")]
    lines += [c.line() for c in checks]
    lines.append("")
    if required_ok(checks):
        lines.append("必需依赖齐全，可以运行。")
        if any(c.warn for c in checks):
            lines.append("上面标 ! 的项不拦启动，但会让播放没声音。")
    else:
        lines.append("缺少必需依赖；按上面的提示装好再运行（--fake 只跳过 cdparanoia）。")
    return "\n".join(lines)


def run_check(fake: bool = False) -> int:
    """CLI 入口：打印清单，缺必需依赖返回 1。"""
    checks = check_dependencies(fake=fake)
    print(format_report(checks, fake=fake))
    return 0 if required_ok(checks) else 1
