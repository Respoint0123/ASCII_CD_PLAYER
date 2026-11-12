"""App 的托盘/装载视图状态。

把 `tray_ui`、`disc_present`、`disc_state`、`_last_disc` 等互相牵连的字段
收进一个对象，并通过 `enter()` 一次性更新；渲染层只读快照，不反向修改
资源生命周期。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TrayPhase(str, Enum):
    MAIN = ""
    OPEN = "open"
    CLOSED_EMPTY = "closed_empty"
    CHECKING = "checking"
    READING = "reading"
    READY = "ready"
    RETURN = "return"
    UNSUPPORTED = "unsupported"
    UNREADABLE = "unreadable"


_UNSET = object()


@dataclass
class TrayState:
    tray: str = ""                  # open / closed-empty / closed-disc / ""
    since: float = 0.0              # 当前托盘状态起点（monotonic）
    phase: TrayPhase = TrayPhase.MAIN
    disc_present: bool = False      # 当前是否已装好一张可播放的盘
    disc_state: str = "no_disc"     # no_disc / checking / ready / tray_open / ...
    last_disc: Any = None           # 开仓前的盘，关仓后比对 discid
    transition: dict | None = None
    ready_at: float = 0.0

    @property
    def ui(self) -> str:
        return self.phase.value

    def set_tray(self, tray: str, since: float | None = None) -> bool:
        """更新托盘三态；返回是否发生边沿。"""
        if tray == self.tray:
            return False
        self.tray = tray
        self.since = time.monotonic() if since is None else since
        return True

    def enter(self, phase: TrayPhase | str, *, present: bool, state: str,
              ready_at: float = 0.0, transition=_UNSET,
              last_disc=_UNSET) -> None:
        """唯一的状态更新入口：phase 与它依赖的字段一起改。"""
        self.phase = TrayPhase(phase)
        self.disc_present = present
        self.disc_state = state
        self.ready_at = ready_at
        if transition is not _UNSET:
            self.transition = transition
        if last_disc is not _UNSET:
            self.last_disc = last_disc

    def enter_main(self) -> None:
        self.phase = TrayPhase.MAIN
        self.transition = None
        self.ready_at = 0.0
