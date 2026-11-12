"""两种布局：全屏仪表盘 / 极简顶栏。都只吃 UIState，不碰线程。"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from config import (PREROLL_S, SEEK_ARROW_2_S, SEEK_ARROW_3_S,
                    SEEK_HOLD_GAP_S, SEEK_HOLD_RELEASE_S, TRACK_HINT_S)
from dsp.analyzer import VisFrame
from ui import idle as idle_anim
from ui.feedback import TrackSnapshot, TrackTransition, VisualPulse, transition_view
from ui.glyphs import (BLOCKS, BOLD, RESET, UNDER, UNDER_OFF, bg, braille_scope,
                       cache_bar, fg, fit, lerp, meter, resample,
                       spectrum_line, text_width, time_fmt, waterfall_line)

STATE_ICON = {
    "playing": "▸", "paused": "‖", "buffering": "…", "idle": "·",
    "eof": "·", "error": "!", "stopped": "■",
}
STATE_TEXT = {
    "playing": "playing", "paused": "paused", "buffering": "buffering",
    "idle": "idle", "eof": "end of track", "error": "error", "stopped": "stopped",
}


@dataclass
class UIState:
    device: str = ""
    model: str = ""
    tracks: list = field(default_factory=list)
    track_no: int = 1
    title: str = ""
    artist: str = ""
    album: str = ""
    position: float = 0.0
    duration: float = 0.0
    state: str = "idle"
    volume: float = 1.0
    theme: dict = field(default_factory=dict)
    theme_name: str = "phosphor"
    vis: VisFrame = field(default_factory=VisFrame)
    cache_lead: float = 0.0
    rip: dict = field(default_factory=dict)
    total_seconds: float = 0.0
    message: str = ""
    hint: str = ""
    spinner: str = ""
    minimal: bool = False
    layout: str = "dash"
    disc_present: bool = True       # False = 光驱里没盘（弹盘/换盘中）
    tray_state: str = ""            # open / closed-empty / closed-disc；"" = 未知/不适用
    tray_since: float = 0.0         # 该托盘状态最近一次变化的 time.monotonic()
    autopause_since: float = 0.0    # 最近一次进入自动缓冲的 time.monotonic()；0 = 未在缓冲
    scope_mix: float = 0.0          # 0=live，1=待机正弦/扫描
    idle_t: float = 0.0             # 进入待机后的秒数（暂停时归零）
    help: bool = False              # h 键打开的帮助布局（带 KEYS 行）
    track_transition: TrackTransition | None = None
    visual_pulse: VisualPulse | None = None


# ── 小工具 ────────────────────────────────────────────────────────────────
def _dim(theme, s: str) -> str:
    return fg(theme["dim"]) + s + RESET


def _lr(left: str, right: str, width: int, theme) -> str:
    rw = text_width(right)
    if rw > width:
        right = fit(right, width)
        rw = width
    out = fit(left, max(0, width - rw))
    return out + right


def _top(w: int, title: str, theme) -> str:
    left = "─ " + title + " "
    fill = max(0, (w - 2) - len(left))
    return fg(theme["dim"]) + "┌" + left + "─" * fill + "┐" + RESET


def _sep(w: int, title: str, theme) -> str:
    left = "─ " + title + " "
    fill = max(0, (w - 2) - len(left))
    return fg(theme["dim"]) + "├" + left + "─" * fill + "┤" + RESET


def _bottom(w: int, theme) -> str:
    return fg(theme["dim"]) + "└" + "─" * (w - 2) + "┘" + RESET


def _row(content: str, w: int, theme) -> str:
    return fg(theme["dim"]) + "│" + RESET + fit(content, w - 2) + fg(theme["dim"]) + "│" + RESET


def _err_count(rip: dict) -> int:
    errs = rip.get("errors") or {}
    return int(errs.get("V", 0)) + int(errs.get("!", 0))


def _error_counts(rip: dict) -> dict[str, int]:
    errs = rip.get("errors") or {}
    return {key: int(errs.get(key, 0) or 0) for key in ("V", "!", "+", "e")}


def _error_tag(errs: dict[str, int], theme) -> str:
    return (fg(theme["err"] if errs["V"] else theme["dim"])
            + f"V{errs['V']}" + RESET
            + fg(theme["warn"] if errs["!"] else theme["dim"])
            + f" !{errs['!']}" + RESET
            + fg(theme["warn"] if errs["+"] else theme["dim"])
            + f" +{errs['+']}" + RESET)


def _phase_tag(phase: str, stage: str, full: bool, theme) -> str:
    if stage == "failed":
        return fg(theme["err"]) + "FAILED" + RESET
    if stage == "error":
        return fg(theme["err"]) + "ERROR" + RESET
    if stage == "paused":
        return fg(theme["warn"]) + "PAUSED" + RESET
    if stage == "done":
        return fg(theme["bar"]) + "DONE" + RESET
    keys = ("read", "verify", "overlap")
    labels = keys if full else ("read", "ver", "ovl")
    active = phase if phase in keys else keys[0]
    return (fg(theme["dim"]) + "·" + RESET).join(
        fg(theme["accent"] if key == active else theme["dim"]) + label + RESET
        for key, label in zip(keys, labels))


def _resample_map(values, width: int) -> list[int]:
    """把任意长度的坏道桶压到 width 格；缩小时取区间最大值。"""
    vals = [max(0, int(v or 0)) for v in (values or [])]
    if width <= 0:
        return []
    if not vals:
        return [0] * width
    if len(vals) == width:
        return vals
    if len(vals) > width:
        n = len(vals)
        return [max(vals[i * n // width:(i + 1) * n // width] or [0])
                for i in range(width)]
    n = len(vals)
    return [vals[min(n - 1, (2 * i + 1) * n // (2 * width))]
            for i in range(width)]


def _error_map(values, width: int, theme) -> str:
    vals = _resample_map(values, width)
    out = []
    for value in vals:
        if value <= 0:
            ch, color = "░", theme["dim"]
        elif value == 1:
            ch, color = "▒", theme["warn"]
        elif value == 2:
            ch, color = "▓", theme["warn"]
        else:
            ch, color = "█", theme["err"]
        out.append(fg(color) + ch + RESET)
    return "".join(out)


def _cache_line(st: UIState, inner: int, theme) -> str:
    """DISC CACHE 行：抓轨阶段 / 错误 map / 缓冲水位都塞进同一行。"""
    rip = st.rip or {}
    track = rip.get("track")
    stage = str(rip.get("stage") or "")
    lead = max(0.0, float(st.cache_lead or 0.0))
    if not isinstance(track, int):
        label = "PAUSED" if stage == "paused" else "--"
        color = theme["warn"] if stage == "paused" else theme["dim"]
        return (fg(color) + f"  RIP {label} " + RESET
                + "▐" + cache_bar(0.0, 12, theme) + "▌"
                + fg(theme["text"]) + "   0% " + RESET
                + fg(theme["dim"]) + f" 0.0x  err 0  retry 0  buf {lead:5.1f}s" + RESET)

    pct = max(0.0, min(100.0, float(rip.get("pct", 0.0) or 0.0)))
    phase = str(rip.get("phase") or "").lower()
    errs = _error_counts(rip)
    total_err = errs["V"] + errs["!"] + errs["+"]
    buffering = st.state == "buffering" and lead < PREROLL_S
    remain = max(0.0, PREROLL_S - lead)
    bar_w = 12 if inner >= 90 else 8
    water_w = 10 if inner >= 90 else 6
    map_w = 20 if inner >= 90 else 12
    full_phase = inner >= 96
    num = f"  RIP {track:02d} "
    progress = (fg(theme["dim"]) + num + RESET
                + "▐" + cache_bar(pct / 100.0, bar_w, theme) + "▌"
                + fg(theme["text"]) + f" {pct:3.0f}% " + RESET)
    speed = fg(theme["text"]) + f" {float(rip.get('speed', 0.0) or 0.0):4.1f}x " + RESET
    has_error = stage in ("failed", "error") or total_err
    preempt = stage == "failed"          # 只有放弃重试才允许抢掉 BUF/AUTO

    # 缓冲时优先保留 BUF/AUTO；stage="error" 仍会重试，继续走水位行。
    if has_error and (not buffering or preempt):
        label = _phase_tag(phase, stage, full_phase, theme)
        line = (progress + " " + label + speed
                + " " + _error_tag(errs, theme)
                + fg(theme["dim"]) + f"  retry {errs['e']}/3" + RESET)
        map_str = _error_map(rip.get("map"), map_w, theme) if rip.get("map") else ""
        if map_str:
            line += fg(theme["dim"]) + "  MAP " + RESET + map_str
        if text_width(line) <= inner:
            return line
        if map_str:
            # 中等宽度：保留错误/坏道 map，省掉阶段名。
            mid = (progress + speed
                   + _error_tag(errs, theme)
                   + fg(theme["dim"]) + f" retry {errs['e']}/3  MAP " + RESET
                   + map_str)
            if text_width(mid) <= inner:
                return mid

    line = (progress + " " + _phase_tag(phase, stage, full_phase, theme) + speed
            + _error_tag(errs, theme)
            + fg(theme["dim"]) + "  BUF " + RESET
            + "▐" + cache_bar(min(1.0, lead / PREROLL_S), water_w, theme,
                              low=buffering) + "▌"
            + fg(theme["text"]) + f" {lead:4.1f}/{PREROLL_S:.1f}s" + RESET)
    if buffering:
        line += fg(theme["warn"]) + f"  AUTO {remain:3.1f}s" + RESET
    elif lead >= PREROLL_S:
        line += fg(theme["bar"]) + "  READY" + RESET
    if stage == "error":
        retry = fg(theme["dim"]) + f"  retry {errs['e']}/3" + RESET
        if text_width(line + retry) <= inner:
            line += retry
    if text_width(line) <= inner:
        return line

    # 中等宽度：先试保留短阶段名，再退到只保留缓冲水位/倒计时。
    mid_phase = (progress + " " + _phase_tag(phase, stage, full_phase, theme)
                 + speed + _error_tag(errs, theme)
                 + fg(theme["dim"]) + "  buf " + RESET
                 + "▐" + cache_bar(min(1.0, lead / PREROLL_S), water_w, theme,
                                   low=buffering) + "▌"
                 + fg(theme["text"]) + f" {lead:4.1f}/{PREROLL_S:.1f}s" + RESET)
    if buffering:
        mid_phase += fg(theme["warn"]) + f"  AUTO {remain:3.1f}s" + RESET
    if text_width(mid_phase) <= inner:
        return mid_phase

    mid = (progress + speed + _error_tag(errs, theme)
           + fg(theme["dim"]) + "  buf " + RESET
           + "▐" + cache_bar(min(1.0, lead / PREROLL_S), water_w, theme,
                             low=buffering) + "▌"
           + fg(theme["text"]) + f" {lead:4.1f}/{PREROLL_S:.1f}s" + RESET)
    if buffering:
        mid += fg(theme["warn"]) + f"  AUTO {remain:3.1f}s" + RESET
    if text_width(mid) <= inner:
        return mid

    # 窄窗口：保留抓轨进度和错误计数，丢掉阶段/水位细节。
    line = (progress + fg(theme["dim"])
            + f"{float(rip.get('speed', 0.0) or 0.0):4.1f}x  " + RESET
            + _error_tag(errs, theme)
            + fg(theme["dim"]) + f"  retry {errs['e']}  buf {lead:4.1f}s" + RESET)
    return line


# ── 极简布局 ──────────────────────────────────────────────────────────────
def render_mini(st: UIState, w: int, h: int) -> list[str]:
    theme = st.theme
    icon = st.spinner or STATE_ICON.get(st.state, "·")
    state_label = STATE_TEXT.get(st.state, st.state)
    if not st.disc_present:
        icon, state_label = "○", "无光盘"
    vol = int(round(st.volume * 100))
    lines: list[str] = []

    head = (fg(theme["dim"]) + "─ asciicd " + "─" * max(0, w - 11) + RESET)
    lines.append(head)

    left = (fg(theme["accent"]) + f" {st.track_no:02d} " + RESET
            + fg(theme["text"]) + st.title + RESET)
    pulse = ""
    if st.visual_pulse is not None:
        pulse = _pulse_text(st.visual_pulse, theme) + "  "
    right = (fg(theme["text"]) + f"{time_fmt(st.position)} / {time_fmt(st.duration)}  "
             + fg(theme["accent"]) + icon + f" {state_label}  " + RESET
             + pulse
             + fg(theme["dim"]) + f"vol {vol:3d}%  cache {st.cache_lead:4.1f}s " + RESET)
    lines.append(_lr(left, right, w, theme))

    spec = spectrum_line(st.vis.spectrum, max(8, w - 8), theme, st.vis.peaks)
    lines.append(fg(theme["dim"]) + " spec " + RESET + spec)

    lw = max(6, (w - 34) // 2)
    lvl = (fg(theme["dim"]) + "  L " + RESET + meter(st.vis.level_l, lw, theme)
           + fg(theme["text"]) + f" {st.vis.rms_db[0]:5.1f}dB " + RESET
           + fg(theme["dim"]) + " R " + RESET + meter(st.vis.level_r, lw, theme)
           + fg(theme["text"]) + f" {st.vis.rms_db[1]:5.1f}dB " + RESET)
    lines.append(fit(lvl, w))

    if st.hint:
        lines.append(fit(fg(theme["warn"]) + "  " + st.hint + RESET, w))
    cache_txt = (fg(theme["dim"]) + f"   rip {st.rip.get('pct', 0):3.0f}% "
                 f"{st.rip.get('speed', 0.0):4.1f}x  err {_err_count(st.rip)}  "
                 f"buf {st.cache_lead:4.1f}s  " + RESET
                 + fg(theme["dim"]) + f"{st.theme_name}" + RESET)
    lines.append(fit(cache_txt, w))
    return lines


# ── 底框提示 / 频谱柱 ─────────────────────────────────────────────────────
def _bottom_note(w: int, theme, note: str = "", warn: bool = True) -> str:
    """瞬时提示嵌在底框那一行（不新增行、不挤掉内容）。"""
    if not note:
        return _bottom(w, theme)
    text = f" {note} "
    inner = w - 2
    if text_width(text) > inner:
        text = fit(text, inner)
    fill = max(0, inner - text_width(text))
    color = theme["warn"] if warn else theme["dim"]
    return (fg(theme["dim"]) + "└" + fg(color) + text
            + fg(theme["dim"]) + "─" * fill + "┘" + RESET)


def _bars(vals: np.ndarray, width: int, rows: int, theme) -> list[str]:
    """0..1 频谱画成 rows 行字符柱，每列 8 级；上亮下暗，至少留 1 级底噪。"""
    vals = resample(np.asarray(vals, np.float32), width)
    units = max(1, rows * 8)
    heights = np.clip(np.rint(vals * units), 1, units).astype(int)
    out: list[str] = []
    for r in range(rows):
        base_u = (rows - 1 - r) * 8
        depth = 1.0 - 0.26 * (r / max(1, rows - 1))
        parts: list[str] = []
        for i in range(width):
            k = int(heights[i]) - base_u
            if k <= 0:
                parts.append(" ")
                continue
            k = min(8, k)
            color = lerp(theme["spec_lo"], theme["spec_hi"],
                         i / max(1, width - 1))
            if k >= 8:
                ch = "█"
            else:
                ch = BLOCKS[k - 1]
                color = lerp(color, theme["text"], 0.16)
            parts.append(fg((int(color[0] * depth), int(color[1] * depth),
                             int(color[2] * depth))) + ch)
        out.append("".join(parts) + RESET)
    return out


# ── 仪表盘 ────────────────────────────────────────────────────────────────
# 高度不够时整块丢弃的优先级：先丢瀑布，再示波器，最后 LEVEL / CACHE。
# 绝不逐行删：标题和内容必须一起走，否则会留下无标题的波形或孤立标签。
_DASH_DROP_ORDER = ("waterfall", "oscilloscope", "level", "cache")


def _fit_dash_blocks(blocks: list[tuple[str, list[str]]], h: int) -> list[str]:
    """按整块裁剪到 h 行；块都丢完仍超长时截断，保证绝不越界。"""
    kept = list(blocks)
    for name in _DASH_DROP_ORDER:
        if sum(len(lines) for _, lines in kept) <= h:
            break
        kept = [blk for blk in kept if blk[0] != name]
    out = [line for _, lines in kept for line in lines]
    return out[:h]


def _keys_dash(theme, width: int) -> str:
    """按键行：键位嵌进动作词里加下划线（l̲ayout / m̲ini），不用颜色高亮。"""
    d = fg(theme["dim"])

    def u(s: str) -> str:
        return f"{UNDER}{s}{UNDER_OFF}"

    full = ["□ play/pause", u("←/→") + " seek", u("↑/↓") + " vol",
            u("1-9") + " track", u("p") + "rev/" + u("n") + "ext",
            u("s") + "top", u("r") + "eplay",
            u("h") + "elp", u("l") + "ayout", u("m") + "ini",
            u("t") + "heme", u("q") + "uit"]
    compact = ["□ play", u("←/→") + " seek", u("↑/↓") + " vol",
               u("1-9") + " trk", u("p") + "rev/" + u("n") + "ext",
               u("s") + "top", u("r") + "eplay",
               u("h") + "elp", u("l") + "ayout", u("m") + "ini",
               u("t") + "heme", u("q") + "uit"]
    for parts, gap in ((full, "  "), (full, " "), (compact, "  "), (compact, " ")):
        text = gap.join(parts)
        if text_width(text) <= width:
            return d + " " + text
    return d + " " + " ".join(compact)


def _fade_to_bg(theme, key: str, q: float):
    return lerp(theme[key], theme["bg_bar"], max(0.0, min(1.0, q)))


def _pulse_mark(pulse: VisualPulse) -> str:
    """Seek 用 1–3 个方向箭头做短尾迹；暂停/恢复用固定图标。"""
    if pulse.kind == "paused":
        return "‖"
    if pulse.kind == "playing":
        return "▸"
    if pulse.direction == 0:
        return "·"                     # 连续 →← 净位移为 0
    arrow = "«" if pulse.direction < 0 else "»"
    hold = max(0.0, float(pulse.hold))
    if pulse.idle > SEEK_HOLD_GAP_S:
        # 松开后让第 3/第 2 个箭头先后回落，避免整块提示突然消失。
        fade = max(0.0, 1.0 - (pulse.idle - SEEK_HOLD_GAP_S)
                   / max(1e-6, SEEK_HOLD_RELEASE_S))
        hold *= fade
    count = 1
    if hold >= SEEK_ARROW_2_S:
        count = 2
    if hold >= SEEK_ARROW_3_S:
        count = 3
    return arrow * count


def _pulse_text(pulse: VisualPulse, theme) -> str:
    return (bg(theme["accent"]) + fg(theme["bg_bar"]) + BOLD
            + f" {_pulse_mark(pulse)} {pulse.label} " + RESET)


def _pulse_badge(w: int, pulse: VisualPulse, theme) -> str:
    badge = _pulse_text(pulse, theme)
    left = "─ NOW PLAYING "
    fill = max(0, (w - 2) - text_width(left) - text_width(badge))
    return (fg(theme["dim"]) + "├" + fg(theme["accent"]) + left + RESET
            + badge + fg(theme["dim"]) + "─" * fill + "┤" + RESET)


def _track_badge(w: int, tt: TrackTransition, new_no: int, total: int,
                 theme) -> str:
    origin = int(tt.origin_no or tt.old_track_no or new_no)
    new_no = int(new_no or 0)
    label = "PREV" if tt.mode == "prev" else "NEXT"
    total = max(1, int(total or 0), origin, new_no)
    badge = (bg(theme["accent"]) + fg(theme["bg_bar"]) + BOLD
             + f" ▸ {label} {origin:02d}/{total:02d} → "
             + f"{new_no:02d}/{total:02d} " + RESET)
    left = "─ NOW PLAYING "
    fill = max(0, (w - 2) - text_width(left) - text_width(badge))
    return (fg(theme["dim"]) + "├" + fg(theme["accent"]) + left + RESET
            + badge + fg(theme["dim"]) + "─" * fill + "┤" + RESET)


def _track_title_line(theme, no: int, title: str, artist: str,
                      position: float, duration: float, volume: float,
                      q: float, inner: int) -> str:
    c_num = _fade_to_bg(theme, "accent", q)
    c_text = _fade_to_bg(theme, "text", q)
    c_dim = _fade_to_bg(theme, "dim", q)
    left = (fg(c_num) + f" {no:02d} " + RESET
            + fg(c_text) + f"♪ {title}" + RESET)
    if artist:
        left += fg(c_dim) + f"  —  {artist}" + RESET
    right = (fg(c_text) + f"{time_fmt(position)} / {time_fmt(duration)}" + RESET
             + fg(c_dim) + f"   vol {int(volume * 100):3d}%  " + RESET)
    return _lr(left, right, inner, theme)


def _track_progress_line(theme, position: float, duration: float,
                         bar_fade: float, inner: int, low: bool = False) -> str:
    duration = max(0.001, duration)
    frac = max(0.0, min(1.0, position / duration))
    bar_w = max(10, inner - 30)
    fill = int(round(frac * bar_w))
    bar_color = lerp(theme["err"] if low else theme["bar"],
                     theme["bg_bar"], max(0.0, min(1.0, bar_fade)))
    text_color = _fade_to_bg(theme, "text", bar_fade)
    dim_color = _fade_to_bg(theme, "dim", bar_fade)
    return (fg(theme["dim"]) + "▐" + RESET
            + fg(bar_color) + "█" * fill
            + fg(theme["bg_bar"]) + "░" * (bar_w - fill) + RESET
            + fg(theme["dim"]) + "▌" + RESET
            + fg(text_color) + f" {frac * 100:3.0f}%  " + RESET
            + fg(dim_color)
            + f"stay {time_fmt(max(0.0, duration - position))}" + RESET)


def _track_transition_rows(st: UIState, tt: TrackTransition, w: int,
                           inner: int, theme):
    visual = transition_view(
        tt,
        TrackSnapshot(
            number=st.track_no, title=st.title, artist=st.artist,
            position=st.position, duration=st.duration,
        ),
        max(0.0, float(tt.elapsed)),
    )
    frame = visual.track
    low = st.cache_lead < 2.0 and st.state in ("playing", "buffering")
    title_row = _row(_track_title_line(
        theme, frame.number, frame.title or f"Track {frame.number:02d}",
        frame.artist, frame.position, frame.duration, st.volume,
        visual.title_fade, inner), w, theme)
    prog_row = _row(" " + _track_progress_line(
        theme, frame.position, frame.duration, visual.bar_fade, inner,
        low=low), w, theme)
    sep = (_track_badge(w, tt, st.track_no, len(st.tracks), theme)
           if tt.elapsed < TRACK_HINT_S else _sep(w, "NOW PLAYING", theme))
    return sep, title_row, prog_row


def render_dash(st: UIState, w: int, h: int,
                help: bool = False) -> list[str]:
    """25 行仪表盘。默认（help=False）无 KEYS，频谱/瀑布各多一行；
    help=True 是带 KEYS 行的帮助布局。"""
    theme = st.theme
    inner = w - 2
    cells = max(8, inner - 4)
    idle_mix = st.scope_mix
    icon = st.spinner or STATE_ICON.get(st.state, "·")
    blocks: list[tuple[str, list[str]]] = []

    # ── 顶栏 ──
    if st.disc_present:
        state_txt = (fg(theme["accent"])
                     + f" {icon} {STATE_TEXT.get(st.state, st.state)}" + RESET)
    else:
        state_txt = fg(theme["warn"]) + " ○ 无光盘" + RESET
    album = f"  ·  {st.album}" if st.album else ""
    head_left = (state_txt + fg(theme["dim"])
                 + f"   CD-DA {time_fmt(st.total_seconds)} · {len(st.tracks)} tracks" + album + RESET)
    head_right = (fg(theme["dim"]) + f"{st.model[:24]}  {st.device}  {theme_safe(st.theme_name)} " + RESET)
    blocks.append(("top", [_top(w, "asciicd", theme)]))
    blocks.append(("header", [_row(_lr(head_left, head_right, inner, theme), w, theme)]))

    # ── 曲目 + 进度（必需块） ──
    tt = st.track_transition
    if tt is not None and tt.elapsed < TRACK_HINT_S:
        now_sep, now_title, now_prog = _track_transition_rows(
            st, tt, w, inner, theme)
    else:
        low = st.cache_lead < 2.0 and st.state in ("playing", "buffering")
        now_sep = _sep(w, "NOW PLAYING", theme)
        now_title = _row(_track_title_line(
            theme, st.track_no, st.title, st.artist, st.position,
            st.duration, st.volume, 0.0, inner), w, theme)
        now_prog = _row(" " + _track_progress_line(
            theme, st.position, st.duration, 0.0, inner, low=low), w, theme)
        if st.visual_pulse is not None:
            now_sep = _pulse_badge(w, st.visual_pulse, theme)
    blocks.append(("now", [now_sep, now_title, now_prog]))

    # ── 频谱 + 瀑布（瀑布单独成块，可整体丢弃） ──
    spec_w = max(8, inner - 8)
    spec_vals = np.asarray(st.vis.spectrum, np.float32)
    peak_vals = np.asarray(st.vis.peaks, np.float32)
    if idle_mix > 0.001:
        idle_spec, idle_peaks = idle_anim.sweep_spectrum(st.idle_t, cells, spec_w)
        spec_vals = spec_vals * (1.0 - idle_mix) + idle_spec * idle_mix
        peak_vals = peak_vals * (1.0 - idle_mix) + idle_peaks * idle_mix

    spec_rows = [_sep(w, "SPECTRUM · 64 bins · log · 20Hz–17kHz", theme)]
    if not help:
        # 默认布局：没有 KEYS，两行给频谱柱和瀑布各一行
        for r, line in enumerate(_bars(spec_vals, spec_w, 2, theme)):
            top = r == 0
            label = f"{'0dB':>5} " if top else " " * 6
            edge = "┤" if top else "│"
            spec_rows.append(_row(_dim(theme, label) + fg(theme["dim"]) + edge
                                  + RESET + line, w, theme))
    else:
        spec_rows.append(_row(_dim(theme, "  0dB ") + fg(theme["dim"]) + "┤" + RESET
                              + spectrum_line(spec_vals, spec_w, theme, peak_vals),
                              w, theme))
    axis_label = "      " if help else "  -46 "
    spec_rows.append(_row(_dim(theme, axis_label) + fg(theme["dim"]) + "└"
                          + "─" * spec_w + RESET, w, theme))
    blocks.append(("spectrum", spec_rows))

    if h >= 21:
        if help:
            rows = 3 if h >= 23 else 2
        else:
            rows = 4 if h >= 25 else (3 if h >= 23 else 2)
        if idle_mix >= 0.999:
            wf_lines = idle_anim.dotfield_waterfall(st.idle_t, theme, cells,
                                                    spec_w, rows)
            wf_rows = [_row(fg(theme["dim"]) + "       " + line, w, theme)
                       for line in wf_lines]
        else:
            hist = list(np.asarray(st.vis.waterfall)[-rows:])
            while len(hist) < rows:
                hist.insert(0, np.zeros(spec_w, np.float32))
            wf_rows = [_row(_dim(theme, "       ")
                            + waterfall_line(row, spec_w, theme), w, theme)
                       for row in hist]
        blocks.append(("waterfall", wf_rows))

    # ── 示波器（整块可丢：标题和波形一起走） ──
    if h >= 19:
        live = (np.asarray(st.vis.scope_l, np.float32)
                + np.asarray(st.vis.scope_r, np.float32)) / 2.0
        scope_rows = 4 if h < 25 else 6
        if idle_mix <= 0.001:
            scope_lines = braille_scope(live, cells, rows=scope_rows)
        else:
            sine = idle_anim.sine_wave(st.idle_t, cells=cells)
            if idle_mix >= 0.999:
                scope_lines = braille_scope(sine, cells, rows=scope_rows, auto_gain=False)
            else:
                scope_lines = idle_anim.morph_scope(live, sine, idle_mix, cells, scope_rows)
        osc = [_sep(w, "OSCILLOSCOPE", theme)]
        osc += [_row("  " + fg(theme["scope"]) + line + RESET, w, theme)
                for line in scope_lines]
        blocks.append(("oscilloscope", osc))

    # ── 电平 ──
    # 待机时电平跟着淡到 -90 dB；暂停时 idle_mix=0，保持冻结时的读数。
    fade = idle_mix
    db_l = st.vis.rms_db[0] * (1.0 - fade) - 90.0 * fade
    db_r = st.vis.rms_db[1] * (1.0 - fade) - 90.0 * fade
    lv_l = max(0.0, min(1.0, (db_l + 60.0) / 60.0))
    lv_r = max(0.0, min(1.0, (db_r + 60.0) / 60.0))
    lw = max(8, (inner - 42) // 2)
    lvl = (fg(theme["dim"]) + "  L " + RESET + meter(lv_l, lw, theme)
           + fg(theme["text"]) + f" {db_l:5.1f} dB" + RESET
           + fg(theme["dim"]) + "       R " + RESET + meter(lv_r, lw, theme)
           + fg(theme["text"]) + f" {db_r:5.1f} dB" + RESET)
    blocks.append(("level", [
        _sep(w, "LEVEL", theme),
        _row(lvl, w, theme),
    ]))

    # ── 抓轨水位 / 阶段 / 错误 map ──
    cache_line = _cache_line(st, inner, theme)
    blocks.append(("cache", [
        _sep(w, "DISC CACHE · progressive rip", theme),
        _row(cache_line, w, theme),
    ]))

    note = st.message or st.hint
    if help:
        # ── 按键 / 瞬时状态：提示顶掉 KEYS 行，不新增行 ──
        if note:
            blocks.append(("status", [
                _sep(w, "STATUS", theme),
                _row(fg(theme["warn"]) + " " + note + RESET, w, theme),
            ]))
        else:
            blocks.append(("status", [
                _sep(w, "KEYS", theme),
                _row(_keys_dash(theme, inner - 2) + RESET, w, theme),
            ]))
        blocks.append(("bottom", [_bottom(w, theme)]))
    else:
        # 默认布局没有 KEYS 行：有提示就嵌进底框，否则保持普通底线
        blocks.append(("bottom", [_bottom_note(w, theme, note)]))

    # 高度不够时整块丢弃（标题与内容一起走），绝不逐行删
    out = _fit_dash_blocks(blocks, h)
    pad = max(0, (h - len(out)) // 2)
    if pad:
        out = [""] * pad + out
    return out


def render_help(st: UIState, w: int, h: int) -> list[str]:
    """帮助布局：带 KEYS 行（提示顶掉它），频谱/瀑布各少一行。"""
    return render_dash(st, w, h, help=True)


def theme_safe(name: str) -> str:
    return name if name.isascii() else "theme"


def render(st: UIState, w: int, h: int) -> list[str]:
    h = max(1, h)
    if st.help and w >= 66 and h >= 25:
        return render_help(st, w, h)
    if st.minimal or st.layout == "mini" or w < 66 or h < 14:
        return render_mini(st, w, max(h, 5))[:h]
    return render_dash(st, w, h)
