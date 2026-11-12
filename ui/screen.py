"""终端底层：备用屏、cbreak 输入、按行差分刷新。"""
from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty

from ui.glyphs import fit

_SEQ = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1b[H": "home", b"\x1b[F": "end",
    b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn",
    b"\x1bOP": "f1", b"\x1b[Z": "backtab",
}
_ESC_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z~]|\x1bO[A-Za-z]")


class Screen:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self._is_tty = bool(getattr(self.out, "isatty", lambda: False)())
        self._saved = None
        self.prev_rows: list[str] | None = None
        self.w = 0
        self.h = 0
        self.active = False

    # ── 尺寸 ──────────────────────────────────────────────────────────────
    def size(self) -> tuple[int, int]:
        try:
            ts = os.get_terminal_size(self.out.fileno())
            return max(20, ts.columns), max(6, ts.lines)
        except (OSError, ValueError):
            return 100, 30

    # ── 进入/退出 ─────────────────────────────────────────────────────────
    def enter(self) -> None:
        try:
            if sys.stdin.isatty():
                self._saved = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
        except (termios.error, OSError):
            self._saved = None
        self.out.write("\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H")
        self.out.flush()
        self.active = True
        self.prev_rows = None

    def leave(self) -> None:
        if self.active:
            self.out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            self.out.flush()
            self.active = False
        if self._saved is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            except (termios.error, OSError):
                pass
            self._saved = None

    # ── 绘制 ──────────────────────────────────────────────────────────────
    def draw(self, rows: list[str]) -> None:
        w, h = self.size()
        chunks: list[str] = []
        if (w, h) != (self.w, self.h):
            self.w, self.h = w, h
            self.prev_rows = None
            chunks.append("\x1b[2J")

        norm = [fit(r, w) for r in rows[:h]]
        while len(norm) < h:
            norm.append(" " * w)

        if self.prev_rows is None or len(self.prev_rows) != h:
            for i, row in enumerate(norm):
                chunks.append(f"\x1b[{i + 1};1H{row}\x1b[0m")
        else:
            for i, (old, new) in enumerate(zip(self.prev_rows, norm)):
                if old != new:
                    chunks.append(f"\x1b[{i + 1};1H{new}\x1b[0m")
        if chunks:
            self.out.write("".join(chunks))
            self.out.flush()
        self.prev_rows = norm

    def clear(self) -> None:
        self.out.write("\x1b[2J\x1b[H")
        self.out.flush()
        self.prev_rows = None


class KeyReader:
    """非阻塞读键，把 ESC 序列翻成可读名字。"""

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.buf = b""

    def read(self) -> list[str]:
        if self.fd is None:
            return []
        try:
            ready, _, _ = select.select([self.fd], [], [], 0)
        except (OSError, ValueError):
            return []
        if not ready:
            return []
        try:
            data = os.read(self.fd, 256)
        except OSError:
            return []
        if not data:                      # stdin 关了：当作退出，别死循环
            return ["quit"]
        self.buf += data
        return self._parse()

    def _parse(self) -> list[str]:
        keys: list[str] = []
        while self.buf:
            if self.buf[0] == 0x1B:
                m = _ESC_RE.match(self.buf)
                if m:
                    seq = m.group(0)
                    self.buf = self.buf[len(seq):]
                    keys.append(_SEQ.get(seq, "unknown"))
                else:
                    self.buf = self.buf[1:]
                    keys.append("esc")
                continue
            b = self.buf[0]
            self.buf = self.buf[1:]
            if b in (0x0D, 0x0A):
                keys.append("enter")
            elif b == 0x20:
                keys.append("space")
            elif b == 0x09:
                keys.append("tab")
            elif b == 0x7F:
                keys.append("backspace")
            elif 32 <= b < 127:
                keys.append(chr(b))
            else:
                keys.append("other")
        return keys

