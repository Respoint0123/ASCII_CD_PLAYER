"""光驱发现、介质分类与 CD-DA 目录（TOC）解析。

介质边界先用 Linux cdrom ioctl（DISC_STATUS + TOC control bits）判断，
详细的音轨目录仍由 cdparanoia 读取；两处结论必须都支持才放行。
"""
from __future__ import annotations

import fcntl
import glob
import hashlib
import os
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, field

from config import CD_SECTOR_BYTES


class CddaError(RuntimeError):
    """CDC/驱动层的可读错误。"""


class NoMediaError(CddaError):
    """光驱里没有盘（或托盘开着）。"""


class UnsupportedDiscError(CddaError):
    """有盘，但确认不是可播放的 CD-DA（纯数据 CD / DVD / 无音轨）。

    这是「确定不支持」而不是「暂时读不到」：调用方应稳定显示 UNSUPPORTED，
    不要像 NoMediaError/CddaError 那样反复重试。
    """

    def __init__(self, message: str, *, media: "MediaInfo | None" = None):
        super().__init__(message)
        self.media = media


class UnreadableDiscError(CddaError):
    """托盘里确实有盘，但暂时认不出 CD-DA（可能是 DVD/数据盘/读 TOC 失败）。

    与 UnsupportedDiscError 不同：它可能只是光驱还没转起来，调用方应在
    有限时间内重试；超过宽限期后再显示 UNREADABLE 并等换盘边沿。
    """


# 介质类型：audio=纯 CD-DA；mixed=CD-Extra/混合模式，只取音轨；
# data=数据 CD/DVD；unknown=ioctl 不支持或读不到，交给 cdparanoia 再判断。
MEDIA_UNKNOWN = "unknown"
MEDIA_AUDIO = "audio"
MEDIA_MIXED = "mixed"
MEDIA_DATA = "data"


@dataclass(frozen=True)
class MediaInfo:
    kind: str = MEDIA_UNKNOWN
    audio_tracks: int = 0
    data_tracks: int = 0
    # 读得到 TOC 时保留音轨编号/起始 LBA，供 query_disc 做逐轨校验；
    # 只有 DISC_STATUS 时为空，不能凭空集合放行任何轨号。
    audio_numbers: tuple[int, ...] = ()
    audio_starts: tuple[tuple[int, int], ...] = ()

    @property
    def has_audio(self) -> bool:
        return self.audio_tracks > 0 or self.kind in (MEDIA_AUDIO, MEDIA_MIXED)

    @property
    def supported(self) -> bool:
        return self.kind in (MEDIA_AUDIO, MEDIA_MIXED)

    @property
    def is_data(self) -> bool:
        return self.kind == MEDIA_DATA


_TRACK_RE = re.compile(
    r"^\s*(\d+)\.\s+(\d+)\s+\[\s*([\d:.]+)\s*\]\s+(\d+)\s+\[\s*([\d:.]+)\s*\]"
)
_TOTAL_RE = re.compile(r"^\s*TOTAL\s+(\d+)", re.MULTILINE)
_MODEL_RE = re.compile(r"CDROM sensed:\s*(.+?)\s*$", re.MULTILINE)
_NO_MEDIA_HINTS = (
    "unable to open disc",
    "no medium",
    "unable to read table of contents",
    "无法打开光盘",
)
# cdparanoia 在部分平台/版本上会明确提到数据轨；有这些证据时才能把
# 「没有音轨」判成 unsupported，不能只凭一次空输出就断言。
_UNSUPPORTED_HINTS = (
    "data track",
    "data disc",
    "not an audio cd",
    "no audio tracks",
    "not a cd-da",
    "unsupported disc",
)


@dataclass
class Track:
    number: int
    start_lba: int
    sectors: int
    copy: bool = False
    pre_emphasis: bool = False
    channels: int = 2

    @property
    def seconds(self) -> float:
        return self.sectors / 75.0

    @property
    def pcm_bytes(self) -> int:
        return self.sectors * CD_SECTOR_BYTES

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"Track {self.number:02d} ({self.seconds:.1f}s)"


