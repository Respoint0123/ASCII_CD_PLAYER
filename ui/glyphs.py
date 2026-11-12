"""字符层：宽度计算、ANSI 颜色、频谱条、Braille 示波器、电平表。"""
from __future__ import annotations

import re
import unicodedata

import numpy as np

BLOCKS = "▁▂▃▄▅▆▇█"
SHADES = " ░▒▓█"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
UNDER = "\x1b[4m"        # 下划线开（不用颜色，只靠下标强调）
UNDER_OFF = "\x1b[24m"   # 下划线关


def cw(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def text_width(s: str) -> int:
    return sum(cw(c) for c in strip_ansi(s))


def _is_control(ch: str) -> bool:
    """除 ESC（ANSI 序列要用）外的 C0/C1 控制字符。"""
    if ch == "\x1b":
        return False
    code = ord(ch)
    return code < 32 or 0x7f <= code < 0xa0


def fit(s: str, width: int, align: str = "left") -> str:
    """按显示宽度裁剪/补齐，ANSI 转义不影响宽度。

    换行/制表符等控制字符一律按一个空格算：调用方按「一行一个控件」布局，
    让 \n 原样进缓冲区会把整块画面顶掉。
    """
    if width <= 0:
        return ""
    if any(_is_control(ch) for ch in s):
        s = "".join(" " if _is_control(ch) else ch for ch in s)
    out: list[str] = []
    vis = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\x1b":
            j = s.find("m", i)
            if j == -1:
                break
            out.append(s[i: j + 1])
            i = j + 1
            continue
        w = cw(ch)
        if vis + w > width:
            break
        out.append(ch)
        vis += w
        i += 1
    pad = width - vis
    if pad <= 0:
        return "".join(out)
    return (" " * pad + "".join(out)) if align == "right" else ("".join(out) + " " * pad)


def fg(color: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{color[0]};{color[1]};{color[2]}m"


def bg(color: tuple[int, int, int]) -> str:
    return f"\x1b[48;2;{color[0]};{color[1]};{color[2]}m"


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def time_fmt(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    total_min, sec = divmod(seconds, 60)
    hour, minute = divmod(total_min, 60)
    if hour:
        return f"{hour}:{minute:02d}:{sec:02d}"
    return f"{minute:02d}:{sec:02d}"


def resample(values: np.ndarray, width: int) -> np.ndarray:
    """把 N 个 bin 重采样成 width 个（等宽分段取最大值，视觉上不丢峰值）。

    用 reshape+max 向量化：宽 100 列 × 每帧几次调用，Python 循环版本会吃掉 ~1.5ms/帧。
    """
    values = np.asarray(values, dtype=np.float32)
    if width <= 0:
        return np.zeros(0, np.float32)
    n = len(values)
    if n == 0:
        return np.zeros(width, np.float32)
    if n == width:
        return values.copy()
    if n < width:                                   # 上采样：线性插值
        idx = np.linspace(0, n - 1, width)
        return np.interp(idx, np.arange(n), values).astype(np.float32)
    groups = -(-n // width)                         # ceil
    pad = width * groups - n
    if pad:
        values = np.concatenate([values, np.full(pad, values[-1], dtype=np.float32)])
    return values.reshape(width, groups).max(axis=1)


def spectrum_line(values: np.ndarray, width: int, theme: dict, peaks: np.ndarray | None = None) -> str:
    """一根根字符柱，按频率渐变上色；有 peaks 时在上方叠峰值标记。"""
    vals = resample(values, width)
    pks = resample(peaks, width) if peaks is not None else None
    out: list[str] = []
    for i in range(width):
        t = i / max(1, width - 1)
        color = lerp(theme["spec_lo"], theme["spec_hi"], t)
        v = float(vals[i]) if i < len(vals) else 0.0
        idx = min(7, max(0, int(v * 7.99)))
        ch = BLOCKS[idx]
        if pks is not None and pks[i] > v + 0.04:
            pch = "▔"
        else:
            pch = None
        if pch:
            out.append(bg(theme["bg_bar"]) + fg(color) + pch)
        else:
            out.append(fg(color) + ch)
    return "".join(out) + RESET


def waterfall_line(row: np.ndarray, width: int, theme: dict) -> str:
    vals = resample(row, width)
    out: list[str] = []
    for i in range(width):
        t = i / max(1, width - 1)
        v = float(vals[i])
        shade = SHADES[min(4, max(0, int(v * 4.99)))]
        color = lerp(theme["bg_bar"], lerp(theme["spec_lo"], theme["spec_hi"], t), 0.35 + 0.65 * v)
        out.append(fg(color) + shade)
    return "".join(out) + RESET


def braille_scope(samples: np.ndarray, cells: int, rows: int = 4,
                  auto_gain: bool = True) -> list[str]:
    """把波形画进 Braille 点阵：每字符 2×4 点。

    返回 rows 个字符串，每个长度 cells。空点用空格（不用 U+2800，避免字体把它画成方块）。
    """
    cells = max(1, cells)
    dot_rows = rows * 4
    dot_cols = cells * 2
    empty = [" " * cells for _ in range(rows)]
    samples = np.asarray(samples, dtype=np.float32).ravel()
    if samples.size < dot_cols:
        samples = np.pad(samples, (0, dot_cols - samples.size))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-5:
        return empty
    scale = 0.95 / peak if auto_gain else 1.0
    s = np.clip(samples * scale, -1.0, 1.0)

    edges = np.linspace(0, s.size, dot_cols + 1).astype(int)
    lo = np.empty(dot_cols, np.float32)
    hi = np.empty(dot_cols, np.float32)
    for i in range(dot_cols):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        seg = s[a:b]
        lo[i] = float(seg.min())
        hi[i] = float(seg.max())

    bits_l = (0x01, 0x02, 0x04, 0x40)
    bits_r = (0x08, 0x10, 0x20, 0x80)
    grid = [[0] * cells for _ in range(rows)]
    for x in range(dot_cols):
        top = int(round((1.0 - (float(hi[x]) + 1.0) / 2.0) * (dot_rows - 1)))
        bot = int(round((1.0 - (float(lo[x]) + 1.0) / 2.0) * (dot_rows - 1)))
        top = max(0, min(dot_rows - 1, top))
        bot = max(0, min(dot_rows - 1, bot))
        if bot < top:
            top, bot = bot, top
        bits = bits_l if x % 2 == 0 else bits_r
        for d in range(top, bot + 1):
            grid[d // 4][x // 2] |= bits[d % 4]

    return ["".join(chr(0x2800 + grid[r][c]) if grid[r][c] else " " for c in range(cells))
            for r in range(rows)]


def meter(frac: float, width: int, theme: dict, peak: float | None = None) -> str:
    width = max(2, width)
    frac = max(0.0, min(1.0, frac))
    inner = width - 2
    fill = int(round(frac * inner))
    body = "█" * fill + "░" * (inner - fill)
    color = lerp(theme["bar"], theme["err"], max(0.0, (frac - 0.75) / 0.25))
    text = fg(color) + "▐" + body + "▌"
    if peak is not None and peak > frac + 0.02:
        pos = min(inner - 1, int(round(min(1.0, peak) * inner)))
        body = body[:pos] + "▏" + body[pos + 1:]
        text = fg(color) + "▐" + body + "▌"
    return text + RESET


def cache_bar(frac: float, width: int, theme: dict, low: bool = False) -> str:
    """抓轨水位条：低于安全线用告警色。"""
    frac = max(0.0, min(1.0, frac))
    inner = max(1, width)
    fill = int(round(frac * inner))
    color = theme["err"] if low else theme["bar"]
    return fg(color) + "█" * fill + fg(theme["bg_bar"]) + "░" * (inner - fill) + RESET
