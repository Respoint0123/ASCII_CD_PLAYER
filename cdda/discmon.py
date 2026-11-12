"""盘片存在探测与换盘监视。

为什么不用 `cdparanoia -Q` 轮询：它要打开光驱读 TOC，会打断正在进行的抓轨。
这里用 Linux 的 `CDROM_DRIVE_STATUS` ioctl：只读托盘状态，开销极小，可和抓轨
进程同时用。只有「从无盘变成有盘」时才读一次 TOC。

已知限制：如果一次 closed-disc → closed-disc 的快速换盘完全落在两次轮询之间，
没有 open / closed-empty 边沿，`CDROM_DRIVE_STATUS` 看不到变化；要覆盖这种
情况需要内核 media-change 事件或额外重新探测。

探测函数和读盘函数都可注入，测试不需要真实光驱。
"""
from __future__ import annotations

import fcntl
import os
import threading
import time

from cdda.device import (CddaError, Disc, UnreadableDiscError,
                          UnsupportedDiscError)
from config import DISC_UNREADABLE_S

CDROM_DRIVE_STATUS = 0x5326
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# 托盘三态：open=托盘弹出；closed-empty=托盘关着但没盘；closed-disc=托盘关着且有盘。
# None = 判断不了（设备不存在/无权限/ioctl 不支持/驱动器正在过渡）。
TRAY_OPEN = "open"
TRAY_CLOSED_EMPTY = "closed-empty"
TRAY_CLOSED_DISC = "closed-disc"
_TRAY_STATES = (TRAY_OPEN, TRAY_CLOSED_EMPTY, TRAY_CLOSED_DISC)


def drive_tray_state(device: str) -> str | None:
    """读取托盘三态；None 表示未知或过渡态，调用方应维持现状。"""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        status = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return {
        CDS_TRAY_OPEN: TRAY_OPEN,
        CDS_NO_DISC: TRAY_CLOSED_EMPTY,
        CDS_DISC_OK: TRAY_CLOSED_DISC,
    }.get(status)


def drive_has_disc(device: str) -> bool | None:
    """True=关仓且有盘，False=托盘开着/关仓无盘，None=判断不了。

    `CDS_DRIVE_NOT_READY` 是托盘动作过程中的过渡态，归入 None：这时不能
    误报「弹盘」，否则开合仓动画会被打断。
    """
    tray = drive_tray_state(device)
    if tray is None:
        return None
    return tray == TRAY_CLOSED_DISC


def _normalize_probe(value: bool | str | None) -> str | None:
    """把探测函数的返回值归一成托盘三态。

    兼容旧的 bool 探测函数：True=关仓有盘，False=关仓无盘。未知字符串按
    未知处理，绝不把探测器的笔误当成弹盘。
    """
    if value is None or isinstance(value, str):
        return value if value in _TRAY_STATES else None
    return TRAY_CLOSED_DISC if value else TRAY_CLOSED_EMPTY


class DiscMonitor(threading.Thread):
    """后台轮询光驱：记录托盘三态与读出的 Disc。

    `poll_once()` 同步跑一轮（测试直接调用）；`run()` 按 interval 循环。
    snapshot() 返回 (present, disc)：
        (None, None)  状态未知，调用方应维持现状
        (False, None) 无盘/托盘开着
        (True, None)  检测到盘但 TOC 还没读到（查询中/下一轮重试），或用
                      error_snapshot() 读到 Unsupported/Unreadable 错误
        (True, Disc)  盘就绪
    tray_snapshot() 额外给出 (tray, since, disc)：since 是该托盘状态最近一次
    变化的 time.monotonic()，给开合仓/换盘动画当起始时间。
    error_snapshot() 返回最近一次确定的异常：UnsupportedDiscError 立即
    sticky；UnreadableDiscError 在 DISC_UNREADABLE_S 宽限后 sticky。托盘
    边沿会清掉它们。暂时性的 CddaError 不记录，下一轮继续读 TOC。
    """

    def __init__(self, probe_present, read_disc, interval: float = 1.5):
        super().__init__(name="disc-monitor", daemon=True)
        self._probe_present = probe_present
        self._read_disc = read_disc
        self.interval = interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._tray: str | None = None
        self._since = 0.0
        self._disc: Disc | None = None
        self._error: (UnsupportedDiscError | UnreadableDiscError | None) = None
        self._unreadable_since: float | None = None

    def prime(self, present: bool | str | None, disc: Disc | None) -> None:
        """启动时告诉监视器当前状态，避免立刻重读一次 TOC。"""
        tray = _normalize_probe(present)
        with self._lock:
            self._tray = tray
            self._disc = disc if tray == TRAY_CLOSED_DISC else None
            self._error = None
            self._unreadable_since = None
            self._since = time.monotonic()

    def snapshot(self) -> tuple[bool | None, Disc | None]:
        with self._lock:
            present = (None if self._tray is None
                       else self._tray == TRAY_CLOSED_DISC)
            return present, self._disc

    def tray_snapshot(self) -> tuple[str | None, float, Disc | None]:
        with self._lock:
            return self._tray, self._since, self._disc

    def error_snapshot(self) -> (UnsupportedDiscError
                                 | UnreadableDiscError | None):
        with self._lock:
            return self._error

    def _set(self, tray: str, disc: Disc | None,
             error: (UnsupportedDiscError | UnreadableDiscError | None) = None
             ) -> None:
        with self._lock:
            if tray != self._tray:
                self._since = time.monotonic()
            self._tray, self._disc, self._error = tray, disc, error

    def poll_once(self) -> None:
        tray = _normalize_probe(self._probe_present())
        if tray is None:
            return                       # 判断不了/过渡态：维持现状，不能误报弹盘
        if tray != TRAY_CLOSED_DISC:
            with self._lock:
                self._unreadable_since = None
            self._set(tray, None)        # 托盘边沿清掉错误，换盘后重新判断
            return
        with self._lock:
            if self._tray == TRAY_CLOSED_DISC and (
                    self._disc is not None
                    or isinstance(self._error,
                                  (UnsupportedDiscError, UnreadableDiscError))):
                # 已就绪，或已确认这张盘不支持/读不出：都别反复读 TOC。
                # 后者要等 open/closed-empty 边沿清掉错误再重试。
                return
        # 先发布「关仓、查询中」快照再做阻塞 query：主线程才能在真正读
        # TOC 的几秒里进入 CHECKING 并画 LEAD-IN。托盘探测已经确认
        # closed-disc，这里不会把未知/过渡态误报成已关仓。
        self._set(TRAY_CLOSED_DISC, None)
        try:
            disc = self._read_disc()
        except UnsupportedDiscError as exc:
            with self._lock:
                self._unreadable_since = None
            self._set(TRAY_CLOSED_DISC, None, exc)
            return
        except UnreadableDiscError as exc:
            now = time.monotonic()
            with self._lock:
                if self._unreadable_since is None:
                    self._unreadable_since = now
                since = self._unreadable_since
            if now - since >= DISC_UNREADABLE_S:
                # 宽限期过了还认不出：停在 UNREADABLE，不再无限 CHECKING。
                self._set(TRAY_CLOSED_DISC, None, exc)
            else:
                self._set(TRAY_CLOSED_DISC, None)
            return
        except CddaError:
            disc = None                  # 刚插盘还没转起来：下一轮再读
        else:
            with self._lock:
                self._unreadable_since = None
        self._set(TRAY_CLOSED_DISC, disc)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:            # 监视线程不能带崩主程序
                pass
            self._stop_event.wait(self.interval)
