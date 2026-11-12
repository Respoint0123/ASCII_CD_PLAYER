"""一张光盘的资源会话：Disc + Cache + Ripper + 元数据查询的生命周期。

`App` 只负责协调交互；本对象负责「当前这张盘」的磁盘、抓轨线程和后台
元数据任务。换盘时旧会话被 retire，新会话用新的 disc/cache/ripper 构造，
因此旧的后台结果不会写进新盘的缓存或界面。
"""
from __future__ import annotations

import threading
from typing import Callable

from cdda import cdtext
from cdda.cache import Cache
from cdda.device import Disc, toc_matches
from cdda.ripper import RipperBase
from services import metadata


def merge_meta(base: dict, extra: dict) -> dict:
    """把一份元数据合进另一份：extra 的非空字段优先，sources 累积。

    顺序是先 CD-TEXT、再 MusicBrainz —— 盘上自带文字兜底，联网结果更准。
    """
    out = dict(base)
    for key in ("album", "artist", "release_id", "match", "disc_id"):
        if extra.get(key):
            out[key] = extra[key]
    tracks = dict(base.get("tracks") or {})
    for number, title in (extra.get("tracks") or {}).items():
        if title:
            tracks[str(number)] = title
    if tracks:
        out["tracks"] = tracks
    if extra.get("source"):
        out["sources"] = sorted(set(out.get("sources") or []) | {extra["source"]})
    return out


class DiscSession:
    """当前盘的资源与代数（generation）。

    - `seq`：每次 prepare 递增；后台任务用它在读盘前后核对会话是否已换。
    - `started`：抓轨线程是否已按播放请求启动；线程只能 start 一次。
    - `metadata_seq`：元数据查询已经为哪一代会话启动过，避免重复查询。
    """

    def __init__(self, args=None, *,
                 metadata_module=metadata,
                 cdtext_module=cdtext,
                 toc_matcher: Callable[[str, Disc], bool] | None = None):
        self.args = args
        self.disc: Disc | None = None
        self.cache: Cache | None = None
        self.ripper: RipperBase | None = None
        self.seq = 0
        self.started = False
        self.metadata_seq = -1
        self.metadata_thread: threading.Thread | None = None
        self._metadata = metadata_module
        self._cdtext = cdtext_module
        self._toc_matches = toc_matcher or toc_matches
        self._lock = threading.RLock()

    @property
    def ready(self) -> bool:
        return bool(self.disc and self.cache and self.ripper)

    def prepare(self, disc: Disc, cache: Cache, ripper: RipperBase) -> None:
        """装入一张新盘的资源；旧线程必须由调用方先 retire。"""
        with self._lock:
            self.disc = disc
            self.cache = cache
            self.ripper = ripper
            self.seq += 1
            self.started = False
            self.metadata_seq = -1
            self.metadata_thread = None

    def cached_count(self) -> int:
        if not (self.disc and self.cache):
            return 0
        return sum(1 for track in self.disc.tracks
                   if self.cache.is_done(self.disc, track.number))

    def ensure_ripper(self, track_no: int) -> bool:
        """第一次开播时启动抓轨线程；重复调用安全。"""
        if not self.ready or self.ripper is None:
            return False
        with self._lock:
            if not self.started:
                # 先告诉抓轨器当前轨，再 start，避免线程一上来先抓第一轨。
                self.ripper.want(track_no)
                self.ripper.start()
                self.started = True
        return True

    def select_track(self, number: int, immediate: bool = False) -> None:
        """用户主动选轨：清失败记录；已启动时请求抓轨优先级。

        默认经 Ripper 的 300ms 防抖；首播/自动 EOF/已缓存轨由调用方传
        ``immediate=True``，不增加机械等待。
        """
        if self.ripper is None:
            return
        self.ripper.retry(number)
        if self.started:
            self.ripper.request_priority(number, immediate=immediate)

    def claim_metadata(self) -> bool:
        """为当前代会话领取一次元数据查询；返回 False = 已经领过。"""
        with self._lock:
            if self.metadata_seq == self.seq:
                return False
            self.metadata_seq = self.seq
            return True

    def start_metadata(self) -> None:
        """后台补元数据：先联网查 MusicBrainz，查不到再读 CD-TEXT 兜底。

        查不到/没网就静默跳过，绝不挡播放。worker 捕获发起时的 disc/cache，
        换盘后结果仍写回旧盘缓存，但 CD-TEXT 读盘前后会核对会话代数。
        """
        if not (self.disc and self.cache):
            return
        if self.metadata_thread is not None and self.metadata_thread.is_alive():
            return
        disc, cache = self.disc, self.cache
        seq = self.seq
        meta = cache.read_meta(disc)
        sources = set(meta.get("sources") or [])
        complete = len(meta.get("tracks") or {}) >= len(disc.tracks)
        # TOC 模糊匹配的结果过一段时间重查一次（精确命中不重查）
        recheck = self._metadata.toc_result_needs_recheck(meta)
        want_mb = (not getattr(self.args, "no_metadata", False)
                   and ("musicbrainz" not in sources or not complete or recheck))
        want_cdtext = "cdtext" not in sources and not complete
        if not want_mb and not want_cdtext:
            return

        def worker() -> None:
            base = dict(cache.read_meta(disc))
            data = None
            if want_mb:
                try:
                    data = self._metadata.fetch(disc)
                except Exception:
                    data = None
            # 有网时 MB 先到就先显示；只有查不到/查不全才去读盘上的 CD-TEXT
            # （有些光驱读一次 lead-in 要 1~2 秒，能省则省）。
            text = None
            if want_cdtext and (data is None
                                or len(data.get("tracks") or {}) < len(disc.tracks)):
                text = self._read_cdtext(disc, seq)
            if text:
                base = merge_meta(base, text)
            if data:
                base = merge_meta(base, data)        # MusicBrainz 优先
            if data or text:
                try:
                    cache.write_meta(disc, base)
                except OSError:
                    pass

        thread = threading.Thread(target=worker, name="metadata", daemon=True)
        self.metadata_thread = thread
        thread.start()

    def _read_cdtext(self, disc: Disc, seq: int) -> dict | None:
        """读 CD-TEXT，但先确认没换盘；读之前/之后对不上就把结果丢掉。

        网查可能要几秒，这期间用户可能把 A 碟换成 B 碟。cdtext.read 读的是
        「设备当前那张盘」，所以读之前看会话代数，读完之后再比一次 TOC ——
        只比设备路径没用，路径一直没变。
        """
        if self.seq != seq or self.disc is not disc:
            return None
        text = self._cdtext.read(disc.device)
        if text and (self.seq != seq
                     or not self._toc_matches(disc.device, disc)):
            return None
        return text

    def retire(self, preserve_part: bool = False,
               timeout: float | None = 10.0) -> bool:
        """停掉抓轨线程并等它退出；返回 False = 旧写者还没退场。

        未 start 的线程也能安全 retire；只有成功退出才清掉 started，
        防止 App 在旧写者仍活着时启动新 ripper 写同一个 PCM 文件。
        """
        ripper = self.ripper
        if ripper is None:
            self.started = False
            return True
        kwargs = {"preserve_part": preserve_part}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if ripper.stop_and_join(**kwargs):
            self.started = False
            return True
        return False

    def clear_audio(self) -> None:
        if self.cache is None or self.disc is None:
            return
        self.cache.clear_audio(self.disc)