@dataclass
class Disc:
    device: str
    tracks: list[Track] = field(default_factory=list)
    leadout_lba: int = 0
    model: str = "CD-ROM"

    @property
    def discid(self) -> str:
        """TOC 指纹：同一张盘永远同一目录，换盘自动分开。"""
        raw = "|".join(f"{t.start_lba}+{t.sectors}" for t in self.tracks)
        raw = f"{self.leadout_lba}|{raw}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @property
    def total_seconds(self) -> float:
        return sum(t.seconds for t in self.tracks)

    def track(self, number: int) -> Track:
        for t in self.tracks:
            if t.number == number:
                return t
        raise KeyError(f"没有第 {number} 轨")

    def next_number(self, number: int) -> int | None:
        nums = [t.number for t in self.tracks]
        i = nums.index(number)
        return nums[i + 1] if i + 1 < len(nums) else None

    def prev_number(self, number: int) -> int | None:
        nums = [t.number for t in self.tracks]
        i = nums.index(number)
        return nums[i - 1] if i > 0 else None


# Linux CD-ROM ioctl：快速分类介质，并核对「光驱里现在是不是这张盘」；
# 详细音轨目录仍然走 cdparanoia。
_CDROMREADTOCHDR = 0x5305
_CDROMREADTOCENTRY = 0x5306
_CDROM_DRIVE_STATUS = 0x5326
_CDROM_DISC_STATUS = 0x5327
_CDS_NO_DISC = 1
_CDS_TRAY_OPEN = 2
_CDS_DRIVE_NOT_READY = 3
_CDS_DISC_OK = 4
_CDROM_LBA = 1
_CDROM_LEADOUT = 0xAA
_CDROM_DATA_TRACK = 0x04          # cdte_ctrl 里的数据轨标志

# Linux cdrom.h 的 CDROM_DISC_STATUS 返回值
CDS_AUDIO = 100
CDS_DATA_1 = 101
CDS_DATA_2 = 102
CDS_XA_2_1 = 103
CDS_XA_2_2 = 104
CDS_MIXED = 105
_DATA_STATUSES = frozenset((CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2))


def classify_media(status: int | None,
                   entries: list[tuple[int, int, int]] | None = None
                   ) -> MediaInfo:
    """根据 cdrom.h 状态和 TOC 条目判断介质。

    DISC_STATUS 明确为数据盘（DATA_1/2、XA）时直接拒绝，不让 TOC 的
    控制位把它洗成音频。其余情况看 entries 每项 (轨号, 起始 LBA,
    cdte_ctrl)，ctrl 的 0x04 是数据轨：
      - 只有音轨 → audio（允许）
      - 音轨 + 数据轨 → mixed（只抓音轨；数据轨绝不能当 PCM）
      - 只有数据轨 → data（拒绝）
    读不到 TOC 时退回 DISC_STATUS；仍不确定就返回 unknown，交给 cdparanoia，
    绝不能把「暂时读不到」误判成 unsupported。
    """
    audio_entries = ([(n, lba) for n, lba, ctrl in entries
                      if not ctrl & _CDROM_DATA_TRACK]
                     if entries else [])
    audio = len(audio_entries)
    data = (len(entries) - audio) if entries else 0
    numbers = tuple(n for n, _lba in audio_entries)
    starts = tuple(audio_entries)
    if status in _DATA_STATUSES:
        return MediaInfo(MEDIA_DATA, audio, data, numbers, starts)
    if entries:
        if audio:
            return MediaInfo(MEDIA_MIXED if data else MEDIA_AUDIO,
                             audio, data, numbers, starts)
        return MediaInfo(MEDIA_DATA, 0, data, numbers, starts)
    if status == CDS_MIXED:
        return MediaInfo(MEDIA_MIXED)
    if status == CDS_AUDIO:
        return MediaInfo(MEDIA_AUDIO)
    return MediaInfo()


