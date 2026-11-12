"""待机动画：正弦示波器 + 扫描频谱 + 点阵余辉瀑布。

只做纯计算/字符，不碰线程和终端。布局层传入 live 数据、主题和尺寸，
拿回可以直接放进 dash 的行/采样。

时间轴：
  - 一轮扫描固定 16.7 秒，正好是 2 个正弦周期；
  - 示波器正弦用线性相位，匀速移动，不跟频谱的快慢变化；
  - 频谱扫描头用 smoothstep，头尾慢、中段快；
  - 频谱亮峰、点阵扫描头共用同一个 u；示波器只在首/中/尾等锚点与它对齐，
    速度变化的头尾允许短暂不精确跟随。
"""
from __future__ import annotations

import math

import numpy as np

from config import SPECTRUM_BINS
from ui.glyphs import RESET, fg

SCOPE_CYCLES = 2.0        # 屏幕上 2 个完整正弦周期
SINE_RATE = 0.12          # 周期/秒
T_SWEEP = SCOPE_CYCLES / SINE_RATE        # 16.667s
SCOPE_ROW_OFFSET = 2      # 示波器内容从边框内第 2 列开始
SPEC_COL_OFFSET = 7       # 频谱条从边框内第 7 列开始
SCOPE_HISTORY = 50        # 点阵余辉采样数
HISTORY_DT = 0.08
DECAY_S = 2.4


def sweep_linear(t: float) -> float:
    """示波器正弦的匀速扫描进度 0..1；整轮扫完刚好 2 个周期。"""
    return (t / T_SWEEP) % 1.0


def sweep_ease(t: float) -> float:
    """频谱扫描头的一轮进度 0..1：smoothstep，头尾速度 0。"""
    s = (t / T_SWEEP) % 1.0
    return s * s * (3.0 - 2.0 * s)


def sweep_u(t: float, cells: int, spec_w: int) -> float:
    """频谱扫描头的 x（0..1）；示波器峰只在速度变化的锚点附近与它对齐。"""
    cells = max(1, cells)
    spec_w = max(1, spec_w)
    x0 = (SPEC_COL_OFFSET - SCOPE_ROW_OFFSET) / cells
    u_sine = x0 + sweep_ease(t)
    screen_col = SCOPE_ROW_OFFSET + u_sine * cells
    return max(0.0, min(1.0, (screen_col - SPEC_COL_OFFSET) / spec_w))


def sine_wave(t: float, n: int = 768, cells: int = 99) -> np.ndarray:
    """2 个完整周期；正弦按匀速移动，t=0 时第一个峰落在频谱第一根柱上。"""
    cells = max(1, cells)
    x0 = (SPEC_COL_OFFSET - SCOPE_ROW_OFFSET) / cells
    x = np.linspace(0.0, 1.0, max(64, n), endpoint=False)
    amp = 0.30 + 0.03 * math.sin(2 * math.pi * t / 8.5)
    phase = SCOPE_CYCLES * (x0 + sweep_linear(t)) - 0.25
    return (amp * np.sin(2 * math.pi * (SCOPE_CYCLES * x - phase))).astype(np.float32)


def _gauss(x: np.ndarray, center: float, width: float) -> np.ndarray:
    return np.exp(-0.5 * ((x - center) / width) ** 2)


def _edge(u: float) -> float:
    """扫描两端淡入淡出，藏掉换行；右端比左端更早收尾一点。"""
    e = max(0.0, min(1.0, u / 0.02, (1.0 - u) / 0.04))
    return e * e * (3.0 - 2.0 * e)


def sweep_spectrum(t: float, cells: int, spec_w: int) -> tuple[np.ndarray, np.ndarray]:
    """扫描峰的 64 bin 频谱 + 峰值标记；和正弦共用一个 u。"""
    x = np.linspace(0.0, 1.0, SPECTRUM_BINS)
    u = sweep_u(t, cells, spec_w)
    e = _edge(u)
    base = 0.028 + 0.007 * np.sin(2 * np.pi * (t / 8.0 + x * 7.0))
    spec = (base
            + 0.80 * e * _gauss(x, u, 0.012)
            + 0.28 * e * _gauss(x, u - 0.035, 0.016)
            + 0.10 * e * _gauss(x, u - 0.070, 0.018))
    spec = np.clip(spec, 0.0, 1.0).astype(np.float32)
    peaks = np.clip(spec + 0.02, 0.0, 1.0).astype(np.float32)
    return spec, peaks


