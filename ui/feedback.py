"""短暂视觉反馈的协调层：只记录并采样，不碰播放/抓轨/托盘资源。

优先级（高 → 低，冲突时高者取消低者，最新业务状态永远是真值）：

1. 弹仓 / 换盘 / 不可播放介质：``reset()`` 清掉全部曲目动画和提示；
2. Stop：同样 ``reset()``，随后交给停止态待机；
3. Seek / Pause / Resume：``show_pulse()`` 取消旧切轨动画，只留 0.13–0.22 s
   的即时提示；
4. 切轨 / 自动 EOF：``begin_track`` / ``retarget_track``，连续操作时保留
   累计起点用于徽标；重定向从 ``visible_source`` 采到的当前屏幕帧接续，
   进度、标题和透明度不跳变；
5. 缓冲、频谱、待机等持续动画：不经过这里，也不允许回写这里的状态。

所有时间都是 ``time.monotonic()`` 的绝对值，``sample(now)`` 只做纯采样；
调用方可以传入可控时钟，测试不需要 sleep。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from config import (SEEK_HOLD_GAP_S, SEEK_HOLD_MAX_S, SEEK_HOLD_STEP_S,
                    SEEK_HOLD_TAIL_S, TRACK_HINT_S, TRACK_TRANSITION_S)


@dataclass(frozen=True)
class TrackSnapshot:
    """切轨发生前的一帧业务状态；只供表现层做退场，不回写播放器。"""
    number: int
    title: str = ""
    artist: str = ""
    position: float = 0.0
    duration: float = 0.0


@dataclass(frozen=True)
class TrackVisual:
    """某一刻屏幕上实际可见的曲目帧，供重定向时无缝接续。"""
    track: TrackSnapshot
    title_fade: float = 0.0        # 0 = 全亮，1 = 完全退到背景色
    bar_fade: float = 0.0


@dataclass
class TrackTransition:
    """一次切轨的可见退场；elapsed 由 VisualFeedback 每帧采样。"""
    mode: str = "next"             # auto / next / prev
    origin_no: int = 0             # 本轮连续操作起点，用于徽标
    old_track_no: int = 0
    old_title: str = ""
    old_artist: str = ""
    old_position: float = 0.0
    old_duration: float = 0.0
    source_title_fade: float = 0.0  # 重定向时当前标题的淡出程度
    source_bar_fade: float = 0.0    # 重定向时当前进度条的淡出程度
    since: float = 0.0
    elapsed: float = 0.0


def transition_view(tt: TrackTransition, current: TrackSnapshot,
                    elapsed: float) -> TrackVisual:
    """把一次切轨采样成「此刻屏幕实际显示」的一帧。

    这里集中退场插值，App 重定向和 layouts 渲染共用同一套结果，
    避免两处公式漂移；只读快照，不改业务播放头。
    """
    fade = max(0.0, min(1.0, float(elapsed) / TRACK_TRANSITION_S))
    q = min(1.0, fade * 2.0)
    title_start = max(0.0, min(1.0, float(tt.source_title_fade)))
    bar_start = max(0.0, min(1.0, float(tt.source_bar_fade)))

    if fade < 0.5:
        duration = max(0.001, float(tt.old_duration or 0.0))
        position = max(0.0, min(duration, float(tt.old_position or 0.0)))
        mode = tt.mode if tt.mode in ("auto", "next", "prev") else "next"
        if mode == "auto":
            position = duration
        elif mode == "prev":
            position *= 1.0 - q
        else:
            position += (duration - position) * q
        title_fade = title_start + (1.0 - title_start) * q
        if mode == "auto":
            bar_fade = bar_start + (1.0 - bar_start) * q
        else:
            bar_fade = bar_start * (1.0 - q)
        return TrackVisual(
            TrackSnapshot(
                number=int(tt.old_track_no or 0),
                title=tt.old_title,
                artist=tt.old_artist,
                position=position,
                duration=duration,
            ),
            title_fade,
            bar_fade,
        )

    # 后半段显示业务层最新轨；标题从背景色淡入，进度条始终按真实位置。
    return TrackVisual(
        TrackSnapshot(
            number=int(current.number or 0),
            title=current.title,
            artist=current.artist,
            position=max(0.0, float(current.position)),
            duration=max(0.0, float(current.duration)),
        ),
        1.0 - (fade - 0.5) * 2.0,
        0.0,
    )


@dataclass
class VisualPulse:
    """Seek / Pause / Resume 的短提示；不参与进度插值。"""
    kind: str = "seek"             # seek / paused / playing
    label: str = ""
    since: float = 0.0
    duration: float = 0.22
    direction: int = 0             # seek: -1 后退 / +1 前进 / 0 净位移为 0
    elapsed: float = 0.0
    hold: float = 0.0              # 长按累计时长（秒），驱动 1→3 个箭头
    last_input_at: float = 0.0     # 最近一次 Seek 按键的 monotonic 时间
    idle: float = 0.0              # 距最近一次按键的静默时长（sample 更新）


class VisualFeedback:
    """App 的视觉反馈状态机；单一所有者，避免过期标志互相覆盖。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.transition: TrackTransition | None = None
        self.pulse: VisualPulse | None = None

    def _at(self, now: float | None) -> float:
        return self.clock() if now is None else float(now)

    def begin_track(self, source: TrackSnapshot, target_no: int,
                    mode: str, now: float | None = None, *,
                    source_title_fade: float = 0.0,
                    source_bar_fade: float = 0.0,
                    business_from_no: int | None = None
                    ) -> TrackTransition | None:
        """开始或重定向一次切轨。

        ``source`` 应是调用方通过 :meth:`visible_source` 采到的当前可见帧，
        ``business_from_no`` 是本次命令前的业务轨号。方向与徽标以业务切换
        为准；视觉起点仍用可见帧。快速往返时可见源可能恰好等于目标，
        这时允许做一次「排空/回落到 0%」的 settle，而不是直接跳帧。
        ``origin_no`` 保留同一累计方向的连续操作起点；方向反转时从本次
        业务起点重新起算，避免 NEXT/PREV 与动画方向矛盾。
        """
        now = self._at(now)
        base = int(source.number if business_from_no is None
                   else business_from_no)
        if mode not in ("auto", "next", "prev"):
            self.transition = None
            self.pulse = None
            return None
        # 真正的同轨重选没有业务位移，不启动过渡；但 07→02 时屏幕可能
        # 还停在 02 的 75%，这种来回要允许连续排空到 0%。
        if target_no == source.number and base == target_no:
            self.transition = None
            self.pulse = None
            return None

        origin = base
        previous = self.transition
        if (previous is not None
                and now - previous.since < TRACK_HINT_S):
            origin = int(previous.origin_no or previous.old_track_no or origin)
            direction = 1 if mode in ("auto", "next") else -1
            if target_no != origin and (target_no - origin) * direction < 0:
                origin = base
        # 快速绕回本轮起点时，继续用起点→起点会得到无意义的 02→02 徽标；
        # 改为从本次业务起点重新起算，仍然不排队旧动画。
        if target_no == origin:
            origin = base

        self.transition = TrackTransition(
            mode=mode,
            origin_no=origin,
            old_track_no=source.number,
            old_title=source.title,
            old_artist=source.artist,
            old_position=max(0.0, float(source.position)),
            old_duration=max(0.0, float(source.duration)),
            source_title_fade=max(0.0, min(1.0, source_title_fade)),
            source_bar_fade=max(0.0, min(1.0, source_bar_fade)),
            since=now,
        )
        self.pulse = None                     # 新切轨立即盖掉旧 Seek/Pause 提示
        return self.transition

    def retarget_track(self, source: TrackSnapshot, target_no: int,
                       mode: str, now: float | None = None, *,
                       source_title_fade: float = 0.0,
                       source_bar_fade: float = 0.0,
                       business_from_no: int | None = None
                       ) -> TrackTransition | None:
        """``begin_track`` 的显式重定向入口；语义相同，便于调用点自解释。"""
        return self.begin_track(
            source, target_no, mode, now=now,
            source_title_fade=source_title_fade,
            source_bar_fade=source_bar_fade,
            business_from_no=business_from_no,
        )

    def visible_source(self, current: TrackSnapshot,
                       now: float | None = None) -> TrackVisual:
        """当前屏幕实际可见的曲目帧；没有活动过渡时就是业务当前帧。"""
        now = self._at(now)
        tt = self.transition
        if tt is None or now - tt.since >= TRACK_HINT_S:
            return TrackVisual(current)
        return transition_view(tt, current, max(0.0, now - tt.since))

    def cancel_track(self) -> None:
        self.transition = None

    def show_pulse(self, kind: str, label: str, *, duration: float,
                   direction: int = 0, now: float | None = None) -> VisualPulse:
        """取消切轨并显示一个短提示；Seek/Pause 用，不插值播放头。

        单次 Seek 用 :meth:`seek_pulse`，它能跨重复键累积长按状态；
        这个方法仍用于 Pause/Resume 和显式短提示。
        """
        self.transition = None
        self.pulse = VisualPulse(
            kind=kind, label=label, since=self._at(now),
            duration=max(0.01, float(duration)), direction=direction,
        )
        return self.pulse

    def seek_pulse(self, label: str, direction: int, *, now: float | None = None,
                   duration: float = 0.22) -> VisualPulse:
        """更新 Seek 提示并累积长按时长。

        同一轮连续按键复用同一个 pulse：短按只有 1 个箭头；重复键在
        ``SEEK_HOLD_GAP_S`` 内到达时累计 hold，渲染层按 1→2→3 增长。
        超过 gap 再按视为新一轮短按，箭头回到 1。
        """
        now = self._at(now)
        pulse = self.pulse
        self.transition = None
        if pulse is not None and pulse.kind == "seek":
            last = pulse.last_input_at or pulse.since
            gap = max(0.0, now - last)
            if gap <= SEEK_HOLD_GAP_S:
                pulse.hold = min(
                    SEEK_HOLD_MAX_S,
                    pulse.hold + min(gap, SEEK_HOLD_STEP_S),
                )
            else:
                pulse.hold = 0.0
                pulse.since = now
            pulse.label = label
            pulse.direction = direction
            pulse.last_input_at = now
            pulse.idle = 0.0
            return pulse
        self.pulse = VisualPulse(
            kind="seek", label=label, since=now,
            duration=max(0.01, float(duration)), direction=direction,
            last_input_at=now,
        )
        return self.pulse

    def reset(self) -> None:
        """Eject / 换盘 / Stop：最高优先级清理，全部瞬态归零。"""
        self.transition = None
        self.pulse = None

    def sample(self, now: float | None = None) -> None:
        """按绝对时间更新 elapsed 并清掉过期项；不创建新动画。"""
        now = self._at(now)
        trans = self.transition
        if trans is not None:
            trans.elapsed = max(0.0, now - trans.since)
            if trans.elapsed >= TRACK_HINT_S - 1e-9:
                self.transition = None
        pulse = self.pulse
        if pulse is not None:
            pulse.elapsed = max(0.0, now - pulse.since)
            if pulse.kind == "seek" and pulse.hold > 0.0:
                # 长按提示按“最后一次按键”续命；松开后先让箭头回落，
                # 再保留一小段 1 箭头提示，最后才消失。
                last = pulse.last_input_at or pulse.since
                pulse.idle = max(0.0, now - last)
                if pulse.idle > SEEK_HOLD_GAP_S + SEEK_HOLD_TAIL_S:
                    self.pulse = None
            elif pulse.elapsed >= pulse.duration - 1e-9:
                self.pulse = None
