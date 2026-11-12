"""抓轨缓存布局：~/.cache/asciicd/<discid>/NN.pcm(.part) + meta.json

缓存根目录里还住着 fontcache/、crash.log 等别的东西，所以这里的每个删除
操作都要先回答两个问题：**这个目录是本应用生成的吗？**、**现在有别的实例
在用它吗？** 答案不确定时一律跳过 —— 宁可少清几百 MB，也不能删用户文件。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from cdda.device import Disc
from config import CACHE_DIR, META_VERSION, PCM_FORMAT_VERSION
from textfmt import one_line

try:                                  # 非 POSIX 平台上没有 flock，退化成不加锁
    import fcntl
except ImportError:                   # pragma: no cover - Windows
    fcntl = None                      # type: ignore[assignment]

# discid 是 sha1 十六进制前 16 位。清缓存时只认这种目录名，
# 免得把同一层里放着的 fontcache/、crash.log 之类一起删掉。
_DISCID_RE = re.compile(r"^[0-9a-f]{16}$")
# 本应用生成的音频文件名：NN.pcm / NN.pcm.part / NN.done
_AUDIO_RE = re.compile(r"^\d{2}\.(?:pcm|pcm\.part|done)$")

_MARKER = ".asciicd-cache"            # 目录归属标记：只有我们创建的目录才有
_LOCK = ".lock"                       # 每盘独占锁：同一张盘只允许一个实例写
_HOUSEKEEPING = frozenset({_MARKER, _LOCK})


def cache_root_for(args) -> Path:
    """本次运行的缓存根。

    --fake 单独一个子目录：试玩合成光盘不该清掉正在播放的真盘 PCM，
    也不该被另一个实例的换碟清理顺手删掉。
    """
    base = getattr(args, "cache_dir", None)
    if base:
        root = Path(base).expanduser()
    else:
        root = Path(os.environ.get("ASCIICD_CACHE") or os.path.expanduser(CACHE_DIR))
    return root / "fake" if getattr(args, "fake", False) else root


def _read_json_object(path: Path) -> dict | None:
    """读一个「应该是对象」的 JSON 文件；损坏或形状不对都当不存在。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> bool:
    """原子写：先写 .tmp 再 rename，断电也不会留下半个 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def clean_meta(data: dict) -> dict:
    """把元数据里的外部文本压成单行；结构不对的字段一律丢掉。

    meta.json 可能来自旧版本、手写或损坏的磁盘，读进来先按预期形状校验，
    再用同样的规则清洗 —— 渲染层就不用再防换行和转义序列。
    """
    clean = dict(data)
    clean["album"] = one_line(data.get("album"))
    clean["artist"] = one_line(data.get("artist"))
    tracks = data.get("tracks")
    cleaned: dict[str, str] = {}
    if isinstance(tracks, dict):
        for number, title in tracks.items():
            text = one_line(title)
            if text:
                cleaned[str(number)] = text
    clean["tracks"] = cleaned
    sources = data.get("sources")
    clean["sources"] = [str(s) for s in sources] if isinstance(sources, list) else []
    return clean


class Cache:
    def __init__(self, root: str | Path | None = None):
        if root is None:
            root = os.environ.get("ASCIICD_CACHE") or os.path.expanduser(CACHE_DIR)
        self.root = Path(root).expanduser()
        self.shared = True        # False = 实例私有临时缓存，close() 时删掉
        self._meta_cache: tuple[tuple[str, float], dict] | None = None
        self._root_fd: int | None = None
        self._disc_fd: int | None = None

    @classmethod
    def isolated(cls, prefix: str = "asciicd-cache-") -> "Cache":
        """实例私有的临时缓存：拿不到共享缓存时，只写自己家的目录。"""
        cache = cls(tempfile.mkdtemp(prefix=prefix))
        cache.shared = False
        return cache

    # ── 目录 ──────────────────────────────────────────────────────────────
    def disc_dir(self, disc: Disc) -> Path:
        """纯路径计算，不碰磁盘。

        以前这里顺手 mkdir，导致每帧 UI 刷新（available_bytes / read_meta）
        都白做几百次 mkdir 系统调用。建目录统一走 ensure_dir()。
        """
        return self.root / disc.discid

    def ensure_dir(self, disc: Disc) -> Path:
        d = self.disc_dir(disc)
        d.mkdir(parents=True, exist_ok=True)
        if not d.is_symlink() and self._looks_like_ours(d):
            self._mark_owned(d)
        return d

    def part_path(self, disc: Disc, number: int) -> Path:
        return self.disc_dir(disc) / f"{number:02d}.pcm.part"

    def pcm_path(self, disc: Disc, number: int) -> Path:
        return self.disc_dir(disc) / f"{number:02d}.pcm"

    def done_path(self, disc: Disc, number: int) -> Path:
        return self.disc_dir(disc) / f"{number:02d}.done"

    def meta_path(self, disc: Disc) -> Path:
        return self.disc_dir(disc) / "meta.json"

    # ── 归属与互斥 ────────────────────────────────────────────────────────
    def _looks_like_ours(self, path: Path) -> bool:
        """目录名像 discid，且要么带归属标记，要么只含我们认识的文件。

        后半条是为了接管旧版本留下的缓存：只有「一个陌生文件都没有」才认，
        所以 other-project/ 这类同名层级的目录永远不会被当成我们的。
        """
        if not _DISCID_RE.match(path.name):
            return False
        if path.is_symlink():
            return False
        try:
            if not path.is_dir():
                return False
        except OSError:
            return False
        marker = path / _MARKER
        if marker.exists() and not marker.is_symlink():
            return True
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name in _HOUSEKEEPING:
                        continue
                    if entry.name in ("meta.json", "meta.json.tmp"):
                        continue
                    if _AUDIO_RE.match(entry.name):
                        continue
                    return False
        except OSError:
            return False
        return True

    def _mark_owned(self, path: Path) -> None:
        try:
            (path / _MARKER).touch(exist_ok=True)
        except OSError:
            pass

    def _open_root_lock(self) -> None:
        """在缓存根上挂一把共享锁：运行期间别的实例不会去清整根缓存。"""
        if fcntl is None or self._root_fd is not None:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / _LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return
        self._root_fd = fd

    def _purge_allowed(self) -> bool:
        """只有没有别的实例在跑时，才允许清整根缓存/迁移格式。"""
        if self._root_fd is None:
            return True
        try:
            fcntl.flock(self._root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        try:
            fcntl.flock(self._root_fd, fcntl.LOCK_SH)
        except OSError:
            pass
        return True

    def acquire(self, disc: Disc) -> bool:
        """拿到这张盘的独占写权限；False = 换个隔离缓存再去玩。

        同一张盘同时开两个实例时，第二个实例不能写共享的 .pcm/.part，
        否则两个写者会互相踩。调用方拿到 False 应改用 Cache.isolated()。
        """
        self._open_root_lock()
        if not self._format_current():
            return False
        d = self.disc_dir(disc)
        if d.is_symlink():
            return False
        if d.exists() and not self._looks_like_ours(d):
            return False                   # 同名目录但不是我们建的：绕开
        try:
            self.ensure_dir(disc)
        except OSError:
            return False
        fd = self._open_disc_lock(d)
        if fd is None:
            return False
        self._disc_fd = fd
        return True

    def _open_disc_lock(self, d: Path) -> int | None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(d / _LOCK, flags, 0o600)
        except OSError:
            return None
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return None
        return fd

    def close(self) -> None:
        """释放锁；实例私有缓存连目录一起删掉。"""
        fd, self._disc_fd = self._disc_fd, None
        if fd is not None:
            _close_fd(fd)
        fd, self._root_fd = self._root_fd, None
        if fd is not None:
            _close_fd(fd)
        if not self.shared:
            shutil.rmtree(self.root, ignore_errors=True)

    # ── 格式版本（换 PCM 格式时一次性清掉旧缓存） ─────────────────────────
    def _format_current(self) -> bool:
        data = _read_json_object(self.root / "format.json")
        if data is None:
            return False
        try:
            return int(data.get("pcm_version", 0)) == PCM_FORMAT_VERSION
        except (TypeError, ValueError):
            return False

    def ensure_format(self) -> None:
        if self._format_current():
            return
        # 先挂根锁，再判断能不能清：否则「另一个实例正在用」根本不可见。
        self._open_root_lock()
        if not self._purge_allowed():
            # 有别的实例正在用这个缓存根：既不能删它的 PCM，也不能把
            # format.json 标成新格式 —— 留旧标记，acquire() 会让本次改用
            # 隔离缓存，避免读到格式不对的旧音频。
            return
        # 旧缓存可能字节序/格式不对，宁可重抓。只清确认属于我们的目录里的
        # 音频文件，别的实例正在用的目录由 _disc_dirs() 跳过。
        for path in self._disc_dirs():
            self._remove_audio(path)
            self._drop_if_empty(path)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        _write_json(self.root / "format.json", {
            "pcm_version": PCM_FORMAT_VERSION, "format": "s16le",
            "rate": 44100, "channels": 2,
        })
        self._meta_cache = None

    def _disc_dirs(self):
        """遍历缓存根下确认属于本应用的 disc 目录（跳过正在被别的实例用的）。"""
        try:
            with os.scandir(self.root) as entries:
                paths = [Path(e.path) for e in entries]
        except OSError:
            return
        for path in paths:
            if not self._looks_like_ours(path):
                continue
            if _dir_in_use(path):
                continue
            yield path

    # ── 状态 ──────────────────────────────────────────────────────────────
    def bytes_expected(self, disc: Disc, number: int) -> int:
        """按 TOC 算这一轨应有的 PCM 字节数（用来校验缓存是否完整）。"""
        return disc.track(number).pcm_bytes

    def pcm_size(self, disc: Disc, number: int) -> int | None:
        try:
            return self.pcm_path(disc, number).stat().st_size
        except OSError:
            return None

    def part_size(self, disc: Disc, number: int) -> int | None:
        try:
            return self.part_path(disc, number).stat().st_size
        except OSError:
            return None

    def is_done(self, disc: Disc, number: int) -> bool:
        """完成标记 + PCM 大小都和 TOC 对得上才算真的抓完。

        只有 .done 而没有正确大小的 .pcm（或反过来）都说明缓存损坏/只写了一半，
        必须重抓，不能当完成品播放。
        """
        if not self.done_path(disc, number).exists():
            return False
        return self.pcm_size(disc, number) == self.bytes_expected(disc, number)

    def mark_done(self, disc: Disc, number: int) -> None:
        self.done_path(disc, number).touch()

    def reset_track(self, disc: Disc, number: int) -> None:
        """重抓一轨前清干净：半成品、完成标记，以及可疑的旧 .pcm 都要扔掉。"""
        for p in (self.part_path(disc, number), self.done_path(disc, number),
                  self.pcm_path(disc, number)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def available_bytes(self, disc: Disc, number: int) -> int:
        """当前可信的 PCM 字节数（进行中的 .part 也算）。

        已完成但大小与 TOC 不符的旧 .pcm 不可信：宁可当 0，也不能把截断的
        数据喂给播放器（否则会被当成正常轨尾，静默跳轨）。
        """
        expected = self.bytes_expected(disc, number)
        if self.pcm_size(disc, number) == expected:
            return expected
        part = self.part_size(disc, number)
        return min(part, expected) if part is not None else 0

    def readable_path(self, disc: Disc, number: int) -> Path | None:
        """返回可信的读取路径：大小正确的完成品优先，其次正在抓的 .part。"""
        if self.pcm_size(disc, number) == self.bytes_expected(disc, number):
            return self.pcm_path(disc, number)
        if self.part_size(disc, number) is not None:
            return self.part_path(disc, number)
        return None

    def finalize_track(self, disc: Disc, number: int, tolerate: int = 0) -> bool:
        """把 .pcm.part 收尾成正式 .pcm：大小对齐 TOC，再加 .done 标记。

        tolerate 容忍 cdparanoia 少量少写（补静音凑齐），防止最后一小段对不上
        导致整轨被当成损坏。返回 False 表示文件不完整，调用方应按失败重试。
        """
        expected = self.bytes_expected(disc, number)
        part = self.part_path(disc, number)
        try:
            size = part.stat().st_size
        except OSError:
            return False
        if size < expected - tolerate:
            return False
        try:
            if size < expected:
                with part.open("ab") as fh:
                    fh.write(b"\x00" * (expected - size))
            elif size > expected:
                os.truncate(part, expected)
            part.replace(self.pcm_path(disc, number))
        except OSError:
            return False
        try:
            self.mark_done(disc, number)
        except OSError:
            pass                    # PCM 大小已经对齐，RipperBase.is_done() 的兜底仍认这一轨
        return True

    def repair_incomplete(self, disc: Disc) -> None:
        """启动时自愈上一次崩溃/断电留下的缓存：

        - .part 已经写满整轨（只是没来得及 rename + 落 .done）→ 直接收尾，不重抓；
        - 半截 .part 是废数据 → 删掉，让抓轨器从头来。
        """
        if not self._looks_like_ours(self.disc_dir(disc)):
            return
        for track in disc.tracks:
            number = track.number
            if self.is_done(disc, number):
                continue
            part = self.part_size(disc, number)
            if part is None:
                continue
            if part >= self.bytes_expected(disc, number):
                self.finalize_track(disc, number)
            else:
                try:
                    self.part_path(disc, number).unlink()
                except OSError:
                    pass

    # ── 元数据（曲名 / CD-TEXT / MusicBrainz 缓存） ───────────────────────
    def read_meta(self, disc: Disc) -> dict:
        path = self.meta_path(disc)
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return {}
        key = (disc.discid, stamp)
        if self._meta_cache and self._meta_cache[0] == key:
            return self._meta_cache[1]
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        # 合法 JSON 不代表形状对：[] 也能通过 json.loads，但 .get 会炸。
        if not isinstance(data, dict):
            self._meta_cache = (key, {})
            return {}
        # 旧版本查出来的元数据可能是错的（比如按模糊 TOC 配到了别的盘），
        # 版本对不上就当没有，让后台重新查。空结果也缓存，别每帧重读文件。
        if data.get("meta_version") != META_VERSION:
            self._meta_cache = (key, {})
            return {}
        # 老格式只有 release_id / source，没有 sources；按来源补一个，
        # 让「还要不要再去查 CD-TEXT / MB」的判断有依据。
        sources = data.get("sources")
        if not isinstance(sources, list):
            sources = ["musicbrainz"] if data.get("release_id") else []
        data = clean_meta(data)
        data["sources"] = [str(s) for s in sources]
        self._meta_cache = (key, data)
        return data

    def write_meta(self, disc: Disc, data: dict) -> None:
        data = clean_meta(data)
        data.setdefault("meta_version", META_VERSION)
        data.setdefault("discid", disc.discid)
        data.setdefault("device_model", disc.model)
        data["toc"] = [{"n": t.number, "start": t.start_lba, "sectors": t.sectors} for t in disc.tracks]
        data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._meta_cache = None
        self.ensure_dir(disc)
        tmp = self.meta_path(disc).with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.meta_path(disc))

    def title_of(self, disc: Disc, number: int) -> tuple[str, str]:
        """返回 (曲名, 艺人)；没有元数据时给个像样的占位。"""
        meta = self.read_meta(disc)
        track_title = one_line((meta.get("tracks") or {}).get(str(number)) or "")
        artist = one_line(meta.get("artist") or "")
        album = one_line(meta.get("album") or "")
        if not track_title:
            track_title = f"Track {number:02d}"
        if not artist and album:
            artist = album
        return track_title, artist

    # ── 清理 ──────────────────────────────────────────────────────────────
    def clear(self, disc: Disc) -> None:
        d = self.disc_dir(disc)
        if d.exists() and self._looks_like_ours(d):
            shutil.rmtree(d, ignore_errors=True)
        self._meta_cache = None

    def clear_audio(self, disc: Disc) -> None:
        """只删本盘的 PCM/半成品，保留 meta.json（曲名离线也能用）。"""
        d = self.disc_dir(disc)
        if not self._looks_like_ours(d):
            return
        self._remove_audio(d)
        # 本实例自己拿着这把锁，收掉空壳是安全的：剩下的 PCM 已经删完，
        # 打开的 fd 指向被 unlink 的 inode，不影响 rmdir。
        self._drop_if_empty(d, ignore_lock=True)

    def purge_except(self, disc: Disc) -> None:
        """换碟时清掉别的盘的音频缓存，但把它们的 meta.json 留下。

        PCM 体积大，只为当前这张盘服务；曲名缓存只有几 KB，留着以后没网
        也能显示。只认本应用生成的 disc 目录和音频文件名；别的实例正在用
        的盘、缓存根目录里的 fontcache 之类，一概不碰。
        """
        if not self._purge_allowed():
            return                       # 有别的实例在跑：这轮不清理
        keep = disc.discid
        for path in self._disc_dirs():
            if path.name == keep:
                continue
            self._remove_audio(path)
            self._drop_if_empty(path)
        self._meta_cache = None

    def _remove_audio(self, path: Path) -> None:
        try:
            with os.scandir(path) as entries:
                names = [e.name for e in entries if _AUDIO_RE.match(e.name)]
        except OSError:
            return
        for name in names:
            target = path / name
            try:
                if target.is_symlink() or target.is_dir():
                    continue             # 不跟随、也不删目录
                target.unlink()
            except OSError:
                pass

    def _drop_if_empty(self, path: Path, ignore_lock: bool = False) -> None:
        """只剩归属标记和锁文件时收掉空壳；有 meta.json 的留着。"""
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name not in _HOUSEKEEPING:
                        return
        except OSError:
            return
        if not ignore_lock and _dir_in_use(path):
            return
        shutil.rmtree(path, ignore_errors=True)

    def size_on_disk(self, disc: Disc) -> int:
        d = self.disc_dir(disc)
        return sum(p.stat().st_size for p in d.glob("*") if p.is_file())


def _close_fd(fd: int) -> None:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


def _dir_in_use(path: Path) -> bool:
    """这个目录的 .lock 是否被别的进程（或本进程的另一个打开描述）占着。"""
    if fcntl is None:
        return False
    try:
        fd = os.open(path / _LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
