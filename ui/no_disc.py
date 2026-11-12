"""无盘/开仓稳态界面：A+ 版 1-DIN 车机卡。

状态严格按光驱能力区分：
  open          托盘开着，机器不知道盘在不在（不能断言 NO DISC）
  closed_empty  关仓后 query 确认无盘
  checking      关仓后正在 query/等 TOC
  reading       query 已返回，TOC 完成过渡
  unsupported   确认有盘但没有 CD-DA 音轨（数据 CD/DVD）
  unreadable    有盘但宽限期后仍认不出 CD-DA（读 TOC 失败/未知介质）
  ready         盘就绪，可回仪表盘

面板/槽/按钮/扫描线复用 ui.boot 的实体；这里只换 LCD 内容和操作提示。
"""
from __future__ import annotations

from ui.boot import (MIN_H, MIN_W, Grid, _face_rect, _lcd_rect, _slot_rect,
                     draw_box, draw_buttons)
from ui.glyphs import RESET, fg, fit, text_width, time_fmt

PHASES = ("open", "closed_empty", "checking", "reading",
          "unsupported", "unreadable", "ready")

_LABELS = {
    "open": "TRAY OPEN — INSERT CD",
    "closed_empty": "NO DISC — INSERT CD",
    "checking": "CHECKING DISC…",
    "reading": "TOC READY",
    "unsupported": "UNSUPPORTED DISC — AUDIO CD ONLY",
    "unreadable": "UNREADABLE DISC — CHECK OR EJECT",
    "ready": "CD READY",
}


