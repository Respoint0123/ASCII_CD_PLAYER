#!/usr/bin/env python3
"""在伪终端里跑 TUI、抓屏、还原成纯文本 —— 用来检查真实渲染效果。

    python3 tools/pty_smoke.py --seconds 4 --cols 110 --rows 32
    python3 tools/pty_smoke.py --minimal
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import unicodedata

_ESC = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


def _cw(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def render_screen(raw: str) -> list[str]:
    """把「带光标定位的终端流」还原成字符网格。"""
    grid: dict[int, dict[int, str]] = {}
    row = col = 1
    i = 0
    while i < len(raw):
        m = _ESC.match(raw, i)
        if m:
            params, cmd = m.group(1), m.group(2)
            i = m.end()
            if cmd == "H":
                parts = params.split(";") if params else []
                row = int(parts[0]) if parts and parts[0] else 1
                col = int(parts[1]) if len(parts) > 1 and parts[1] else 1
            elif cmd == "J":
                grid.clear()
                row = col = 1
            elif cmd == "K":
                grid.pop(row, None)
            continue
        ch = raw[i]
        i += 1
        if ch == "\x1b":
            i += 1
            continue
        if ch == "\n":
            row += 1
            col = 1
            continue
        if ch == "\r":
            col = 1
            continue
        if ch == "\x07":
            continue
        w = _cw(ch)
        grid.setdefault(row, {})[col] = ch
        if w == 2:
            grid[row][col + 1] = ""
        col += w
    if not grid:
        return []
    height = max(grid)
    lines = []
    for r in range(1, height + 1):
        cells = grid.get(r, {})
        width = max(cells) if cells else 0
        buf = [" "] * (width + 1)
        for c, ch in cells.items():
            buf[c] = ch
        lines.append("".join(buf).rstrip())
    return lines


def spawn(cmd: list[str], cols: int, rows: int, seconds: float, key: bytes,
          exit_timeout: float = 15.0) -> tuple[str, int]:
    """在伪终端里跑一遍，返回 (原始输出, 退出码)。

    按下 key 之后继续读到子进程真正退出：`leave()` 写出的备用屏/光标恢复
    序列都在最后几百毫秒里，提前关 fd 会把它们丢掉。
    """
    pid, fd = pty.fork()
    if pid == 0:                                    # 子进程
        os.environ.setdefault("TERM", "xterm-256color")
        os.environ.setdefault("COLORTERM", "truecolor")
        os.execvp(cmd[0], cmd)
        os._exit(127)                               # pragma: no cover
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    out = b""
    t0 = time.time()
    while time.time() - t0 < seconds:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            out += data
    if key:
        try:
            os.write(fd, key)
        except OSError:
            pass
    status = None
    end = time.time() + exit_timeout
    while time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if data:
                out += data
            else:
                break
        done, st = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            status = st
            # 子进程退出后管道里可能还有最后一帧 + 恢复序列，读干净
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                out += data
            break
    if status is None:
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
    try:
        os.close(fd)
    except OSError:
        pass
    text = out.decode("utf-8", "replace")
    # 抓屏可能停在半个转义序列/半个 UTF-8 字符上，截掉尾巴再渲染
    cut = text.rfind("\x1b")
    if cut != -1 and not _ESC.match(text, cut):
        text = text[:cut]
    if text and ord(text[-1]) >= 0xD800:
        text = text[:-1]
    return text, os.waitstatus_to_exitcode(status)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--cols", type=int, default=110)
    ap.add_argument("--rows", type=int, default=32)
    ap.add_argument("--minimal", action="store_true")
    ap.add_argument("--theme", default="phosphor")
    ap.add_argument("--key", default="q")
    ap.add_argument("--app", default=None)
    ap.add_argument("--raw", action="store_true", help="打印原始转义流")
    ap.add_argument("--check", action="store_true",
                    help="CI 断言：预期画面、q 后退出码、终端恢复")
    args = ap.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = args.app or os.path.join(here, "asciicd.py")
    cmd = [sys.executable, app, "--fake", "--speed", "6", "--theme", args.theme]
    if args.minimal:
        cmd.append("--minimal")

    seconds = max(args.seconds, 6.0) if args.check else args.seconds
    raw, code = spawn(cmd, args.cols, args.rows, seconds, args.key.encode())
    if args.check:
        return check(raw, code)
    if args.raw:
        print(repr(raw[:4000]))
        return 0
    screen = render_screen(raw)
    tail = screen[-args.rows:] if len(screen) > args.rows else screen
    print("\n".join(tail))
    return 0


# --fake 启动 2.9 秒后进入仪表盘；--check 会等到 6 秒再按 q。
# 这三段文字覆盖框体标题、播放区和缓存区，任何一区没画出来都会失败。
EXPECTED_MARKERS = ("asciicd", "NOW PLAYING", "DISC CACHE")
_ENTER_ALT = "\x1b[?1049h"
_LEAVE_ALT = "\x1b[?1049l"
_SHOW_CURSOR = "\x1b[?25h"


def check(raw: str, code: int) -> int:
    """CI 用的最小验收：画面出来了、q 生效了、终端原样还回去了。"""
    errors: list[str] = []
    screen = render_screen(raw)
    joined = "\n".join(screen)
    for marker in EXPECTED_MARKERS:
        if marker not in joined:
            errors.append(f"画面里找不到 {marker!r}")
    if len(screen) < 8:
        errors.append(f"只抓到 {len(screen)} 行画面")
    if code != 0:
        errors.append(f"q 之后退出码是 {code}，应为 0")
    if "Traceback (most recent call last)" in raw:
        errors.append("输出里有 Python traceback")
    enter_at = raw.rfind(_ENTER_ALT)
    if enter_at == -1:
        errors.append("没有进入备用屏")
    else:
        if raw.rfind(_LEAVE_ALT) < enter_at:
            errors.append("退出时没有离开备用屏（终端没恢复）")
        if _SHOW_CURSOR not in raw[enter_at:]:
            errors.append("退出时没有恢复光标")
    for err in errors:
        print(f"✘ {err}", file=sys.stderr)
    if errors:
        return 1
    print(f"✔ PTY 冒烟通过：画面 {len(screen)} 行、退出码 {code}、终端已恢复")
    return 0


if __name__ == "__main__":
    sys.exit(main())
