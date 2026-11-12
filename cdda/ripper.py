"""渐进抓轨：后台把 CD 轨转成本地 PCM 文件，播放端只读本地。

线程模型：一个 ripper 线程，同一时刻只有一个 cdparanoia 子进程（避免光驱来回换轨）。
优先级：播放器当前想要的轨 > 下一轨 > 其余未完成的轨。
"""
from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time

import numpy as np

from cdda.cache import Cache
from cdda.device import Disc
from cdda.fake import MusicSynth
from config import (BYTES_PER_FRAME, BYTES_PER_SEC, FEED_CHUNK, RIP_ATTEMPTS,
                    RIP_FAST, RIP_MODE, RIP_MODES, RIP_PRIO_DEBOUNCE_S,
                    RIP_RETRIES, RIP_SHORT_TOLERANCE, RIP_SPEED_LIMIT)

_PROGRESS_HINT = re.compile(rb"\[|\]|%")
_SMILEY = re.compile(rb":-?\)|:-\||:-/|:-P|8-\||:-0|:-\(|;\(-\)|8-X|:\^D")


class RipperBase(threading.Thread):
    """抓轨器接口 + 优先级调度。子类只需实现 _rip_one()。"""

    def __init__(self, disc: Disc, cache: Cache, name: str = "ripper",
                 mode: str | None = None, clock=None):
        super().__init__(name=name, daemon=True)
        self.disc = disc
        self.cache = cache
        self.mode = mode if mode in RIP_MODES else RIP_MODE
        self._lock = threading.RLock()
        self._stop_event = threading.Event()     # 不能叫 _stop：会盖掉 Thread._stop()，join() 会炸
        self._pause_event = threading.Event()    # Stop 暂停缓存：挂起线程并杀掉抓轨子进程
        # pause 次数只增不减：resume 清掉事件，但不能抹掉旧 _rip_one 的取消原因。
        self._pause_generation = 0
        self.retry_delay = 0.5                   # 失败后歇一下再重试（测试里可设 0）
        self._prio: int | None = None           # effective：真正允许抢占的优先级
        self._pending_prio: int | None = None   # 稳定窗口内待提交的目标
        self._pending_at = 0.0
        self._clock = clock or time.monotonic
        self._failed: set[int] = set()
        self._attempts: dict[int, int] = {}      # 每个轨道已经失败过几次（只在这里计数）
        self._errors: dict[int, str] = {}        # 每个轨道最后一次失败原因
        self._preserve_part = False               # 停止时保留 .pcm.part（弹盘继续播缓存）
        self._proc: subprocess.Popen | None = None
        self.status: dict = {
            "track": None, "stage": "idle", "phase": "", "pct": 0.0, "speed": 0.0,
            "errors": {"V": 0, "!": 0, "e": 0, "+": 0}, "smile": "", "msg": "",
            "map": [0] * 20,
        }

    # ── 对外接口 ──────────────────────────────────────────────────────────
    def want(self, number: int) -> None:
        """立即提交最想要的轨；首播、自动下一轨、已缓存命中走这里。"""
        self.request_priority(number, immediate=True)

    def request_priority(self, number: int, immediate: bool = False) -> None:
        """请求抓轨优先级；默认 300ms trailing-edge 防抖。

        相同 pending 的重复请求不刷新截止时间，因此 30 FPS 的
        ``App._tick()`` 不会把窗口无限推迟。只有目标真正变化才重新计时；
        若用户又选回当前 effective 轨，则直接取消 pending。
        """
        now = self._clock()
        with self._lock:
            if immediate:
                self._pending_prio = None
                self._pending_at = 0.0
                self._prio = number
                return
            if number == self._pending_prio:
                return
            if number == self._prio:
                self._pending_prio = None
                self._pending_at = 0.0
                return
            self._pending_prio = number
            self._pending_at = now + RIP_PRIO_DEBOUNCE_S

    def cancel_priority_request(self) -> None:
        """Stop / Eject / 换盘：取消尚未提交的目标，取消后不会晚到生效。"""
        with self._lock:
            self._pending_prio = None
            self._pending_at = 0.0

    def _commit_pending(self, now: float | None = None) -> bool:
        """在调度安全点提交已稳定的 pending；返回是否发生提交。"""
        now = self._clock() if now is None else now
        with self._lock:
            if (self._pending_prio is None
                    or now < self._pending_at):
                return False
            self._prio = self._pending_prio
            self._pending_prio = None
            self._pending_at = 0.0
            return True

    def _effective_priority(self) -> int | None:
        self._commit_pending()
        with self._lock:
            return self._prio

    def _pending_wait(self, default: float) -> float:
        """空闲/select 的最长等待：pending 到期时提前醒来提交，不做忙轮询。"""
        with self._lock:
            if self._pending_prio is None:
                return default
            remaining = self._pending_at - self._clock()
        return max(0.0, min(default, remaining))

    def pause(self) -> None:
        """暂停缓存并立即停下抓轨子进程（Stop 键）。

        线程本身保留，Space 恢复播放时 ``resume()`` 继续抓；已完成的
        ``.pcm`` 不动，进行中的 ``.part`` 由 _rip_one 收尾时丢掉，恢复后
        从该轨开头重抓。

        每次 pause 都递增 ``_pause_generation``：即使 Space 立刻 resume
        清掉了 ``_pause_event``，旧 _rip_one 仍能从代数变化知道自己是被
        主动取消的，不会把 kill 产生的 rc=-9 记成坏盘失败。
        """
        with self._lock:
            self._pause_generation += 1
            self._pause_event.set()
        self.cancel_priority_request()
        self.kill_child()
        self._update(track=None, stage="paused", phase="", pct=0.0, speed=0.0)

    def resume(self) -> None:
        """恢复被 ``pause()`` 挂起的缓存；未启动/未暂停时只清标志。"""
        if not self._pause_event.is_set():
            return
        self._pause_event.clear()
        self._update(track=None, stage="idle", phase="", pct=0.0, speed=0.0)

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def _pause_generation_snapshot(self) -> int:
        with self._lock:
            return self._pause_generation

    def _pause_cancelled(self, generation: int) -> bool:
        """这次尝试是否在它执行期间被 pause 取消（resume 不清除）。"""
        with self._lock:
            return self._pause_generation != generation

    def stop(self, preserve_part: bool = False) -> None:
        """停止抓轨。

        preserve_part=True 时保留 `.pcm.part`：弹盘后播放器还要继续放这段
        已缓冲音频（--on-eject continue），不能把文件删掉。
        """
        self._preserve_part = preserve_part
        self.cancel_priority_request()
        self._pause_event.clear()
        self._stop_event.set()
        self.kill_child()

    def stop_and_join(self, timeout: float = 10.0,
                      preserve_part: bool = False) -> bool:
        """停止并等线程真正退出，返回是否已退出。

        换盘/退出时必须等旧线程收尾：它可能还在 unlink 半成品 `.pcm.part`，
        新抓轨器已经 reset_track() 写了同路径 —— 会互相删文件。返回 False
        表示超时仍未退出，调用方不应启动新抓轨器。
        """
        self.stop(preserve_part=preserve_part)
        if self is threading.current_thread():
            return False
        if self.is_alive():
            self.join(timeout)
        return not self.is_alive()

    def kill_child(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def is_done(self, number: int) -> bool:
        if self.cache.is_done(self.disc, number):
            return True
        # 崩溃窗口：rename 已完成但 .done 还没落盘 —— 大小完全正确就算完成，
        # 不要为了一张缺失的标记把整轨重抓一遍。
        return self.cache.pcm_size(self.disc, number) == self.disc.track(number).pcm_bytes

    def is_failed(self, number: int) -> bool:
        with self._lock:
            return number in self._failed

    def failure_reason(self, number: int) -> str:
        """这一轨最后一次失败的原因。"""
        with self._lock:
            return self._errors.get(number, "")

    def attempts_of(self, number: int) -> int:
        """这一轨已经失败过几次（0 = 还没失败过）。"""
        with self._lock:
            return self._attempts.get(number, 0)

    def retry(self, number: int) -> None:
        """清掉某一轨的失败记录，让 _pick() 重新抓它（用户主动重试）。"""
        with self._lock:
            self._failed.discard(number)
            self._attempts.pop(number, None)
            self._errors.pop(number, None)
            if self.status.get("track") in (None, number):  # 界面上的 retry 计数别停在旧值
                self.status["errors"]["e"] = 0
                self.status["msg"] = ""

    def available(self, number: int) -> int:
        return min(self.cache.available_bytes(self.disc, number), self.disc.track(number).pcm_bytes)

    def snapshot(self) -> dict:
        with self._lock:
            st = dict(self.status)
            st["errors"] = dict(st["errors"])
            st["map"] = list(st.get("map") or [])
            return st

    # ── 内部 ──────────────────────────────────────────────────────────────
    def _update(self, **kw) -> None:
        with self._lock:
            self.status.update(kw)

    def _bump_error(self, sym: str) -> None:
        with self._lock:
            errs = self.status["errors"]
            if sym in errs:
                errs[sym] += 1

    def _begin_attempt(self, number: int, speed: float = 0.0) -> None:
        """开始一次新的进程级抓轨。

        清掉本轮 cdparanoia 的 V/!/+ 计数，但把 e 保留为「这一轨已失败过的次数」，
        UI 的 retry 显示的才是真实重试数。
        """
        with self._lock:
            errs = self.status["errors"]
            for sym in ("V", "!", "+"):
                errs[sym] = 0
            errs["e"] = self._attempts.get(number, 0)
            self.status.update(track=number, stage="ripping", phase="",
                               pct=0.0, speed=speed, msg="", smile="",
                               map=[0] * 20)

    def _candidates(self, prio: int | None) -> list[int]:
        """这一轮允许抓哪些轨 —— 由 RIP_MODE 决定硬盘写入范围。

        disc: 整盘（当前轨之后 → 回头补前面）
        next: 当前轨 + 下一轨（默认；换轨不卡，又不白写整盘）
        track: 只抓当前轨
        还没 want() 过时从第一轨开始，保持「插盘先抓第一轨」的直觉。
        """
        numbers = [t.number for t in self.disc.tracks]
        if not numbers:
            return []
        if self.mode == "disc":
            if prio in numbers:
                i = numbers.index(prio)
                return numbers[i:] + numbers[:i]
            return numbers
        span = 1 if self.mode == "track" else 2
        if prio not in numbers:
            return numbers[:span]
        i = numbers.index(prio)
        return numbers[i:i + span]

    def _pick(self) -> int | None:
        self._commit_pending()
        with self._lock:
            prio = self._prio
            waiting = self._pending_prio is not None
        if waiting:
            # 防抖窗口内不新开预读轨：现有 _rip_one 要么继续，要么自然结束，
            # 等目标稳定后再提交，避免为中间目标制造一次寻道。
            return None
        for n in self._candidates(prio):
            if n in self._failed or self.is_done(n):
                continue
            return n
        return None

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._update(track=None, stage="paused", phase="",
                             pct=0.0, speed=0.0)
                self._stop_event.wait(0.05)      # resume() 最多 50ms 内被看到
                continue
            nxt = self._pick()
            if nxt is None:
                self._update(track=None, stage="idle", phase="",
                             pct=0.0, speed=0.0)
                self._stop_event.wait(self._pending_wait(0.25))
                continue
            attempt_gen = self._pause_generation_snapshot()
            try:
                self.cache.ensure_dir(self.disc)     # 建目录只在真要写盘时做
                self._rip_one(nxt)
            except Exception as exc:                     # 抓轨失败不能带崩 UI
                if self._stop_event.is_set():
                    break
                if self._pause_cancelled(attempt_gen):
                    # pause() 杀子进程会让 read/write 抛异常，甚至 resume()
                    # 已经清掉 pause 标志；按代数判断，这不是坏盘。
                    self._update(track=None,
                                 stage="paused" if self._pause_event.is_set()
                                 else "idle",
                                 phase="", pct=0.0, speed=0.0)
                    continue
                if self._pause_event.is_set():
                    self._update(track=None, stage="paused", phase="",
                                 pct=0.0, speed=0.0)
                    continue
                self._record_failure(nxt, exc)
                self._stop_event.wait(self.retry_delay)

    def _record_failure(self, number: int, exc: Exception) -> None:
        """失败只在这里计数（_rip_one 内不再 +1），避免一次失败被记两次。"""
        self.kill_child()                                # 异常路径上别把子进程漏下
        with self._lock:
            tries = self._attempts.get(number, 0) + 1
            self._attempts[number] = tries
            self._errors[number] = str(exc)
            give_up = tries >= RIP_ATTEMPTS
            if give_up:
                self._failed.add(number)
            self.status["errors"]["e"] = tries           # UI 的 retry 显示真实失败次数
        self._update(track=number, pct=0.0, speed=0.0, phase="",
                     stage="failed" if give_up else "error",
                     msg=f"{exc}（{tries}/{RIP_ATTEMPTS} 次，{'已放弃' if give_up else '将重试'}）")

    def _rip_one(self, number: int) -> None:             # pragma: no cover
        raise NotImplementedError


class Ripper(RipperBase):
    """真正的光驱抓轨：cdparanoia 的 raw PCM 直接从 stdout 流进缓存文件。"""

    def __init__(self, disc: Disc, cache: Cache, device: str,
                 fast: bool | None = None, mode: str | None = None,
                 clock=None):
        super().__init__(disc, cache, name="ripper", mode=mode, clock=clock)
        self.device = device
        self.fast = RIP_FAST if fast is None else fast

    def _cmd(self, number: int) -> list[str]:
        # 注意三个坑（都在真光驱上验证过）：
        #   1) "-z 5" 会被 getopt 当成 span=5，必须写成 "-z5"
        #   2) 不给 outfile 时 cdparanoia 会写默认文件 cdda.raw，不会走 stdout；
        #      要 stdout 必须显式给 "-"
        #   3) 必须用 -r（raw little-endian）而不是 -p：这台机器的 -p 给的是大端，
        #      当小端播放就是一片沙沙声
        cmd = ["cdparanoia", "-d", self.device, "-e", "-r", f"-z{RIP_RETRIES}"]
        if self.fast:
            cmd.append("-Y")             # 只做 overlap 校验，明显更快
        if RIP_SPEED_LIMIT:
            cmd += ["-S", str(RIP_SPEED_LIMIT)]
        cmd += [str(number), "-"]        # span, outfile=stdout
        return cmd

    def _drain_stderr(self, proc: subprocess.Popen, number: int) -> None:
        buf = b""
        while True:
            try:
                chunk = proc.stderr.read(4096)           # type: ignore[union-attr]
            except (ValueError, OSError):
                break
            if not chunk:
                break
            buf += chunk
            while True:
                m = re.search(rb"[\r\n]", buf)
                if not m:
                    break
                seg, buf = buf[: m.start()], buf[m.end():]
                self._score(seg)
        if buf:
            self._score(buf)

    # cdparanoia -e 的进度格式： `##: <code> [<stage>] @ <byte offset>`
    # 正常阶段只有 read / verify / overlap / wrote / finished；
    # 其它阶段名（scratch / skip / repair / backoff / drift / jitter / fixup…）才算“在抢救”。
    _PROGRESS = re.compile(
        rb"^##:\s*(-?\d+)\s+\[([a-z_]+)\](?:\s+@\s*(\d+))?")
    _NORMAL_STAGES = {b"read", b"verify", b"overlap", b"wrote", b"finished"}
    _PHASES = {b"read": "read", b"verify": "verify", b"overlap": "overlap"}
    _HOT_STAGES = {b"scratch", b"skip", b"repair", b"backoff", b"drift", b"jitter",
                   b"fixup_edge", b"fixup_overlap", b"fixup_gap", b"fixup_atom", b"damaged"}

    def _score(self, seg: bytes) -> None:
        if not seg:
            return
        m = self._PROGRESS.match(seg)
        if m:
            stage = m.group(2)
            if stage in self._PHASES:
                self._update(phase=self._PHASES[stage])
                return
            if stage in (b"wrote", b"finished"):
                self._update(phase="")
                return
            if stage not in self._NORMAL_STAGES:
                key = "V" if stage in (b"skip", b"damaged") else ("!" if stage == b"scratch" else "+")
                self._bump_error(key)
                if m.group(3):
                    self._mark_error(int(m.group(3)))
                self._update(smile=stage.decode("ascii", "replace"))
            return
        sm = _SMILEY.search(seg)
        if sm:
            self._update(smile=sm.group(0).decode("ascii", "replace"))

    def _mark_error(self, offset: int) -> None:
        """把一次坏道事件落到 20 格坏道 map 上（按轨内字节位置）。"""
        with self._lock:
            number = self.status.get("track")
            if not isinstance(number, int):
                return
            try:
                expected = self.disc.track(number).pcm_bytes
            except KeyError:
                return
            if expected <= 0:
                return
            bucket = max(0, min(19, offset * 20 // expected))
            marks = self.status.setdefault("map", [0] * 20)
            if len(marks) != 20:
                marks[:] = (list(marks) + [0] * 20)[:20]
            marks[bucket] += 1

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        """cdparanoia 的 stdout/stderr 管道必须显式关，否则每失败一次漏两个 fd。"""
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _rip_one(self, number: int) -> None:
        if self._stop_event.is_set() or self._pause_event.is_set():
            return
        attempt_gen = self._pause_generation_snapshot()
        track = self.disc.track(number)
        expected = track.pcm_bytes
        part = self.cache.part_path(self.disc, number)
        self.cache.reset_track(self.disc, number)
        self._begin_attempt(number)

        try:
            proc = subprocess.Popen(self._cmd(number), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            if self._pause_cancelled(attempt_gen):
                return
            raise RuntimeError(f"启动 cdparanoia 失败: {exc}") from exc

        with self._lock:
            self._proc = proc
        if (self._stop_event.is_set() or self._pause_event.is_set()
                or self._pause_cancelled(attempt_gen)):
            # pause() 恰好卡在 Popen 前后：马上收掉刚起的进程，别让光驱继续转。
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._close_pipes(proc)
            return
        drain = threading.Thread(target=self._drain_stderr, args=(proc, number), daemon=True)
        drain.start()
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            written = 0
            t0 = time.monotonic()
            last_report = 0.0
            aborted = False
            try:
                while (written < expected - 32
                       and not self._stop_event.is_set()
                       and not self._pause_event.is_set()
                       and not self._pause_cancelled(attempt_gen)):
                    prio = self._effective_priority()
                    # 只有当「正在播的那轨还没抓完」时才抢占；否则让顺序预取安心跑完。
                    # 那轨已经抓轨失败就别抢了：抢过来也抓不出数据，反而会把
                    # 「放弃 → 换下一轨 → 立刻被抢回去」变成空转（真机上会狂转光驱）。
                    if (prio is not None and prio != number
                            and not self.is_done(prio)
                            and not self.is_failed(prio)):
                        aborted = True
                        break
                    ready, _, _ = select.select(
                        [proc.stdout], [], [], self._pending_wait(0.25))
                    if not ready:
                        if proc.poll() is not None:
                            break
                        continue
                    chunk = proc.stdout.read(65536) or b""     # type: ignore[union-attr]
                    if not chunk:
                        break
                    os.write(fd, chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if now - last_report > 0.2:
                        last_report = now
                        el = max(1e-6, now - t0)
                        self._update(track=number,
                                     pct=min(100.0, 100.0 * written / expected),
                                     speed=written / el / BYTES_PER_SEC)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

            # 收尾：正常完成时让 cdparanoia 自己退出，别一上来就 kill（会得到 rc=-9）
            if (aborted or self._stop_event.is_set()
                    or self._pause_event.is_set()
                    or self._pause_cancelled(attempt_gen)):
                proc.kill()
                proc.wait(timeout=10)
                if not self._preserve_part:
                    try:
                        part.unlink()
                    except OSError:
                        pass
                self._update(stage="idle", phase="", track=None,
                             pct=0.0, speed=0.0)
                return

            try:
                rc = proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait(timeout=5)
        except Exception:
            if self._pause_cancelled(attempt_gen):
                try:
                    part.unlink()
                except OSError:
                    pass
                self._update(stage="idle", phase="", track=None,
                             pct=0.0, speed=0.0)
                return
            raise
        finally:
            # 异常路径（os.open/os.write/select 出错）也必须收掉子进程：
            # 这里先 kill+wait，run()/_record_failure 的 kill_child 才不会扑空。
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._close_pipes(proc)            # stdout/stderr 管道不关会漏 fd
            drain.join(timeout=0.2)

        if self._pause_cancelled(attempt_gen):
            # pause()+resume() 交错时旧流可能已经正常/被 kill 结束；不 finalize、
            # 不记失败，交给 run() 的下一轮重新抓。
            try:
                part.unlink()
            except OSError:
                pass
            self._update(stage="idle", phase="", track=None,
                         pct=0.0, speed=0.0)
            return

        self._update(pct=100.0, speed=written / max(1e-6, time.monotonic() - t0) / BYTES_PER_SEC)
        if rc == 0 and written >= expected - RIP_SHORT_TOLERANCE:
            if not self.cache.finalize_track(self.disc, number, tolerate=RIP_SHORT_TOLERANCE):
                raise RuntimeError("抓轨完成但缓存落盘校验失败（PCM 不完整）")
            self._update(stage="done", phase="")
            return
        if rc == 0 and written == 0:
            raise RuntimeError("cdparanoia 返回成功但没有输出 PCM（outfile 没走 stdout？）")

        # 失败：丢掉半成品，等下一轮重试（计数与放弃判断统一由 run()/_record_failure 做）
        try:
            part.unlink()
        except OSError:
            pass
        raise RuntimeError(f"cdparanoia rc={rc}，已写入 {written / expected * 100:.1f}%")


class FakeRipper(RipperBase):
    """合成音频冒充光驱，用于 --fake / 单元测试；按 speed 倍速慢慢写。"""

    def __init__(self, disc: Disc, cache: Cache, speed: float = 8.0,
                 mode: str | None = None, clock=None):
        super().__init__(disc, cache, name="fake-ripper", mode=mode,
                         clock=clock)
        self.speed = speed
        self.synth = MusicSynth()

    def _rip_one(self, number: int) -> None:
        if self._stop_event.is_set() or self._pause_event.is_set():
            return
        attempt_gen = self._pause_generation_snapshot()
        track = self.disc.track(number)
        total = track.pcm_bytes
        part = self.cache.part_path(self.disc, number)
        self.cache.reset_track(self.disc, number)
        self._begin_attempt(number, speed=self.speed)
        frames_per_chunk = FEED_CHUNK // BYTES_PER_FRAME
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        written = 0
        start_frame = 0
        t0 = time.monotonic()
        try:
            while (written < total
                   and not self._stop_event.is_set()
                   and not self._pause_event.is_set()
                   and not self._pause_cancelled(attempt_gen)):
                prio = self._effective_priority()
                if (prio is not None and prio != number
                        and not self.is_done(prio) and not self.is_failed(prio)):
                    os.close(fd)
                    part.unlink(missing_ok=True)
                    self._update(stage="idle", phase="", track=None, pct=0.0)
                    return
                n = min(frames_per_chunk, (total - written) // BYTES_PER_FRAME + 1)
                if n <= 0:
                    break
                audio = self.synth.render(number, start_frame, n)
                pcm = np.clip(audio * 32767.0, -32768, 32767).astype("<i2").tobytes()
                if written + len(pcm) > total:
                    pcm = pcm[: total - written]
                os.write(fd, pcm)
                written += len(pcm)
                start_frame += n
                if self.speed < 1e6:
                    target = written / (BYTES_PER_SEC * self.speed)
                    lag = target - (time.monotonic() - t0)
                    if lag > 0:
                        time.sleep(self._pending_wait(min(lag, 0.1)))
                self._update(track=number, pct=min(100.0, 100.0 * written / total), speed=self.speed)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

        cancelled = self._pause_cancelled(attempt_gen)
        if self._stop_event.is_set() or self._pause_event.is_set() or cancelled:
            if self._pause_event.is_set() or cancelled:
                try:
                    part.unlink()
                except OSError:
                    pass
                self._update(stage="idle", phase="", track=None,
                             pct=0.0, speed=0.0)
            return
        if not self.cache.finalize_track(self.disc, number):
            raise RuntimeError("合成抓轨落盘校验失败（PCM 不完整）")
        self._update(pct=100.0, stage="done", phase="")