def render(w: int, h: int, theme, phase: str, t: float,
           tracks: int = 0, total_seconds: float = 0.0,
           cached: int = 0) -> list[str]:
    """返回正好 h 行、宽 w 的无盘界面。"""
    w, h = max(1, int(w)), max(1, int(h))
    if phase not in PHASES:
        phase = "checking"
    if w < MIN_W or h < MIN_H:
        return _compact(w, h, theme, phase, tracks, total_seconds)
    g = Grid(w, h)
    fx, fy, fw, fh = _face_rect(g)
    lx, ly, lw, lh = _lcd_rect(g)
    sx, sy, sw, sh = _slot_rect(g)
    draw_box(g, fx, fy, fx + fw - 1, fy + fh - 1, theme["text"])

    # 槽：只有 open 是开口；关仓后盖板合上
    g.text(sx, sy, "▁" * sw, theme["text"])
    if phase == "open":
        g.fill(sx, sy + 1, sx + sw - 1, sy + 1, " ", theme["text"], (0, 0, 0))
        g.put(sx + sw // 2, sy + 1, "▾", theme["accent"], (0, 0, 0))
    else:
        g.text(sx, sy + 1, "▬" * sw, theme["dim"])
    g.text(sx, sy + 2, "▔" * sw, theme["dim"])

    led = {"open": theme["dim"], "closed_empty": theme["dim"],
           "checking": theme["warn"], "reading": theme["warn"],
           "unsupported": theme["err"], "unreadable": theme["err"],
           "ready": theme["bar"]}[phase]
    g.text(sx, sy + 3, "●", led)
    g.text(sx + 2, sy + 3, "CD IN",
           theme["dim"] if phase in ("open", "closed_empty") else led)
    g.text(sx + sw - 5, sy + 3, "EJECT",
           theme["accent"] if phase in ("open", "closed_empty",
                                        "unsupported", "unreadable")
           else theme["dim"])

    # LCD：保留老车机的指示灯 / TRK / 轨道条 / MEM
    bg = theme["bg_bar"]
    g.fill(lx, ly, lx + lw - 1, ly + lh - 1, " ", theme["text"], bg)
    for i, label in enumerate(("CD", "RPT", "RDM", "DSP", "EQ")):
        g.text(lx + 2 + i * 6, ly, label,
               theme["accent"] if i == 0 else theme["dim"], bg)

    if phase == "ready":
        g.text(lx + 2, ly + 2, "CD READY", theme["text"], bg)
        trk = f"{tracks:2d} TRK" if tracks else "-- TRK"
        total = time_fmt(total_seconds) if total_seconds else "--:--"
        g.text(lx + lw - 1 - text_width(total), ly + 2, total,
               theme["text"], bg)
        g.text(lx + lw - 2 - text_width(total) - text_width(trk), ly + 2,
               trk, theme["dim"], bg)
    else:
        msg = {"open": "TRAY OPEN", "closed_empty": "NO DISC",
               "checking": "CHECKING", "reading": "TOC READY",
               "unsupported": "UNSUPPORTED",
               "unreadable": "UNREADABLE"}[phase]
        if phase in ("checking", "reading"):
            msg += "." * (int(t * 4) % 4)
        g.text(lx + 2, ly + 2, msg,
               theme["err"] if phase in ("unsupported", "unreadable")
               else theme["warn"], bg)
        if phase in ("open", "closed_empty"):
            cursor = "▸" if int(t * 1.0) % 2 == 0 else " "
            g.text(lx + 14, ly + 2, cursor + " INSERT CD",
                   theme["text"], bg)
        elif phase == "unsupported":
            g.text(lx + 15, ly + 2, "AUDIO CD ONLY", theme["text"], bg)
        elif phase == "unreadable":
            g.text(lx + 15, ly + 2, "CHECK DISC", theme["text"], bg)
        g.text(lx + lw - 15, ly + 2, "-- TRK", theme["dim"], bg)
        g.text(lx + lw - 5, ly + 2, "--:--", theme["dim"], bg)

    if phase == "checking":
        # 真正阻塞读 TOC 的是 query 阶段，扫描放在这里；不给假百分比。
        _draw_lead_in(g, lx, ly, bg, theme, t, complete=False)
    elif phase == "reading":
        _draw_lead_in(g, lx, ly, bg, theme, t, complete=True)
    elif phase == "ready":
        g.text(lx + 2, ly + 3, "TOC OK", theme["bar"], bg)
        g.text(lx + 12, ly + 3, "▓" * 34, theme["scope"], bg)

    pos = int(t * 0.8) % 16
    for i in range(16):
        active = phase not in ("ready", "unsupported", "unreadable") and i == pos
        g.text(lx + 2 + i * 3, ly + 4,
               "▓▓" if active else "░░",
               theme["scope"] if active else theme["dim"], bg)
    g.text(lx + lw - 10, ly + 4, "MEM", theme["dim"], bg)
    mem = (f"{cached:02d}/{tracks:02d}"
           if phase == "ready" and tracks else "--/--")
    g.text(lx + lw - 6, ly + 4, mem, theme["dim"], bg)

    draw_buttons(g, fx, fy, fw, theme)
    g.scan_area = (lx, ly, lx + lw - 1, ly + lh - 1)
    return [fit(row, w) for row in g.render(scan=True)]


def _draw_lead_in(g: Grid, lx: int, ly: int, bg, theme, t: float,
                  *, complete: bool = False) -> None:
    """TOC lead-in：查询中只跑扫描头，不给会造成误解的假百分比。"""
    x0 = lx + 12
    width = 34
    g.text(lx + 2, ly + 3, "LEAD-IN", theme["dim"], bg)
    if complete:
        g.text(x0, ly + 3, "▓" * width, theme["scope"], bg)
        return
    head = int(((t * 0.55) % 1.0) * (width - 1))
    for i in range(width):
        d = abs(i - head)
        if d == 0:
            ch, color = "█", theme["accent"]
        elif d <= 2:
            ch, color = "▓", theme["scope"]
        else:
            ch, color = "░", theme["dim"]
        g.put(x0 + i, ly + 3, ch, color, bg)


def _compact(w: int, h: int, theme, phase: str,
             tracks: int, total_seconds: float) -> list[str]:
    """小窗口兜底：不画 1-DIN，只给一行状态。"""
    label = _LABELS[phase]
    if phase == "ready" and tracks:
        label = f"CD READY · {tracks:2d} TRK · {time_fmt(total_seconds)}"
    lines = [
        "  " + fg(theme["accent"]) + "asciicd" + RESET,
        "",
        "  " + fg(theme["warn"] if phase != "ready" else theme["text"])
        + label + RESET,
    ]
    top = max(0, (h - len(lines)) // 2)
    rows = [""] * top + [fit(line, w) for line in lines]
    rows += [""] * max(0, h - len(rows))
    return rows[:h]
