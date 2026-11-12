"""播放引擎：把 PCM 喂给 `pw-cat`（PipeWire），自己算播放头。

为什么用 pw-cat：本机没有 pip 也没有 python-sounddevice，而 PipeWire 自带 pw-cat，
读 stdin 的裸 PCM；管道的反压天然就是节拍器，不需要我们自己写音频回调。
播放头 = 已写入字节 - 管道/节点缓冲，误差约 0.2 s —— 对可视化和进度条完全够用。
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import threading
import time

import numpy as np

from audio.media import FeedAbort, TrackMedia
from config import (BYTES_PER_FRAME, BYTES_PER_SEC, CHANNELS, FEED_CHUNK,
                    OUTPUT_LATENCY_S, OUTPUT_QUICK_STOP_S, PIPE_BYTES,
                    SAMPLE_RATE)
from services.deps import pipewire_socket

_F_SETPIPE_SZ = 1031


def _launch_error(exc: OSError | None) -> str:
    """pw-cat 根本没起来：最常见的是包没装，别只说「启动失败」。"""
    if isinstance(exc, FileNotFoundError):
        return ("找不到 pw-cat：装 PipeWire（Arch: pipewire-audio / "
                "Ubuntu: pipewire-bin），再用 --check-deps 核对")
    return f"启动 pw-cat 失败: {exc}"


def _output_error() -> str:
    """pw-cat 提前退出：区分「没装」和「装了但用户服务没起」。"""
    if shutil.which("pw-cat") is None:
        return "找不到 pw-cat：装 PipeWire 后重试（--check-deps 可核对）"
    if pipewire_socket() is None:
        return "未检测到 PipeWire 会话：systemctl --user start pipewire"
    return "pw-cat 提前退出：检查 PipeWire 会话和音频设备（可先跑 --check-deps）"


def _apply_gain(data: bytes, gain: float) -> bytes:
    arr = np.frombuffer(data, dtype="<i2").astype(np.float32) * gain
    return np.clip(arr, -32768, 32767).astype("<i2").tobytes()


class Engine:
    """一个播放头 + 一个 pw-cat 子进程 + 一个喂数据线程。"""

    def __init__(self, on_audio=None, on_state=None):
        self._lock = threading.RLock()
        self.on_audio = on_audio
        self.on_state = on_state

        self.state = "idle"           # idle / buffering / playing / paused / eof / error
        self.error = ""
        self.volume = 1.0
        self._media: TrackMedia | None = None
        self._consumed = 0            # 已交给 pw-cat 的字节
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._abort: threading.Event | None = None
        self._paused = threading.Event()
        self._generation = 0          # 每次 _start_output +1；喂数据线程只收尾自己那一代

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def load(self, media: TrackMedia, start_s: float = 0.0, paused: bool = False) -> None:
        self._stop_output()
        with self._lock:
            self._media = media
            if paused:
                self._paused.set()
            else:
                self._paused.clear()
            offset = int(max(0.0, start_s) * BYTES_PER_SEC) // BYTES_PER_FRAME * BYTES_PER_FRAME
            self._consumed = offset
        self._start_output(offset)

    def cue(self, media: TrackMedia, start_s: float = 0.0) -> None:
        """装好音源但不开输出：启动/插盘默认停在 stopped。

        和 load(paused=True) 的区别：后者仍会起一个 pw-cat 子进程和喂数据线程，
        只是暂时不喂；这里连输出都不建，等用户按 Space 调 play() 才真正开播。
        启动阶段因此不需要碰光驱抓轨，也不会多一个空转的音频进程。
        """
        self._stop_output()
        with self._lock:
            self._media = media
            self._paused.set()
            offset = int(max(0.0, start_s) * BYTES_PER_SEC) // BYTES_PER_FRAME * BYTES_PER_FRAME
            self._consumed = offset
        self._set_state("stopped")

    def stop(self) -> None:
        self._stop_output()
        with self._lock:
            self._media = None
            self._consumed = 0
            self._set_state("idle")

    def stop_playback(self) -> None:
        """停止（区别于暂停）：关输出、播放头回到本轨开头，音源保留。

        和 stop() 的区别是仍持有 media，Space 可以立刻从头重播；也不留
        暂停中的 pw-cat 进程。seek 在 stopped 态只挪播放头，不出声。
        """
        with self._lock:
            if self._media is None:
                return
            self._paused.set()               # 先堵住喂数据线程，避免尾巴漏音
        self._stop_output()
        with self._lock:
            self._consumed = 0
            self._set_state("stopped")

    # ── 控制 ──────────────────────────────────────────────────────────────
    def play(self) -> None:
        if self.state == "stopped":
            with self._lock:
                offset = self._consumed
            self._paused.clear()
            self._start_output(offset)       # 从停止位置（默认 0）重开输出
            return
        if self.state in ("paused", "buffering"):
            self._paused.clear()
            self._set_state("playing")

    def pause(self) -> None:
        if self.state == "playing":
            self._paused.set()
            self._set_state("paused")

    def toggle(self) -> None:
        if self.state == "stopped":
            self.play()
        elif self._paused.is_set() or self.state in ("paused", "buffering"):
            self.play()
        elif self.state == "playing":
            self.pause()

    def seek_to(self, seconds: float) -> None:
        with self._lock:
            media = self._media
            if media is None:
                return
            seconds = max(0.0, min(seconds, media.track.seconds - 0.05))
            offset = int(seconds * BYTES_PER_SEC) // BYTES_PER_FRAME * BYTES_PER_FRAME
            if self.state == "stopped":
                self._consumed = offset     # 停止态：只挪播放头，不重开输出
                return
            if (self.state in ("playing", "paused")
                    and self._proc is not None and offset == self._consumed):
                return                      # 原地 Seek 不白重启 pw-cat
            paused = self._paused.is_set()
        # Seek 是用户主动打断旧流：快速切断，避免 proc.wait/join 的 1s 上限
        # 把主线程卡住。正常 stop/换轨仍走优雅收尾。
        self._stop_output(quick=True)
        with self._lock:
            self._consumed = offset
            if not paused:
                self._paused.clear()
        # 暂停态保持 _paused，让 _start_output 直接进入 paused；旧实现先
        # clear 再 start 再 set，喂数据线程可能抢跑发出短暂声音。
        self._start_output(offset)

    def seek_rel(self, delta: float) -> None:
        self.seek_to(self.position() + delta)

    def set_volume(self, value: float) -> None:
        with self._lock:
            self.volume = max(0.0, min(1.5, value))

    def nudge_volume(self, delta: float) -> None:
        self.set_volume(self.volume + delta)

    # ── 查询 ──────────────────────────────────────────────────────────────
    @property
    def media(self) -> TrackMedia | None:
        return self._media

    def position(self) -> float:
        with self._lock:
            consumed = self._consumed
            live = self._proc is not None      # 停止态没有输出，不该再减管道延迟
        latency = OUTPUT_LATENCY_S if live else 0.0
        return max(0.0, consumed / BYTES_PER_SEC - latency)

    def duration(self) -> float:
        return self._media.track.seconds if self._media else 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "error": self.error,
                "volume": self.volume,
                "position": self.position(),
                "duration": self.duration(),
                "track": self._media.track.number if self._media else None,
            }

    # ── 内部：音频输出 ────────────────────────────────────────────────────
    def _set_state(self, state: str) -> None:
        with self._lock:
            if self.state == state:
                return
            self.state = state
        if self.on_state:
            self.on_state(state)

    def _set_state_if_current(self, state: str, abort: threading.Event,
                              generation: int) -> bool:
        """只有仍属于 ``abort``/``generation`` 这一代时才改状态。

        旧 feeder 可能在 read 返回 EOF 后被切走；Seek 重启新流后再恢复时，
        它不能把新流标成 eof。检查与赋值必须在同一把锁内完成。
        """
        with self._lock:
            if (abort.is_set() or self._abort is not abort
                    or self._generation != generation):
                return False
            changed = self.state != state
            self.state = state
        if changed and self.on_state:
            self.on_state(state)
        return True

    def _stream_is_current(self, abort: threading.Event,
                           generation: int) -> bool:
        with self._lock:
            return (not abort.is_set() and self._abort is abort
                    and self._generation == generation)

    def _start_output(self, offset: int) -> None:
        # 偶发 EBADF：Python 3.14 走 posix_spawn 时，若另一线程正好在关闭 fd，
        # glibc 的 close 文件动作会拿到 EBADF。重试几次即可，代价可以忽略。
        proc = None
        last_exc: OSError | None = None
        for attempt in range(5):
            try:
                proc = subprocess.Popen(
                    ["pw-cat", "-p", "-a", "--rate", str(SAMPLE_RATE),
                     "--channels", str(CHANNELS), "--format", "s16", "-"],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, bufsize=0,
                )
                break
            except OSError as exc:
                last_exc = exc
                time.sleep(0.01 * (attempt + 1))
        if proc is None:
            self.error = _launch_error(last_exc)
            self._set_state("error")
            return
        try:  # 收窄管道到 16 KB，降低播放头误差
            fcntl.fcntl(proc.stdin.fileno(), _F_SETPIPE_SZ, PIPE_BYTES)  # type: ignore[union-attr]
        except OSError:
            pass

        abort = threading.Event()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._proc = proc
            self._abort = abort
        self._set_state("paused" if self._paused.is_set() else "playing")
        th = threading.Thread(target=self._feed_loop,
                              args=(proc, abort, offset, generation),
                              name="feeder", daemon=True)
        with self._lock:
            self._thread = th
        th.start()

    def _stop_output(self, abort: threading.Event | None = None, *,
                     quick: bool = False) -> bool:
        """停掉当前输出。

        传 abort 时只停它所属的那一条流：旧的喂数据线程收尾时不能误杀 seek/换轨
        后新装的 pw-cat。返回 False 表示这条流已经被替换，什么都没做。

        ``quick=True`` 用于 Seek：不再等旧 pw-cat 把缓冲放完，终止后最多
        ``OUTPUT_QUICK_STOP_S`` 就 kill；正常 stop/换轨仍保留优雅收尾。
        """
        with self._lock:
            if abort is not None and self._abort is not abort:
                return False
            ab, proc, th = self._abort, self._proc, self._thread
            self._abort = None
            self._proc = None
            self._thread = None
        if ab:
            ab.set()
        if proc:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            if quick:
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                proc.wait(timeout=OUTPUT_QUICK_STOP_S if quick else 1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=OUTPUT_QUICK_STOP_S if quick else 1.0)
                except subprocess.TimeoutExpired:
                    pass
        if th and th is not threading.current_thread():
            th.join(timeout=OUTPUT_QUICK_STOP_S if quick else 1.0)
        return True

    def _write_all(self, proc: subprocess.Popen, data: bytes) -> None:
        fd = proc.stdin.fileno()            # type: ignore[union-attr]
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise BrokenPipeError("pw-cat 不再接收数据")
            view = view[n:]

    def fail(self, message: str) -> None:
        """外部（app）判定当前音源不可用时调用：收尾当前输出并进入 error 状态。"""
        with self._lock:
            abort, generation = self._abort, self._generation
        if abort is None:
            return
        self._fail(message, abort, generation)

    def _fail(self, message: str, abort: threading.Event, generation: int) -> None:
        """喂数据线程出错：不当作正常 EOF。

        只收尾自己所属的那一条流 —— seek/换轨会换 abort 并递增 generation，
        旧线程迟到的失败不能把新流标成 error。
        """
        with self._lock:
            if self._abort is not abort or self._generation != generation:
                return
        if not self._stop_output(abort):
            return
        with self._lock:
            if self._generation != generation:
                return
        self.error = message
        self._set_state("error")

    def _feed_loop(self, proc: subprocess.Popen, abort: threading.Event, offset: int,
                   generation: int) -> None:
        media = self._media
        if media is None:
            return
        try:
            while not abort.is_set():
                if self._paused.is_set():
                    time.sleep(0.02)
                    continue
                data = media.read(offset, FEED_CHUNK, abort)
                if not data:
                    self._set_state_if_current("eof", abort, generation)
                    return
                with self._lock:
                    gain = self.volume
                out = data if gain == 1.0 else _apply_gain(data, gain)
                self._write_all(proc, out)
                offset += len(data)
                # 旧代喂数据线程可能在 close/join 的窗口里刚写完一块；它绝
                # 不能把 seek 后的新播放头覆盖回旧位置。
                with self._lock:
                    if abort.is_set() or self._generation != generation:
                        return
                    self._consumed = offset
                if self.on_audio and self._stream_is_current(abort, generation):
                    self.on_audio(out)
        except FeedAbort:
            return
        except BrokenPipeError:
            if not abort.is_set():
                self._fail(_output_error(), abort, generation)
        except Exception as exc:                      # noqa: BLE001 - 线程里不能漏
            if not abort.is_set():
                self._fail(str(exc) or exc.__class__.__name__, abort, generation)