def _spans(samples: np.ndarray, dot_cols: int, dot_rows: int,
           auto_gain: bool) -> tuple[np.ndarray, np.ndarray]:
    samples = np.asarray(samples, dtype=np.float32).ravel()
    if samples.size < dot_cols:
        samples = np.pad(samples, (0, dot_cols - samples.size))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-5:
        mid = np.full(dot_cols, (dot_rows - 1) // 2, dtype=int)
        return mid, mid.copy()
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
    top = np.rint((1.0 - (hi + 1.0) / 2.0) * (dot_rows - 1)).astype(int)
    bot = np.rint((1.0 - (lo + 1.0) / 2.0) * (dot_rows - 1)).astype(int)
    top = np.clip(top, 0, dot_rows - 1)
    bot = np.clip(bot, 0, dot_rows - 1)
    return np.minimum(top, bot), np.maximum(top, bot)


def _render_spans(top: np.ndarray, bot: np.ndarray, cells: int,
                  rows: int) -> list[str]:
    bits_l = (0x01, 0x02, 0x04, 0x40)
    bits_r = (0x08, 0x10, 0x20, 0x80)
    out = []
    for r in range(rows):
        line = []
        for c in range(cells):
            bits = 0
            for d in range(4):
                y = r * 4 + d
                if top[c * 2] <= y <= bot[c * 2]:
                    bits |= bits_l[d]
                if top[c * 2 + 1] <= y <= bot[c * 2 + 1]:
                    bits |= bits_r[d]
            line.append(chr(0x2800 + bits) if bits else " ")
        out.append("".join(line))
    return out


def morph_scope(live: np.ndarray, sine: np.ndarray, mix: float,
                cells: int, rows: int) -> list[str]:
    """live 的上下点行逐列插值到正弦，mix=0 live，mix=1 sine。"""
    mix = max(0.0, min(1.0, mix))
    lt, lb = _spans(live, cells * 2, rows * 4, auto_gain=True)
    it, ib = _spans(sine, cells * 2, rows * 4, auto_gain=False)
    top = np.rint(lt * (1.0 - mix) + it * mix).astype(int)
    bot = np.rint(lb * (1.0 - mix) + ib * mix).astype(int)
    return _render_spans(top, bot, cells, rows)


def _hash2(x, y):
    h = np.sin((x + 1.0) * 12.9898 + (y + 1.0) * 78.233) * 43758.5453
    return h - np.floor(h)


def _mix_color(a, b, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def dotfield_waterfall(t: float, theme, scope_cells: int, spec_w: int,
                       rows: int) -> list[str]:
    """独立视觉的 Braille 点阵余辉：底噪 + 扫描头点亮 + 2.4s 衰减。

    扫描头与频谱峰共用 u；右缘 sigma 0.016 比左侧 0.024 更急，
    但前面还铺了一层更宽的预辉光，避免突然变密。
    """
    spec_w = max(1, spec_w)
    dot_rows, dot_cols = rows * 4, spec_w * 2
    xs = np.linspace(0.0, 1.0, dot_cols)
    yf = np.linspace(0.0, 1.0, dot_rows)
    xg = np.arange(dot_cols)[None, :]
    yg = np.arange(dot_rows)[:, None]

    phase = _hash2(xg, yg)
    wave = 0.5 + 0.5 * np.sin(2 * np.pi * t / 6.5 + phase * 2 * np.pi)
    env = np.exp(-0.5 * ((yf - 0.5) / 0.45) ** 2)[:, None]
    base = 0.14 * wave * env

    boost = np.zeros((dot_rows, dot_cols), np.float32)
    for k in range(SCOPE_HISTORY):
        age = k * HISTORY_DT
        tt = t - age
        u = sweep_u(tt, scope_cells, spec_w)
        e = _edge(u)
        if e <= 0.0:
            continue
        yc = 0.5 + 0.20 * math.sin(2 * math.pi * tt / 4.5)
        decay = math.exp(-age / DECAY_S)
        dx = xs - u
        sigma = np.where(dx < 0.0, 0.024, 0.016)
        prof = np.exp(-0.5 * (dx / sigma) ** 2)
        vert = np.exp(-0.5 * ((yf - yc) / 0.22) ** 2)
        boost += (0.34 * e * decay) * prof[None, :] * vert[:, None]

    u0 = sweep_u(t, scope_cells, spec_w)
    e0 = _edge(u0)
    yc0 = 0.5 + 0.20 * math.sin(2 * math.pi * t / 4.5)
    dx0 = xs - u0
    pre = np.where(dx0 > 0.0, np.exp(-0.5 * (dx0 / 0.050) ** 2), 0.0)
    vert0 = np.exp(-0.5 * ((yf - yc0) / 0.22) ** 2)
    boost += (0.12 * e0) * pre[None, :] * vert0[:, None]

    inten = np.clip(base + boost, 0.0, 1.0)
    inten = inten * (0.55 + 0.45 * yf)[:, None]
    h = _hash2(xg + 1.0, yg + 5.0)
    on = inten > h * 0.95

    bits_l = (0x01, 0x02, 0x04, 0x40)
    bits_r = (0x08, 0x10, 0x20, 0x80)
    out = []
    for r in range(rows):
        line = []
        fade = 0.60 + 0.28 * (r / max(1, rows - 1))
        for c in range(spec_w):
            bits = 0
            cmax = 0.0
            for d in range(4):
                y = r * 4 + d
                if on[y, 2 * c]:
                    bits |= bits_l[d]
                    cmax = max(cmax, float(inten[y, 2 * c]))
                if on[y, 2 * c + 1]:
                    bits |= bits_r[d]
                    cmax = max(cmax, float(inten[y, 2 * c + 1]))
            if not bits:
                line.append(" ")
                continue
            color = _mix_color(theme["spec_lo"], theme["spec_hi"],
                               c / max(1, spec_w - 1))
            if cmax > 0.55:
                color = _mix_color(color, theme["text"], 0.55)
            color = tuple(int(v * fade) for v in color)
            line.append(fg(color) + chr(0x2800 + bits))
        line.append(RESET)
        out.append("".join(line))
    return out
