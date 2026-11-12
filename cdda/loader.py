"""开机 TOC 后台加载器：读盘、重试、介质错误分类。

不依赖 App；`--wait-disc` 时读不到可以停在等待状态，由界面读取 `state`。
"""
from __future__ import annotations

import threading
import time

from cdda.device import (CddaError, NoMediaError, UnreadableDiscError,
                         UnsupportedDiscError, query_disc)
from cdda.discmon import TRAY_CLOSED_DISC
from cdda.fake import build_fake_disc
from config import DISC_UNREADABLE_S


class DiscLoader:
    """后台读 TOC；--wait-disc 时读不到就隔一会儿再试，不挡开机动画。

    state:
      reading      首次查询中 / unreadable 宽限重试中（显示 READING）
      waiting      没盘/IO 暂不可用（显示 NO DISC，继续等）
      unsupported  确认有盘但不是 CD-DA（显示 UNSUPPORTED；等换盘，不重跑）
      unreadable   有盘但宽限期后仍认不出（显示 UNREADABLE；等换盘）
      ready        盘已读到
    """

    def __init__(self, args, wait: bool, interval: float = 1.2,
                 tray_probe=None):
        self.args = args
        self.wait = wait
        self.interval = interval
        self.tray_probe = tray_probe
        self.state = "idle"
        self.disc = None
        self.error: Exception | None = None
        self.unsupported: UnsupportedDiscError | None = None
        self.unreadable: UnreadableDiscError | None = None
        self._unreadable_since: float | None = None
        self.crash: Exception | None = None
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name="boot-toc", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def finished(self) -> bool:
        return self._finished.is_set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.wait and self.tray_probe is not None:
                # --wait-disc 时托盘没关好就别跑 cdparanoia；既能避免在
                # 开仓状态阻塞，也能让数据盘换盘后的等待更安静。
                tray = self.tray_probe()
                if tray is not None and tray != TRAY_CLOSED_DISC:
                    self.error = None
                    self.unsupported = None
                    self.unreadable = None
                    self._unreadable_since = None
                    self.state = "waiting"
                    self._stop.wait(self.interval)
                    continue
            # 真正发起 query_disc 前才切 READING：重试时 cdparanoia 可能
            # 阻塞几秒，不能让这段时间把上一轮的 NO DISC 状态留在屏幕上。
            self.state = "reading"
            try:
                disc = (build_fake_disc() if self.args.fake
                        else query_disc(self.args.device))
            except UnsupportedDiscError as exc:
                self.error = exc
                self.unsupported = exc
                self.unreadable = None
                self._unreadable_since = None
                self.state = "unsupported"
                if not self.wait:
                    self._finished.set()
                    return
                self._wait_for_tray_change()
                continue
            except UnreadableDiscError as exc:
                now = time.monotonic()
                if self._unreadable_since is None:
                    self._unreadable_since = now
                self.error = exc
                self.unsupported = None
                if not self.wait:
                    self.unreadable = exc
                    self.state = "unreadable"
                    self._finished.set()
                    return
                if now - self._unreadable_since < DISC_UNREADABLE_S:
                    self.unreadable = None
                    self.state = "reading"   # 先给光驱转起来/重读 TOC 的宽限
                    self._stop.wait(self.interval)
                    continue
                self.unreadable = exc
                self.state = "unreadable"
                self._wait_for_tray_change()
                continue
            except NoMediaError as exc:
                self.error = exc
                self.unsupported = None
                self.unreadable = None
                self._unreadable_since = None
                self.state = "waiting"
                if not self.wait:
                    self._finished.set()
                    return
                self._stop.wait(self.interval)
                continue
            except CddaError as exc:
                # 不是 NoMediaError：盘还在，只是暂时读不出。启动画面
                # 继续显示 READING，不能假装 NO DISC。
                self.error = exc
                self.unsupported = None
                self.unreadable = None
                self.state = "reading"
                if not self.wait:
                    self._finished.set()
                    return
                self._stop.wait(self.interval)
                continue
            except Exception as exc:          # noqa: BLE001 - 线程里不能静默死掉
                self.crash = exc
                self._finished.set()
                return
            self.disc = disc
            self.error = None
            self.unsupported = None
            self.unreadable = None
            self._unreadable_since = None
            self.state = "ready"
            self._finished.set()
            return

    def _wait_for_tray_change(self) -> None:
        """数据盘留在仓里时不要每 1.2s 重跑 cdparanoia；等托盘动过再试。"""
        probe = self.tray_probe
        if probe is None:                 # 测试/非 Linux：退回定时重试
            self._stop.wait(self.interval)
            return
        while not self._stop.is_set():
            tray = probe()
            if tray is None:
                # 读不到托盘状态（非 Linux/驱动不支持）：不能无限等，
                # 退回定时重试；Linux 上正常读数据盘不会走到这里。
                self._stop.wait(self.interval)
                return
            if tray != TRAY_CLOSED_DISC:  # open / closed-empty
                self.error = None
                self.unsupported = None
                self.unreadable = None
                self._unreadable_since = None
                self.state = "waiting"
                self._stop.wait(self.interval)
                return
            self._stop.wait(0.1)

