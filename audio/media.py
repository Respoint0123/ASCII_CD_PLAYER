"""把「缓存里正在变长的 PCM 文件」包装成可阻塞读取的音源。"""
from __future__ import annotations

import threading
import time

from cdda.cache import Cache
from cdda.device import Disc, Track
from cdda.ripper import RipperBase


class FeedAbort(Exception):
    """播放端主动中断（seek / 换轨 / 退出）。"""


class MediaFailed(Exception):
    """抓轨最终失败：这一轨没有可用音频。

    必须和「正常播到轨尾」区分开 —— 调用方（engine/app）不能把 b"" 当 EOF
    直接自动下一轨，否则坏盘会静默跳轨。
    """


class MediaStalled(Exception):
    """抓轨长时间没有任何进展。"""


class TrackMedia:
    def __init__(self, cache: Cache, disc: Disc, track: Track, ripper: RipperBase,
                 stall_timeout: float = 90.0):
        self.cache = cache
        self.disc = disc
        self.track = track
        self.ripper = ripper
        self.stall_timeout = stall_timeout

    @property
    def total_bytes(self) -> int:
        return self.track.pcm_bytes

    def available(self) -> int:
        return min(self.cache.available_bytes(self.disc, self.track.number), self.total_bytes)

    def _resolve(self):
        """返回 (path, size)；文件正在创建/刚被 rename 时返回 (None, None)。

        size 截断到 TOC 算出的轨长：.part 收尾前可能比轨长多写一点，播放端
        只认轨内字节，超出的部分不能喂给音频输出。
        """
        path = self.cache.readable_path(self.disc, self.track.number)
        if path is None:
            return None, None
        try:
            size = path.stat().st_size
        except OSError:
            return None, None
        return path, min(size, self.total_bytes)

    def read(self, offset: int, nbytes: int, abort: threading.Event) -> bytes:
        """阻塞式读取：数据还没抓到时原地等待。

        - 正常播到轨尾 → 返回 b""
        - 这一轨被标记抓轨失败 → 抛 MediaFailed（不能当 EOF 用）
        - 长时间没有任何进展 → 抛 MediaStalled
        """
        last_size = -1
        last_growth = time.monotonic()
        while True:
            if abort.is_set():
                raise FeedAbort()
            path, size = self._resolve()
            if size is not None:
                if size != last_size:
                    last_size = size
                    last_growth = time.monotonic()
                if offset < size:
                    try:
                        with path.open("rb") as fh:      # type: ignore[union-attr]
                            fh.seek(offset)
                            data = fh.read(min(nbytes, size - offset))
                    except OSError:
                        data = b""
                    if data:
                        return data
            # 抓轨已被判死刑：没数据了就明确报错，别装成正常轨尾
            if self.ripper.is_failed(self.track.number):
                reason = self.ripper.failure_reason(self.track.number)
                detail = f"：{reason}" if reason else ""
                raise MediaFailed(f"第 {self.track.number} 轨抓轨失败{detail}")
            # 抓轨已到终态：再确认一次是否真的没数据了（避开 rename 瞬间）
            if self.ripper.is_done(self.track.number):
                _, final_size = self._resolve()
                if final_size is not None and final_size > offset:
                    continue
                return b""
            if time.monotonic() - last_growth > self.stall_timeout:
                raise MediaStalled(f"第 {self.track.number} 轨抓轨停滞")
            time.sleep(0.01)