def probe_media(device: str) -> MediaInfo:
    """用 Linux ioctl 快速判断介质类型；非 Linux/读不到返回 unknown。

    不靠光驱型号（DVDRAM 也能读 CD）和文件扩展名，只看光驱报告的 TOC
    控制位与 DISC_STATUS。这个探测只读目录，不抓数据。
    """
    if not sys.platform.startswith("linux"):
        return MediaInfo()
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return MediaInfo()
    try:
        status = None
        try:
            status = int(fcntl.ioctl(fd, _CDROM_DISC_STATUS, 0))
        except (OSError, ValueError):
            pass
        return classify_media(status, _read_toc_entries(fd))
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _tray_has_disc(device: str) -> bool | None:
    """CDROM_DRIVE_STATUS 的独立小检查：区分「没盘」和「有盘但读不出」。

    query_disc 只有在这个检查确认 DISC_OK 时，才把 cdparanoia 的
    "unable to open disc / Is there an audio CD?" 解释成 unreadable，
    绝不能只凭一句宽泛错误把它当成数据盘。
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        status = int(fcntl.ioctl(fd, _CDROM_DRIVE_STATUS, 0))
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if status == _CDS_DISC_OK:
        return True
    if status in (_CDS_NO_DISC, _CDS_TRAY_OPEN):
        return False
    return None


def _read_toc_entries(fd: int) -> list[tuple[int, int, int]] | None:
    """读完整 TOC（含数据轨）；读不到返回 None。"""
    hdr = bytearray(2)                      # struct cdrom_tochdr
    try:
        fcntl.ioctl(fd, _CDROMREADTOCHDR, hdr)
    except OSError:
        return None
    first, last = hdr[0], hdr[1]
    if first < 1 or last < first:
        return []
    entries: list[tuple[int, int, int]] = []
    for number in range(first, last + 1):
        entry = _toc_entry(fd, number)
        if entry is None:
            return None
        lba, ctrl = entry
        entries.append((number, lba, ctrl))
    return entries


def disc_signature(disc: Disc) -> tuple:
    """本地 TOC 指纹：音轨 (轨号, 起始 LBA) + lead-out。"""
    return (tuple((t.number, t.start_lba) for t in disc.tracks), disc.leadout_lba)


def read_toc_signature(device: str) -> tuple | None:
    """用 ioctl 快速读光驱当前这张盘的 TOC 指纹；读不到/非 Linux 返回 None。

    只读目录，不抓数据（毫秒级），用来防「换盘瞬间读错盘」的竞态。
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        entries = _read_toc_entries(fd)
        if entries is None:
            return None
        starts = [(number, lba) for number, lba, ctrl in entries
                  if not ctrl & _CDROM_DATA_TRACK]   # CD-Extra 数据轨不算音轨
        lead = _toc_entry(fd, _CDROM_LEADOUT)
        if lead is None:
            return None
        return (tuple(starts), lead[0])
    except OSError:
        return None
    finally:
        os.close(fd)


def _toc_entry(fd: int, number: int) -> tuple[int, int] | None:
    entry = bytearray(12)                       # struct cdrom_tocentry
    entry[0] = number
    entry[2] = _CDROM_LBA
    try:
        fcntl.ioctl(fd, _CDROMREADTOCENTRY, entry)
    except OSError:
        return None
    lba = struct.unpack_from("<i", entry, 4)[0]
    return lba, (entry[1] >> 4) & 0x0F


def toc_matches(device: str, disc: Disc) -> bool:
    """光驱里现在这张盘的 TOC 还是不是 disc；读不到一律当不匹配（宁可不用 CD-TEXT）。"""
    sig = read_toc_signature(device)
    return sig is not None and sig == disc_signature(disc)


