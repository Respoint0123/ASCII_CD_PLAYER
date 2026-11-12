"""开机画面：1-DIN 车机面板 + 电动 CD 槽 + 四选一 LCD 点亮。

这一版只负责「某一时刻画成一屏字符」：不碰终端、线程和播放器。
时间轴、面板尺寸与扫描线范围按早先的设计稿确定，改动时三处要一起对。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ui.glyphs import RESET, bg, cw, fg, fit, lerp, time_fmt

INTRO_NAMES = ("open", "backlight", "static", "flash")
BTN_LABELS = ("◀◀", "▶‖", "▶▶", "DISP", "MUTE")

CRT_INTRO_END = 0.25
CRT_REVEAL_START = 0.27
CRT_REVEAL_DUR = 0.48
READY_HOLD = 0.40           # READY/PAUSE 至少停留这么久再展开（TOC 慢时）
MOTOR_OPEN = (0.18, 0.42)
DISC_SLIDE = (0.42, 0.68)
MOTOR_CLOSE = (0.70, 0.90)

SCAN_DIM = 0.60
MIN_W = 84          # 再小就不画面板，交给 app 的纯文字兜底
MIN_H = 18


@dataclass
class BootInfo:
    """启动画面需要知道的全部外部状态。ready 之前不展开、不显示真实曲目。"""

    fast: bool = False
    no_disc: bool = False
    unsupported: bool = False
    unreadable: bool = False
    paused: bool = False
    ready: bool = False
    hold_expand: bool = False   # 真盘 TOC 慢时先停在 READY，别急着展开
    intro: int = 0
    tracks: int = 0
    total_seconds: float = 0.0
    cached: int = 0

    @property
    def intro_name(self) -> str:
        return INTRO_NAMES[self.intro % len(INTRO_NAMES)]

    @property
    def blocked(self) -> bool:
        """没有可播放的 CD-DA（无盘/不支持/读不出）；启动画面不展开 READY。"""
        return self.no_disc or self.unsupported or self.unreadable


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def h01(x: int, y: int, seed: int = 0) -> float:
    n = (x * 374761393 + y * 668265263 + seed * 1013904223) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((n ^ (n >> 16)) & 0xFFFF) / 65535.0


class Grid:
    """固定尺寸字符网格；每格 (字符, 前景, 背景)。CJK 占两格，第二格记 ""。"""

    def __init__(self, w: int, h: int):
        self.w = w
        self.h = h
        self.c: list[list[tuple[str, tuple | None, tuple | None] | None]] = (
            [[None] * w for _ in range(h)])
        self.scan_area: tuple[int, int, int, int] | None = None

    def put(self, x: int, y: int, ch: str, color, bgc=None) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.c[y][x] = (ch, color, bgc)

    def text(self, x: int, y: int, s: str, color, bgc=None) -> None:
        for ch in s:
            if ch == "\n":
                continue
            width = cw(ch)
            if 0 <= x < self.w and 0 <= y < self.h:
                self.c[y][x] = (ch, color, bgc)
                if width == 2 and x + 1 < self.w:
                    self.c[y][x + 1] = ("", color, bgc)
            x += width

    def fill(self, x0: int, y0: int, x1: int, y1: int, ch: str, color, bgc=None) -> None:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.put(x, y, ch, color, bgc)

    def render(self, brightness: float = 1.0, scan: bool = False) -> list[str]:
        rows = []
        for y, row in enumerate(self.c):
            dim_row = scan and y % 2 == 1
            parts: list[str] = []
            cur = None
            for x, cell in enumerate(row):
                if cell is None:
                    ch, color, bgc = " ", None, None
                else:
                    ch, color, bgc = cell
                    if ch == "":
                        continue
                in_area = True
                if self.scan_area is not None:
                    ax0, ay0, ax1, ay1 = self.scan_area
                    in_area = ax0 <= x <= ax1 and ay0 <= y <= ay1
                b = brightness * (SCAN_DIM if dim_row and in_area else 1.0)
                fgc = tuple(min(255, int(v * b)) for v in color) if color else None
                bgs = tuple(min(255, int(v * b)) for v in bgc) if bgc else None
                key = (fgc, bgs)
                if key != cur:
                    parts.append(RESET)
                    if bgs is not None:
                        parts.append(bg(bgs))
                    if fgc is not None:
                        parts.append(fg(fgc))
                    cur = key
                parts.append(ch)
            parts.append(RESET)
            rows.append("".join(parts))
        return rows


# ── 面板 / 时间轴 ─────────────────────────────────────────────────────────
def timeline(fast: bool, no_disc: bool = False) -> dict:
    if fast:
        return {"toc": 0.22, "cache": 0.30, "ready": 0.90, "expand": 1.02,
                "dash": 1.14, "wait": 0.80}
    return {"toc": 2.05, "cache": 2.12, "ready": 2.35, "expand": 2.75,
            "dash": 2.87, "wait": 1.50}


def draw_box(g: Grid, x0: int, y0: int, x1: int, y1: int, color,
             title: str | None = None, title_color=None) -> None:
    if x1 <= x0 or y1 < y0:
        return
    w = x1 - x0 + 1
    g.text(x0, y0, "┌" + "─" * (w - 2) + "┐", color)
    if y1 > y0:
        for y in range(y0 + 1, y1):
            g.put(x0, y, "│", color)
            g.put(x1, y, "│", color)
        g.text(x0, y1, "└" + "─" * (w - 2) + "┘", color)
    if title and w > len(title) + 6:
        g.text(x0 + 2, y0, " " + title + " ", title_color or color)


def expand_box(g: Grid, x0: int, y0: int, x1: int, y1: int, p: float, theme) -> None:
    p = clamp01(p)
    X0 = round(x0 * (1 - p))
    Y0 = round(y0 * (1 - p))
    X1 = round(x1 + (g.w - 1 - x1) * p)
    Y1 = round(y1 + (g.h - 1 - y1) * p)
    col = theme["dim"] if p < 0.7 else theme["text"]
    draw_box(g, X0, Y0, X1, Y1, col, "asciicd", theme["accent"])


# ── LCD 内容 ──────────────────────────────────────────────────────────────
def draw_indicators(g: Grid, x: int, y: int, t: float, theme,
                    bgc, blocked: bool = False) -> None:
    labels = ("CD", "RPT", "RDM", "DSP", "EQ")
    test = t < 0.16
    for i, label in enumerate(labels):
        if test:
            on = True
        elif i == 0:
            on = (not blocked) or int(t * 2) % 2 == 0
        else:
            on = False
        g.text(x + i * 6, y, label, theme["accent"] if on else theme["dim"], bgc)


def draw_message(g: Grid, x: int, y: int, t: float, info: BootInfo, theme, bgc) -> None:
    tl = timeline(info.fast)
    if info.unsupported:
        txt, col = "UNSUPPORTED DISC", theme["err"]
    elif info.unreadable:
        txt, col = "UNREADABLE DISC", theme["err"]
    elif info.no_disc:
        if t < tl["wait"]:
            txt, col = "READING", theme["warn"]
        else:
            txt = "NO DISC" if int(t * 2) % 2 == 0 else "INSERT CD"
            col = theme["warn"]
    elif not info.ready or t < tl["toc"]:
        txt, col = "READING" + "." * (int(t * 4) % 4), theme["warn"]
    elif t < tl["ready"]:
        txt, col = "CD READY", theme["text"]
    else:
        txt = "PAUSE" if info.paused else "READY"
        col = theme["accent"]
    g.text(x, y, txt, col, bgc)


def draw_tracks(g: Grid, x: int, y: int, t: float, info: BootInfo, theme, bgc) -> None:
    tl = timeline(info.fast)
    filled = 0
    if info.ready and not info.blocked and info.tracks and t >= tl["cache"]:
        filled = min(16, int(round(16 * info.cached / info.tracks)))
    scanning = (not info.blocked) and (not info.ready or t < tl["toc"])
    pos = int(t * 9) % 16
    for i in range(16):
        if i < filled:
            ch, col = "██", theme["accent"]
        elif scanning and i == pos:
            ch, col = "▓▓", theme["scope"]
        elif info.blocked and i == 0 and int(t * 2) % 2 == 0:
            ch, col = ("██",
                       theme["err"] if info.unsupported or info.unreadable
                       else theme["warn"])
        else:
            ch, col = "░░", theme["dim"]
        g.text(x + i * 3, y, ch, col, bgc)


def draw_lcd(g: Grid, lx: int, ly: int, lw: int, t: float, info: BootInfo,
             theme, bgc) -> None:
    tl = timeline(info.fast)
    ok = info.ready and not info.blocked and t >= tl["toc"]
    col = theme["text"] if ok else theme["dim"]
    g.text(lx + lw - 15, ly + 2, f"{info.tracks:2d} TRK" if ok else "-- TRK", col, bgc)
    total = time_fmt(info.total_seconds) if ok else "--:--"
    g.text(lx + lw - 2 - len(total), ly + 2, total, col, bgc)
    draw_message(g, lx + 2, ly + 2, t, info, theme, bgc)
    draw_tracks(g, lx + 2, ly + 4, t, info, theme, bgc)
    cache_ok = ok and t >= tl["cache"]
    g.text(lx + lw - 10, ly + 4, "MEM", theme["dim"], bgc)
    if info.tracks:
        mem = f"{info.cached}/{info.tracks}" if cache_ok else f"--/{info.tracks}"
    else:
        mem = "--/--"
    g.text(lx + lw - 6, ly + 4, mem,
           theme["accent"] if cache_ok else theme["dim"], bgc)


# ── 1-DIN 面板 / 槽门 ─────────────────────────────────────────────────────
def _face_rect(g: Grid) -> tuple[int, int, int, int]:
    fw, fh = min(80, g.w - 4), 18
    return (g.w - fw) // 2, (g.h - fh) // 2, fw, fh


def _lcd_rect(g: Grid) -> tuple[int, int, int, int]:
    fx, fy, fw, fh = _face_rect(g)
    return fx + 1, fy + 1, fw - 2, 6


def _slot_rect(g: Grid) -> tuple[int, int, int, int]:
    fx, fy, fw, fh = _face_rect(g)
    return fx + (fw - 38) // 2, fy + 8, 38, 3


def draw_buttons(g: Grid, fx: int, fy: int, fw: int, theme) -> None:
    bw, gap = 7, 1
    total = len(BTN_LABELS) * bw + (len(BTN_LABELS) - 1) * gap
    bx, by = fx + (fw - total) // 2, fy + 13
    for i, label in enumerate(BTN_LABELS):
        kx = bx + i * (bw + gap)
        g.text(kx, by, "┌" + "─" * (bw - 2) + "┐", theme["dim"])
        g.put(kx, by + 1, "│", theme["dim"])
        g.put(kx + bw - 1, by + 1, "│", theme["dim"])
        g.text(kx, by + 2, "└" + "─" * (bw - 2) + "┘", theme["dim"])
        g.text(kx + (bw - len(label)) // 2, by + 1, label, theme["text"])


def draw_faceplate(g: Grid, t: float, theme, info: BootInfo) -> None:
    """完整 1-DIN：亮框、6 行 LCD、38 格 CD 槽、电动槽门、CD IN 灯、五颗键。"""
    fx, fy, fw, fh = _face_rect(g)
    draw_box(g, fx, fy, fx + fw - 1, fy + fh - 1, theme["text"])
    sx, sy, sw, sh = _slot_rect(g)

    g.text(sx, sy, "▁" * sw, theme["text"], (0, 0, 0))
    g.fill(sx, sy + 1, sx + sw - 1, sy + 1, " ", theme["text"], (0, 0, 0))
    g.text(sx, sy + 2, "▔" * sw, theme["dim"], (0, 0, 0))

    # 电动槽门：关 → 开 → 吞盘 → 关
    cover = sw
    if MOTOR_OPEN[0] <= t < MOTOR_OPEN[1]:
        p = (t - MOTOR_OPEN[0]) / (MOTOR_OPEN[1] - MOTOR_OPEN[0])
        cover = int(sw * (1 - p))
    elif MOTOR_OPEN[1] <= t < MOTOR_CLOSE[0]:
        cover = 0
    elif MOTOR_CLOSE[0] <= t < MOTOR_CLOSE[1]:
        p = (t - MOTOR_CLOSE[0]) / (MOTOR_CLOSE[1] - MOTOR_CLOSE[0])
        cover = int(sw * p)
    if cover > 0:
        g.text(sx, sy + 1, "▬" * cover, theme["dim"], (0, 0, 0))

    # 盘片边缘滑入（槽门开着）
    if not info.blocked and DISC_SLIDE[0] <= t < DISC_SLIDE[1]:
        p = (t - DISC_SLIDE[0]) / (DISC_SLIDE[1] - DISC_SLIDE[0])
        seg = 8
        start = sx + sw - int(p * (sw + seg))
        for i in range(seg):
            xx = start + i
            if sx <= xx < sx + sw:
                g.put(xx, sy + 1, "▬", theme["accent"], (0, 0, 0))

    # ● CD IN 灯 + 丝印
    if t < MOTOR_OPEN[0]:
        led = theme["dim"]
    elif t < MOTOR_CLOSE[1]:
        led = theme["warn"] if int(t * 8) % 2 == 0 else theme["dim"]
    else:
        led = theme["bar"]
    g.text(sx, sy + 3, "●", led)
    g.text(sx + 2, sy + 3, "CD IN",
           theme["warn"] if MOTOR_OPEN[0] <= t < MOTOR_CLOSE[1] else theme["dim"])
    g.text(sx + sw - 5, sy + 3, "EJECT", theme["dim"])
    if info.blocked:
        g.text(fx + fw - 4, sy + 3, "Q", theme["dim"])

    draw_buttons(g, fx, fy, fw, theme)


# ── 四种 LCD 点亮 ─────────────────────────────────────────────────────────
def _intro_open(g: Grid, t: float, theme, info: BootInfo) -> None:
    lx, ly, lw, lh = _lcd_rect(g)
    cy = ly + lh // 2
    if t < 0.08:
        n = max(1, int(lw * clamp01(t / 0.08)))
        g.text(lx + (lw - n) // 2, cy, "▄" * n, theme["accent"])
        return
    q = clamp01((t - 0.08) / 0.17)
    hh = max(1, int(lh * q))
    top = cy - (hh - 1) // 2
    bot = top + hh - 1
    if hh == 1:
        g.text(lx, top, "▄" * lw, theme["accent"])
    else:
        g.text(lx, top, "┌" + "─" * (lw - 2) + "┐", theme["text"])
        g.text(lx, bot, "└" + "─" * (lw - 2) + "┘", theme["text"])
        for yy in range(top + 1, bot):
            g.put(lx, yy, "│", theme["text"])
            g.put(lx + lw - 1, yy, "│", theme["text"])


def _intro_backlight(g: Grid, t: float, theme, info: BootInfo) -> None:
    lx, ly, lw, lh = _lcd_rect(g)
    k = clamp01((t - 0.03) / 0.19)
    if k <= 0:
        return
    panel = lerp((0, 0, 0), theme["bg_bar"], k)
    g.fill(lx, ly, lx + lw - 1, ly + lh - 1, " ", theme["text"], panel)
    if 0.19 <= t < CRT_INTRO_END:
        yy = ly + lh // 2
        g.text(lx + 2, yy, "▁" * (lw - 4), theme["accent"])


def _intro_static(g: Grid, t: float, theme, info: BootInfo) -> None:
    lx, ly, lw, lh = _lcd_rect(g)
    rng = random.Random(int(t * 60))
    dens = 1.0 if t < 0.18 else 0.6
    for yy in range(ly, ly + lh):
        for xx in range(lx, lx + lw):
            if rng.random() < dens:
                ch = rng.choice("░▒▓")
                col = rng.choice((theme["dim"], theme["scope"], theme["text"]))
                g.put(xx, yy, ch, col,
                      theme["bg_bar"] if rng.random() < 0.55 else (0, 0, 0))


def _intro_flash(g: Grid, t: float, theme, info: BootInfo) -> None:
    if t < 0.02:
        return
    lx, ly, lw, lh = _lcd_rect(g)
    on = (0.03 <= t < 0.08) or (0.12 <= t < 0.17)
    if on:
        g.fill(lx, ly, lx + lw - 1, ly + lh - 1, "█",
               theme["text"], theme["bg_bar"])
    else:
        g.fill(lx, ly, lx + lw - 1, ly + lh - 1, " ",
               theme["text"], (0, 0, 0))


_INTROS = (_intro_open, _intro_backlight, _intro_static, _intro_flash)


def scene(g: Grid, t: float, theme, info: BootInfo) -> None:
    tl = timeline(info.fast)
    draw_faceplate(g, t, theme, info)
    lx, ly, lw, lh = _lcd_rect(g)
    g.scan_area = (lx, ly, lx + lw - 1, ly + lh - 1)

    if t < CRT_INTRO_END:
        intro = int(info.intro) % len(_INTROS)
        _INTROS[intro](g, t, theme, info)
        return

    bgc = theme["bg_bar"]
    g.fill(lx, ly, lx + lw - 1, ly + lh - 1, " ", theme["text"], bgc)

    target = Grid(lw, lh)
    target.fill(0, 0, lw - 1, lh - 1, " ", theme["text"], bgc)
    draw_indicators(target, 2, 0, t, theme, bgc, info.blocked)

    prog = clamp01((t - CRT_REVEAL_START) / CRT_REVEAL_DUR)
    rng = random.Random(int(t * 30))
    for yy in range(lh):
        for xx in range(lw):
            h = h01(xx, yy)
            if h < prog:
                g.c[ly + yy][lx + xx] = target.c[yy][xx]
            elif h < prog + 0.12:
                g.put(lx + xx, ly + yy, rng.choice("░▒"), theme["dim"], bgc)

    draw_lcd(g, lx, ly, lw, t, info, theme, bgc)
    if (info.ready and not info.blocked and not info.hold_expand
            and t >= tl["expand"]):
        fx, fy, fw, fh = _face_rect(g)
        p = (t - tl["expand"]) / max(1e-6, tl["dash"] - tl["expand"])
        expand_box(g, fx, fy, fx + fw - 1, fy + fh - 1, p, theme)


def render_frame(w: int, h: int, theme, t: float, info: BootInfo) -> list[str]:
    """画一帧；行数正好 h，每行显示宽度正好 w。"""
    w, h = max(1, int(w)), max(1, int(h))
    g = Grid(w, h)
    scene(g, t, theme, info)
    return [fit(row, w) for row in g.render(scan=True)]