def find_devices() -> list[str]:
    """列出候选光驱（去重、去掉软链重复项）。"""
    cands: list[str] = []
    for pat in ("/dev/sr*", "/dev/cdrom", "/dev/dvd", "/dev/cdrom*"):
        cands.extend(sorted(glob.glob(pat)))
    out, seen = [], set()
    for c in cands:
        real = os.path.realpath(c)
        if real in seen or not os.path.exists(c):
            continue
        seen.add(real)
        out.append(c)
    return out


def default_device() -> str | None:
    devs = find_devices()
    return devs[0] if devs else None


def _drive_model_from_sysfs(device: str) -> str:
    base = os.path.basename(os.path.realpath(device))
    try:
        with open(f"/sys/block/{base}/device/model", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return "CD-ROM"


def parse_toc(text: str) -> tuple[list[Track], int]:
    """从 `cdparanoia -Q` 的输出里解析曲目表。"""
    tracks: list[Track] = []
    for line in text.splitlines():
        m = _TRACK_RE.match(line)
        if not m:
            continue
        num, sectors, _length, start_lba = m.group(1), m.group(2), m.group(3), m.group(4)
        tail = line[m.end():].split()
        copy = (tail[0].lower() == "yes") if len(tail) > 0 else False
        pre = (tail[1].lower() == "yes") if len(tail) > 1 else False
        ch = 2
        if len(tail) > 2 and tail[2].isdigit():
            ch = int(tail[2])
        tracks.append(Track(int(num), int(start_lba), int(sectors), copy, pre, ch))

    total = 0
    m = _TOTAL_RE.search(text)
    if m:
        total = int(m.group(1))
    if not total and tracks:
        last = tracks[-1]
        total = last.start_lba + last.sectors
    return tracks, total


def _unsupported_message(device: str, media: MediaInfo) -> str:
    """给 UI/CLI 的明确拒绝文案，避免误显示 NO DISC 或无限 CHECKING。"""
    if media.data_tracks:
        return (f"{device} 里是数据盘（{media.data_tracks} 条数据轨、"
                f"{media.audio_tracks} 条 CD-DA 音轨）— 只支持音乐 CD")
    if media.is_data:
        return (f"{device} 被光驱识别为数据盘 — 只支持音乐 CD"
                "（不支持 DVD/数据 CD）")
    return f"{device} 里没有 CD-DA 音轨 — 只支持音乐 CD（不支持 DVD/数据 CD）"


def _unreadable_message(device: str) -> str:
    return (f"{device} 里有盘，但无法识别为 CD-DA"
            "（可能是不支持的 DVD/数据盘，或光驱暂时读不出 TOC）")


def _read_failure(device: str, media: MediaInfo, message: str) -> CddaError:
    """把读盘失败映射成正确的重试类别，顺序不能乱。

    1. 明确 TRAY_OPEN/NO_DISC → NoMediaError；
    2. 已确认 AUDIO/MIXED → 可重试 CddaError（短暂 TOC 故障不应被
       UNREADABLE 永久锁死）；
    3. UNKNOWN（含 DRIVE_STATUS 暂时读不到）→ UnreadableDiscError
       （有限宽限后 sticky，不能无限 CHECKING，也不能冒充无盘）；
    4. 其余异常 → 可重试 CddaError。
    """
    tray = _tray_has_disc(device)
    if tray is False:
        return NoMediaError(f"{device} 里没有可读的碟片")
    if media.kind in (MEDIA_AUDIO, MEDIA_MIXED):
        return CddaError(message)
    if media.kind == MEDIA_UNKNOWN:
        return UnreadableDiscError(_unreadable_message(device))
    return CddaError(message)


def query_disc(device: str, timeout: float = 90.0) -> Disc:
    """跑 cdparanoia -Q 拿 TOC。

    错误分四类，调用方的重试策略不同：
      - NoMediaError：没盘/托盘开着，可等；
      - UnsupportedDiscError：确认有盘但没有 CD-DA 音轨，不重试；
      - UnreadableDiscError：托盘有盘但认不出 CD-DA，有限重试后 UNREADABLE；
      - CddaError：已知音频盘暂时没就绪/IO 失败，可重试。
    """
    if not os.path.exists(device):
        raise CddaError(f"设备不存在: {device}")

    # 先做便宜的介质分类。数据 DVD/CD 不启动 cdparanoia，更不启动抓轨器。
    media = probe_media(device)
    if media.is_data:
        raise UnsupportedDiscError(_unsupported_message(device, media),
                                   media=media)

    try:
        proc = subprocess.run(
            ["cdparanoia", "-Q", "-d", device],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CddaError("找不到 cdparanoia，请先安装（pacman -S cdparanoia）") from exc
    except subprocess.TimeoutExpired as exc:
        raise _read_failure(
            device, media,
            f"光驱 {device} 读取 TOC 超时（碟片/托盘/设备忙？）") from exc

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    low = text.lower()
    snippet = "\n".join(text.strip().splitlines()[:6])
    # 明确的「数据轨」证据优先；然后才处理各种读取失败。
    if any(h in low for h in _UNSUPPORTED_HINTS):
        raise UnsupportedDiscError(
            _unsupported_message(device, media), media=media)
    if any(h in low for h in _NO_MEDIA_HINTS):
        # cdparanoia 对数据 DVD 也常说 "Unable to open disc. Is there an
        # audio CD?"。只有 UNKNOWN + DRIVE_STATUS=DISC_OK 才归到
        # UNREADABLE；已确认 AUDIO/MIXED 的短期 TOC 故障必须可重试。
        raise _read_failure(
            device, media,
            f"{device} 暂时无法读取 TOC：\n{snippet}")

    # 关键边界：cdparanoia 非零退出时，哪怕 stdout/stderr 里恰好有像
    # 轨道行的文本，也绝不能构造 Disc —— 那可能是错误输出或旧盘残留。
    if proc.returncode != 0:
        raise _read_failure(
            device, media,
            f"cdparanoia 读取失败（退出码 {proc.returncode}），"
            f"不会使用其输出：\n{snippet}")

    tracks, total = parse_toc(text)
    if not tracks:
        # TOC 明确说是 audio 却一轨没读到：是读取失败，下一轮再试；
        # 只有 UNKNOWN + 有盘才归到 UNREADABLE。
        raise _read_failure(
            device, media,
            f"读不到音轨目录，cdparanoia 输出：\n{snippet}")

    # 混合模式/普通音频盘：cdparanoia 只该列出 TOC 里的音轨。数量相同
    # 还不够——必须逐轨核对编号，最好再核对起始 LBA，否则数据轨 #1
    # 可能被错误地当成音轨 #1 抓取。
    actual_numbers = tuple(t.number for t in tracks)
    if media.audio_tracks and len(tracks) != media.audio_tracks:
        raise CddaError(
            f"cdparanoia 音轨数与 TOC 不一致（{len(tracks)} vs "
            f"{media.audio_tracks} 条音轨），拒绝使用本次输出")
    if media.audio_numbers and actual_numbers != media.audio_numbers:
        raise CddaError(
            f"cdparanoia 音轨编号与 TOC 不一致（{actual_numbers} vs "
            f"{media.audio_numbers}），拒绝使用本次输出")
    actual_starts = tuple((t.number, t.start_lba) for t in tracks)
    if media.audio_starts and actual_starts != media.audio_starts:
        raise CddaError(
            f"cdparanoia 起始 LBA 与 TOC 不一致（{actual_starts} vs "
            f"{media.audio_starts}），拒绝使用本次输出")

    m = _MODEL_RE.search(text)
    model = m.group(1) if m else _drive_model_from_sysfs(device)
    return Disc(device=device, tracks=tracks, leadout_lba=total, model=model)
