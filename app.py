"""装配与主循环：盘 → 缓存 → 抓轨 → 播放 → 可视化 → 终端。"""
from __future__ import annotations

import random
import time

from app_state import TrayPhase, TrayState
from audio.engine import Engine
from audio.media import TrackMedia
from cdda import cdtext
from cdda.cache import Cache, cache_root_for
from cdda.device import (CddaError, NoMediaError, UnreadableDiscError,
                        UnsupportedDiscError, query_disc, toc_matches)
from cdda.discmon import (DiscMonitor, TRAY_CLOSED_DISC, TRAY_CLOSED_EMPTY,
                          TRAY_OPEN, drive_tray_state)
from cdda.fake import build_fake_disc
from cdda.loader import DiscLoader as _DiscLoader
from cdda.ripper import FakeRipper, Ripper
from cdda.session import DiscSession
from config import (BYTES_PER_SEC, DEFAULT_THEME, DISC_POLL_S, FPS,
                    INSERT_ACTION, PREROLL_S, RIP_MODE, RIP_MODES,
                    SEEK_DEBOUNCE_S, SEEK_PULSE_S, STATE_PULSE_S, THEMES)
from dsp.analyzer import Analyzer, VisFrame
import prefs
from services import metadata
from ui.boot import (INTRO_NAMES, MIN_H, MIN_W, READY_HOLD, BootInfo,
                     render_frame, timeline)
from ui.feedback import TrackSnapshot, TrackTransition, VisualFeedback
from ui.glyphs import RESET, fg, fit, text_width
from ui.layouts import UIState, render
from ui.no_disc import render as render_no_disc
from ui.screen import KeyReader, Screen
from ui.transition import squash as squash_rows

LAYOUTS = ("dash", "mini")          # 两种基础布局；l 键循环，以后加层就挂这里
TRAY_JOIN_S = 0.05                  # 托盘路径等抓轨线程的短超时，不让 UI 卡住


class App:
    def __init__(self, args):
        self.args = args
        # 配色：命令行 > 上次退出时记住的 > 默认（phosphor）
        self.prefs = prefs.load()
        saved = self.prefs.get("theme")
        arg_theme = getattr(args, "theme", None)
        self.theme_name = (arg_theme if arg_theme in THEMES
                           else saved if saved in THEMES
                           else DEFAULT_THEME)
        layout = getattr(args, "layout", "dash")
        if getattr(args, "minimal", False):
            layout = "mini"
        if layout not in LAYOUTS:
            layout = "dash"
        self.layout = layout
        self.help = False                # h 键打开的帮助布局；任意键退出
        self.screen = Screen()
        self.keys = KeyReader()
        self.analyzer = Analyzer()
        self.engine: Engine | None = None
        # 盘片会话：当前 Disc + Cache + Ripper + 抓轨/元数据代数。
        self.session = DiscSession(
            args,
            metadata_module=metadata,
            cdtext_module=cdtext,
            toc_matcher=lambda device, disc: toc_matches(device, disc),
        )
        self.track_no = 1
        self.volume = args.volume
        mode = getattr(args, "rip_mode", None)
        self.rip_mode = mode if mode in RIP_MODES else RIP_MODE
        self.running = True
        self.message = ""
        self._message_until = 0.0
        self._autopaused = False
        self._autopause_since = 0.0      # 进入自动缓冲的 monotonic 时间；0 = 未在缓冲
        self._boot_paused = bool(getattr(args, "paused", False))
        self._wait_disc = bool(getattr(args, "wait_disc", False))
        # 开仓一律停歌；旧的 continue 弹盘策略已取消
        self.eject_action = "stop"
        self.insert_action = getattr(args, "on_insert", None) or INSERT_ACTION
        # 默认也把「启动/等待插盘」当成一次插入：pause 就停在 stopped
        self._boot_pause_prompt = (self._boot_paused
                                   or self.insert_action != "play")
        self.tray = TrayState()
        self.monitor = None
        self._last_dash_rows: list[str] | None = None
        self._ripper_stuck = False       # 旧抓轨线程 join 超时，暂缓换盘
        self.hint = ""
        self._spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._spin_i = 0
        # 待机动画：停止/空闲时示波器与频谱切到正弦+扫描；暂停保持冻结。
        now = time.monotonic()
        self._scope_mix = 1.0 if self._boot_paused else 0.0
        self._idle_target = self._scope_mix
        self._idle_from = self._scope_mix
        self._idle_t0 = now
        self._idle_dur = 0.55 if self._boot_paused else 0.45
        self._idle_start = now
        self._idle_t = 0.0
        self._frozen_vis = VisFrame()      # 暂停时复用的最后一帧
        self.feedback = VisualFeedback()   # 切轨/Seek/Pause 的短命视觉状态
        # 连续 Seek 的 trailing-edge 合并：UI 先走 preview，窗口稳定后只提交
        # 一次真实 seek_to。时间统一走 _clock，测试可注入可控时钟。
        self._clock = None            # 测试可换成可控时钟；None = time.monotonic
        self._pending_seek_target: float | None = None
        self._pending_seek_deadline = 0.0
        self._pending_seek_origin = 0.0
        self._pending_seek_track: int | None = None
        self._pending_seek_discid: str | None = None

    def _now(self) -> float:
        """App 的单调时钟；测试替换 ``_clock`` 即可冻结/推进时间。"""
        clock = getattr(self, "_clock", None)
        return float(clock()) if clock is not None else time.monotonic()

    def _get_feedback(self) -> VisualFeedback:
        """测试常手工 App.__new__；缺协调器时按需补一个。"""
        feedback = getattr(self, "feedback", None)
        if feedback is None:
            feedback = VisualFeedback()
            self.feedback = feedback
        return feedback

    @property
    def _track_transition(self) -> TrackTransition | None:
        return self._get_feedback().transition

    @_track_transition.setter
    def _track_transition(self, value: TrackTransition | None) -> None:
        self._get_feedback().transition = value

    # ── 主题 ──────────────────────────────────────────────────────────────
    @property
    def theme(self) -> dict:
        return THEMES[self.theme_name]

    # ── 当前盘资源（全部委托给 DiscSession） ───────────────────────────────
    def _get_session(self) -> DiscSession:
        """测试常用 App.__new__ 手工搭状态；缺 session 时补一个空会话。"""
        session = getattr(self, "session", None)
        if session is None:
            session = DiscSession(
                getattr(self, "args", None),
                metadata_module=metadata,
                cdtext_module=cdtext,
                toc_matcher=lambda device, disc: toc_matches(device, disc),
            )
            self.session = session
        return session

    def _get_tray(self) -> TrayState:
        tray = getattr(self, "tray", None)
        if tray is None:
            tray = TrayState()
            self.tray = tray
        return tray

    @property
    def disc(self):
        return self._get_session().disc

    @disc.setter
    def disc(self, value) -> None:
        self._get_session().disc = value

    @property
    def cache(self) -> Cache | None:
        return self._get_session().cache

    @cache.setter
    def cache(self, value: Cache | None) -> None:
        self._get_session().cache = value

    @property
    def ripper(self) -> Ripper | FakeRipper | None:
        return self._get_session().ripper

    @ripper.setter
    def ripper(self, value: Ripper | FakeRipper | None) -> None:
        self._get_session().ripper = value

    @property
    def _disc_seq(self) -> int:
        return self._get_session().seq

    @_disc_seq.setter
    def _disc_seq(self, value: int) -> None:
        self._get_session().seq = value

    @property
    def _metadata_seq(self) -> int:
        return self._get_session().metadata_seq

    @_metadata_seq.setter
    def _metadata_seq(self, value: int) -> None:
        self._get_session().metadata_seq = value

    @property
    def _caching(self) -> bool:
        return self._get_session().started

    @_caching.setter
    def _caching(self, value: bool) -> None:
        self._get_session().started = value

    # ── 托盘/装载视图（委托给 TrayState） ─────────────────────────────────
    @property
    def tray_ui(self) -> str:
        return self._get_tray().ui

    @tray_ui.setter
    def tray_ui(self, value: str) -> None:
        self._get_tray().phase = TrayPhase(value)

    @property
    def disc_present(self) -> bool:
        return self._get_tray().disc_present

    @disc_present.setter
    def disc_present(self, value: bool) -> None:
        self._get_tray().disc_present = value

    @property
    def disc_state(self) -> str:
        return self._get_tray().disc_state

    @disc_state.setter
    def disc_state(self, value: str) -> None:
        self._get_tray().disc_state = value

    @property
    def tray_state(self) -> str:
        return self._get_tray().tray

    @tray_state.setter
    def tray_state(self, value: str) -> None:
        self._get_tray().tray = value

    @property
    def _tray_since(self) -> float:
        return self._get_tray().since

    @_tray_since.setter
    def _tray_since(self, value: float) -> None:
        self._get_tray().since = value

    @property
    def _last_disc(self):
        return self._get_tray().last_disc

    @_last_disc.setter
    def _last_disc(self, value) -> None:
        self._get_tray().last_disc = value

    @property
    def _tray_transition(self) -> dict | None:
        return self._get_tray().transition

    @_tray_transition.setter
    def _tray_transition(self, value: dict | None) -> None:
        self._get_tray().transition = value

    @property
    def _tray_ready_at(self) -> float:
        return self._get_tray().ready_at

    @_tray_ready_at.setter
    def _tray_ready_at(self, value: float) -> None:
        self._get_tray().ready_at = value

    def _flash(self, text: str, seconds: float = 2.0) -> None:
        self.message = text
        self._message_until = self._now() + seconds

    def _clear_message(self) -> None:
        """换盘事件发生时先清掉上一张盘的提示，别让「等待新盘」粘住。"""
        self.message = ""
        self._message_until = 0.0

    def _set_tray(self, tray: str, since: float | None = None) -> None:
        """记录托盘状态边沿；since 由 DiscMonitor 提供，保证动画起点一致。"""
        self._get_tray().set_tray(tray, since)

    def _set_autopaused(self, on: bool) -> None:
        """记录自动缓冲的起止：界面用 autopause_since 画倒计时/水位。"""
        if on and not self._autopaused:
            self._autopause_since = self._now()
        elif not on:
            self._autopause_since = 0.0
        self._autopaused = on

    # ── 启动 ──
    def _save_theme(self) -> None:
        """每次换配色就落盘：窗口被合成器关掉也记得住。"""
        self.prefs["theme"] = self.theme_name
        prefs.save(self.prefs)

    def _draw_wait(self, msg: str, title: str = "等待光盘…",
                   hint: str = "插入 CD 后自动开始（q 退出）") -> None:
        """没盘/介质不支持时的纯文字启动画面；插盘后自动继续。"""
        w, h = self.screen.size()
        theme = self.theme
        lines = [
            "  " + fg(theme["accent"]) + "♪ asciicd" + RESET,
            "",
            "  " + fg(theme["text"]) + title + RESET,
            "  " + fg(theme["dim"]) + hint + RESET,
            "",
            "  " + fg(theme["dim"]) + msg + RESET,
        ]
        top = max(0, (h - len(lines)) // 2)
        centred = [" " * max(0, (w - text_width(line)) // 2) + line for line in lines]
        rows = [""] * top + [fit(line, w) for line in centred]
        rows += [""] * max(0, h - len(rows))
        self.screen.draw(rows[:h])

    def _open_disc(self):
        """打开光盘；--wait-disc 时停在等待画面，定期重试而不是直接退出。"""
        while True:
            try:
                return (build_fake_disc() if self.args.fake
                        else query_disc(self.args.device))
            except UnsupportedDiscError as exc:
                if not self._wait_disc:
                    raise
                # 小窗口兜底：明确区别于 NO DISC；等托盘动过再重试，
                # 不在同一张数据盘上反复跑 cdparanoia。
                self._draw_wait(str(exc), "不支持的介质",
                                "只支持 CD-DA 音频 CD；换盘后自动重试（q 退出）")
                for key in self.keys.read():
                    if key in ("q", "quit"):
                        self.running = False
                        raise KeyboardInterrupt
                if not self._wait_tray_change():
                    raise KeyboardInterrupt
            except UnreadableDiscError as exc:
                if not self._wait_disc:
                    raise
                self._draw_wait(str(exc), "无法读取光盘",
                                "光驱有盘但认不出 CD-DA；换盘后自动重试（q 退出）")
                for key in self.keys.read():
                    if key in ("q", "quit"):
                        self.running = False
                        raise KeyboardInterrupt
                if not self._wait_tray_change():
                    raise KeyboardInterrupt
            except NoMediaError as exc:
                if not self._wait_disc:
                    raise
                self._draw_wait(str(exc))
                for key in self.keys.read():
                    if key in ("q", "quit"):
                        self.running = False
                        raise KeyboardInterrupt
                time.sleep(1.2)
            except CddaError as exc:
                if not self._wait_disc:
                    raise
                self._draw_wait(str(exc), "读取光盘失败",
                                "正在重试；换盘或确认盘面后自动继续（q 退出）")
                for key in self.keys.read():
                    if key in ("q", "quit"):
                        self.running = False
                        raise KeyboardInterrupt
                time.sleep(1.2)

    def _wait_tray_change(self, timeout: float = 30.0) -> bool:
        """小窗口兜底：等托盘离开 closed-disc 再重试；q 返回 False。"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for key in self.keys.read():
                if key in ("q", "quit"):
                    self.running = False
                    return False
            tray = drive_tray_state(self.args.device)
            if tray in (TRAY_OPEN, TRAY_CLOSED_EMPTY):
                return True
            time.sleep(0.1)
        return True

    def _reset_idle_clock(self) -> None:
        """开机画面交棒时把待机相位归零，第一遍扫描从最左端开始。"""
        now = time.monotonic()
        self._idle_start = now
        self._idle_t = 0.0
        self._idle_t0 = now
        self._idle_from = self._scope_mix
        self._idle_target = 1.0 if self._scope_mix > 0.5 else 0.0
        self._idle_dur = 0.55 if self._idle_target else 0.45

    def setup(self) -> None:
        self.screen.enter()
        if self._boot_sequence():
            self._reset_idle_clock()
            return
        # 终端太小时不画面板：退回原来的纯文字启动
        self._boot_frame("读取光盘目录…")
        disc = self._open_disc()
        self._prepare_after_disc(disc, hold=False)
        self._reset_idle_clock()

    def _prepare_after_disc(self, disc, hold: bool) -> None:
        """TOC 到手后的装配；hold=True 时先不发声，等开机画面交棒。

        这里只做本地准备工作（缓存目录、轨道号、音源），**不启动抓轨线程，
        也不查 CD-TEXT/MusicBrainz**。插盘后的启动阶段只读 TOC 和本地缓存，
        真正开始缓存歌曲要等用户按 Space（或 --on-insert play 自动开播）。
        """
        cache = self._install_cache(disc)
        ripper = self._make_ripper(disc, cache)
        self.session.prepare(disc, cache, ripper)   # 只建线程对象，不 start

        self.engine = Engine(on_audio=self.analyzer.push, on_state=self._on_engine_state)
        self.engine.set_volume(self.volume)
        self.track_no = self._valid_start_number()
        # 开机动画期间一定先 cue；交棒时若允许自动播放再真正开播。
        start_paused = (hold or self._boot_paused
                        or self.insert_action != "play")
        self._start_track(self.track_no, paused=start_paused)
        self._get_tray().enter(TrayPhase.MAIN, present=True, state="ready")
        self._set_tray(TRAY_CLOSED_DISC)
        self._start_disc_monitor()

    def _cached_count(self, disc) -> int:
        return self.session.cached_count()

    def _start_caching(self) -> None:
        """第一次真正开播时才启动抓轨线程和元数据查询。

        插盘/启动阶段只读了 TOC 和本地缓存；在用户按 Space（或自动开播）
        之前不碰光驱、不跑 cdparanoia、不发 MusicBrainz 请求。按过 Stop 后
        抓轨器仍在（被 pause 挂起），这里先恢复再按需启动。
        """
        session = self.session
        if self.ripper is not None:
            self.ripper.resume()
        if not session.ensure_ripper(self.track_no):
            return
        if session.claim_metadata():
            self._start_metadata_lookup()

    def _begin_playback(self) -> None:
        """从启动停止态开始：先懒启动缓存，再开音频输出。"""
        self._start_caching()
        if self.engine is not None and self.engine.state in (
                "stopped", "paused", "buffering"):
            self.engine.play()
        self._boot_pause_prompt = False
        self._set_autopaused(False)

    def _boot_sequence(self) -> bool:
        """开机动画：后台读 TOC，面板演完 / 播放器就绪后交棒。

        返回 False = 终端太小，调用方走纯文字启动。没盘且没 --wait-disc
        时直接抛错，由 run() 恢复终端后打印。
        """
        w, h = self.screen.size()
        if w < MIN_W or h < MIN_H:
            return False

        start_paused = self._boot_paused or self.insert_action != "play"
        info = BootInfo(intro=random.randrange(len(INTRO_NAMES)),
                        paused=start_paused)
        tray_probe = (None if self.args.fake
                      else lambda: drive_tray_state(self.args.device))
        loader = _DiscLoader(self.args, wait=self._wait_disc,
                             tray_probe=tray_probe)
        loader.start()
        tl = timeline(info.fast)
        t0 = time.monotonic()
        expand_t0: float | None = None
        expand_at = tl["expand"]
        prepared = False
        try:
            while self.running:
                now = time.monotonic()
                for key in self.keys.read():
                    if key in ("q", "quit", "esc"):
                        self.running = False
                        raise KeyboardInterrupt

                if not prepared:
                    disc = loader.disc
                    if disc is None and loader.crash is not None:
                        raise loader.crash
                    if disc is not None:
                        # 等待画面里刚插盘：从 intro 重新演一遍
                        if (info.no_disc or info.unsupported
                                or info.unreadable):
                            t0 = time.monotonic()
                            info.intro = random.randrange(len(INTRO_NAMES))
                        self._prepare_after_disc(disc, hold=True)
                        info.ready = True
                        info.no_disc = False
                        info.unsupported = False
                        info.unreadable = False
                        info.tracks = len(disc.tracks)
                        info.total_seconds = disc.total_seconds
                        info.cached = self._cached_count(disc)
                        prepared = True
                        now = time.monotonic()
                        # TOC 慢时 READY 至少留 0.4s 给人看清，再展开
                        ready_t = now - t0
                        expand_at = max(tl["expand"], ready_t + READY_HOLD)
                        info.hold_expand = True
                    elif self._wait_disc:
                        # 不支持 ≠ 没盘：明确显示 DATA/UNSUPPORTED，等换盘。
                        if loader.unsupported is not None:
                            info.unsupported = True
                            info.no_disc = False
                            info.unreadable = False
                        elif loader.state == "unreadable":
                            info.unsupported = False
                            info.no_disc = False
                            info.unreadable = True
                        elif loader.state == "waiting":
                            info.unsupported = False
                            info.no_disc = True
                            info.unreadable = False
                        else:
                            # reading/idle：正在重试，不要沿用上一帧的 NO DISC
                            info.unsupported = False
                            info.no_disc = False
                            info.unreadable = False
                    elif loader.finished():
                        raise loader.error or CddaError("无法读取光盘目录")

                if prepared and expand_t0 is None and now - t0 >= expand_at:
                    expand_t0 = now
                    info.hold_expand = False

                if expand_t0 is None:
                    t_frame = now - t0
                else:
                    t_frame = tl["expand"] + (now - expand_t0)

                w, h = self.screen.size()
                self.screen.draw(render_frame(w, h, self.theme, t_frame, info))

                if expand_t0 is not None and now - expand_t0 >= tl["dash"] - tl["expand"]:
                    break
                time.sleep(max(0.0, 1.0 / FPS - (time.monotonic() - now)))
        finally:
            loader.stop()

        if not start_paused and self.engine:
            self._begin_playback()      # 自动开播：到这一刻才开始抓轨/查元数据
        self._boot_paused = False
        return True

    def _install_cache(self, disc) -> Cache:
        """准备本盘缓存：格式迁移、清旧盘 PCM、自愈上次留下的半成品。

        同一个缓存根可能被两个实例同时使用（一边在播真盘，一边开着 --fake
        试玩）。共享目录只在拿到本盘独占锁之后才写；抢不到就退到实例私有的
        临时缓存，绝不碰别人的 PCM 和 .part。
        """
        self._close_cache()
        args = getattr(self, "args", None)
        cache = Cache(cache_root_for(args) if args is not None else None)
        cache.ensure_format()
        try:
            shared_ok = cache.acquire(disc)
        except OSError:
            shared_ok = False
        if not shared_ok:
            cache.close()
            cache = Cache.isolated()
            cache.ensure_format()
            try:
                cache.acquire(disc)
            except OSError:
                pass
        else:
            cache.purge_except(disc)    # 清别的盘的 PCM，曲名缓存留着
        cache.repair_incomplete(disc)   # 上次崩溃留下的整轨 .part 直接收尾
        return cache

    def _close_cache(self) -> None:
        """换盘/退出时释放本盘缓存锁；私有临时缓存顺手删掉。"""
        cache = self.cache
        if cache is None:
            return
        try:
            cache.close()
        except (AttributeError, OSError):
            pass

    def _make_ripper(self, disc, cache: Cache) -> Ripper | FakeRipper:
        mode = getattr(self, "rip_mode", RIP_MODE)
        if self.args.fake:
            return FakeRipper(disc, cache, speed=self.args.speed, mode=mode)
        return Ripper(disc, cache, self.args.device,
                      fast=bool(getattr(self.args, "fast_rip", False)),
                      mode=mode)

    # ── 换盘 ──────────────────────────────────────────────────────────────
    def _start_disc_monitor(self) -> None:
        """真光驱才需要监视换盘；--fake 的虚拟盘永远在。"""
        if self.args.fake or not self.disc or not self.disc.device.startswith("/dev/"):
            return
        device = self.disc.device
        monitor = DiscMonitor(lambda: drive_tray_state(device),
                              lambda: query_disc(device),
                              interval=DISC_POLL_S)
        monitor.prime(TRAY_CLOSED_DISC, self.disc)   # 启动时状态已知，不必立刻重读 TOC
        tray, since, _ = monitor.tray_snapshot()
        self._set_tray(tray, since)
        monitor.start()
        self.monitor = monitor

    def _poll_disc(self) -> None:
        """托盘三态边沿；开仓只停歌，关仓后才 query 盘。"""
        monitor = self.monitor
        if monitor is None or self._ripper_stuck:
            return
        tray, tray_since, disc = monitor.tray_snapshot()
        if tray is None:
            return                       # 过渡态/未知：维持现状，不能误报
        view = self._get_tray()
        view.set_tray(tray, tray_since)

        ui = view.ui
        if tray == TRAY_OPEN:
            if ui != "open":
                self._on_tray_open()
            return

        if tray == TRAY_CLOSED_EMPTY:
            if (ui in ("open", "checking", "reading", "unsupported",
                       "unreadable", "ready", "return")
                    or (ui == "" and view.disc_present)):
                self._on_tray_closed_empty()
            return

        # TRAY_CLOSED_DISC：关仓有盘，但 TOC 可能还没读出来/可能是数据盘
        error = (monitor.error_snapshot()
                 if hasattr(monitor, "error_snapshot") else None)
        if isinstance(error, UnsupportedDiscError):
            if ui != "unsupported":
                self._on_tray_unsupported()
            return
        if isinstance(error, UnreadableDiscError):
            if ui != "unreadable":
                self._on_tray_unreadable()
            return
        if (ui == "" and view.disc_present
                and disc is not None and self.disc is not None
                and disc.discid != self.disc.discid):
            # 防御分支：仅当监视器真的提供了新 disc（未来 media-change/
            # 重新探测）时才会走到。纯 DRIVE_STATUS 0.1s 轮询漏掉整个
            # open/closed-empty 边沿时，DiscMonitor 不会重新读 TOC。
            self._last_disc = self.disc
            self.disc_present = False
            self.disc_state = "checking"
            if self.engine:
                self.engine.stop()
                self._set_autopaused(False)
            self._retire_ripper(preserve_part=True)
            self._begin_tray_check()
            self._on_tray_disc(disc)
            return
        if ui in ("", "open", "closed_empty") and not view.disc_present:
            self._begin_tray_check()
        if view.ui == "checking" and disc is not None:
            self._on_tray_disc(disc)

    def _on_tray_open(self) -> None:
        """开仓边沿：立即停歌，保留最后一帧 playing 直接压缩到 TRAY OPEN。"""
        tray = self._get_tray()
        was_ui = tray.ui
        source = getattr(self, "_last_dash_rows", None) if was_ui == "" else None
        if source is None and was_ui == "" and hasattr(self, "screen"):
            source = self._render_dash_rows()
        self._cancel_pending_seek()
        tray.enter(TrayPhase.OPEN, present=False, state="tray_open",
                   transition=None, last_disc=self.disc)
        self.help = False
        self.hint = ""
        self._get_feedback().reset()
        self._clear_message()
        if self.engine:
            self.engine.stop()
            self._set_autopaused(False)
        if was_ui == "" and source is not None:
            target = self._no_disc_rows("open")
            self._start_tray_transition(source, target, 1)
        # 先起 UI/过场，再做非阻塞收尾；抓轨线程晚点退出由 _retry_stuck_ripper 收。
        self._retire_ripper(preserve_part=True, timeout=0.0)

    def _on_tray_closed_empty(self) -> None:
        """关仓后确认无盘：停在 NO DISC，不硬回主界面。"""
        tray = self._get_tray()
        self._cancel_pending_seek()
        last = self.disc if self.disc is not None else tray.last_disc
        tray.enter(TrayPhase.CLOSED_EMPTY, present=False, state="no_disc",
                   transition=None, last_disc=last)
        self._get_feedback().reset()
        self._clear_message()
        if self.engine:
            self.engine.stop()
            self._set_autopaused(False)
        self._retire_ripper(preserve_part=True, timeout=0.0)

    def _on_tray_unsupported(self) -> None:
        """确认是数据 CD/DVD：停播、收抓轨器，稳定停在 UNSUPPORTED。

        不构造 Disc、不启动 Ripper/Engine、不查 MusicBrainz；等开仓或换盘
        边沿清掉状态。`_last_disc` 保留上一张可播放的盘，方便换回时续播。
        """
        self._cancel_pending_seek()
        self._get_tray().enter(TrayPhase.UNSUPPORTED, present=False,
                               state="unsupported", transition=None)
        self.help = False
        self.hint = ""
        self._get_feedback().reset()
        self._clear_message()
        if self.engine:
            self.engine.stop()
            self._set_autopaused(False)
        self._retire_ripper(preserve_part=True, timeout=0.0)

    def _on_tray_unreadable(self) -> None:
        """有盘但宽限期后仍认不出 CD-DA：停播、收抓轨器，稳定停在 UNREADABLE。

        与 unsupported 一样不启动新流程；等开仓/换盘边沿重新判断。
        """
        self._cancel_pending_seek()
        self._get_tray().enter(TrayPhase.UNREADABLE, present=False,
                               state="unreadable", transition=None)
        self.help = False
        self.hint = ""
        self._get_feedback().reset()
        self._clear_message()
        if self.engine:
            self.engine.stop()
            self._set_autopaused(False)
        self._retire_ripper(preserve_part=True, timeout=0.0)

    def _begin_tray_check(self) -> None:
        self._cancel_pending_seek()
        self._get_tray().enter(TrayPhase.CHECKING, present=False,
                               state="checking", transition=None)
        self._get_feedback().reset()
        self._clear_message()

    def _on_tray_disc(self, disc) -> None:
        """query 到盘：同盘恢复，新盘装载；READING 只做 TOC 完成过渡。"""
        if self.tray_ui not in ("checking", "open"):
            return
        same = (self._last_disc is not None
                and disc.discid == self._last_disc.discid)
        if same:
            self._resume_disc(disc)
        else:
            self._load_disc(disc)
        tray = self._get_tray()
        if not tray.disc_present:
            # 抓轨线程收尾失败等：留在 CHECKING 重试，不能误判无盘
            tray.enter(TrayPhase.CHECKING, present=False, state="checking",
                       transition=None)
            return
        tray.enter(TrayPhase.READING, present=True, state="ready",
                   ready_at=time.monotonic() + 0.50)

    def _tray_tick(self, now: float) -> None:
        tray = self._get_tray()
        if tray.phase == TrayPhase.READING and now >= tray.ready_at:
            tray.enter(TrayPhase.READY, present=True, state=tray.disc_state,
                       ready_at=now + 0.50)
        elif tray.phase == TrayPhase.READY and now >= tray.ready_at:
            if not tray.disc_present:
                tray.enter(TrayPhase.CLOSED_EMPTY, present=False,
                           state="no_disc", transition=None)
                return
            dash = self._render_dash_rows()
            ready = self._no_disc_rows("ready")
            tray.enter(TrayPhase.RETURN, present=True, state=tray.disc_state,
                       transition=None)
            if not self._start_tray_transition(dash, ready, -1):
                tray.enter_main()
        elif (tray.phase == TrayPhase.RETURN
              and tray.transition is None):
            tray.enter_main()

    def _no_disc_rows(self, phase: str) -> list[str]:
        w, h = self.screen.size()
        tracks = len(self.disc.tracks) if self.disc else 0
        total = self.disc.total_seconds if self.disc else 0.0
        cached = (self._cached_count(self.disc)
                  if self.disc is not None and self.cache is not None else 0)
        return render_no_disc(w, h, self.theme, phase, time.monotonic(),
                              tracks=tracks, total_seconds=total, cached=cached)

    def _render_dash_rows(self) -> list[str]:
        w, h = self.screen.size()
        try:
            rows = render(self._make_state(), w, h)
        except Exception as exc:          # 渲染不能带崩播放
            rows = [fit(f"  render error: {exc}", w)]
        rows = [fit(r, w) for r in rows[:h]]
        rows += [""] * max(0, h - len(rows))
        return rows

    def _start_tray_transition(self, source: list[str],
                               target: list[str], direction: int) -> bool:
        w, h = self.screen.size()
        if w < MIN_W or h < MIN_H or not source or not target:
            self._tray_transition = None
            return False
        now = time.monotonic()
        self._get_tray().transition = {
            "source": source, "target": target,
            "started": now, "duration": 0.70,
            "direction": direction,
            "p": 0.0 if direction > 0 else 1.0,
        }
        return True

    def _update_tray_transition(self, now: float) -> None:
        tr = self._get_tray().transition
        if tr is None:
            return
        elapsed = (now - tr["started"]) / max(1e-6, tr["duration"])
        if tr["direction"] > 0:
            tr["p"] = min(1.0, elapsed)
            if tr["p"] >= 1.0:
                self._get_tray().transition = None
        else:
            tr["p"] = max(0.0, 1.0 - elapsed)
            if tr["p"] <= 0.0:
                self._get_tray().enter_main()

    def _load_disc(self, disc) -> None:
        """换了一张新盘：重建缓存/抓轨器，并按 --on-insert 决定播不播。

        默认 pause 只 cue 音源；抓轨线程要等 Space/自动开播才启动。
        """
        self._cancel_pending_seek()
        if not self._retire_ripper(timeout=TRAY_JOIN_S):
            self.disc_state = "no_disc"
            return
        if self.engine:
            self.engine.stop()
        cache = self._install_cache(disc)
        ripper = self._make_ripper(disc, cache)
        self.session.prepare(disc, cache, ripper)   # 真正开播时才 start
        tray = self._get_tray()
        tray.enter(tray.phase, present=True, state="ready")
        self._set_tray(TRAY_CLOSED_DISC)
        self._start_after_insert(auto_play=self.insert_action == "play", fresh=True)

    def _resume_disc(self, disc) -> None:
        """同一张盘放回来：缓存还在，重建抓轨器并从当前轨重开。

        不预热：默认停在 stopped，按 Space 后才继续抓没缓存完的轨。
        """
        self._cancel_pending_seek()
        if not self._retire_ripper(preserve_part=True, timeout=TRAY_JOIN_S):
            self.disc_state = "no_disc"
            return
        cache = self.cache
        if cache is None:                    # 测试/异常兜底：同盘也要有缓存
            cache = self._install_cache(disc)
        ripper = self._make_ripper(disc, cache)
        self.session.prepare(disc, cache, ripper)   # 停止态不预热
        tray = self._get_tray()
        tray.enter(tray.phase, present=True, state="ready")
        self._set_tray(TRAY_CLOSED_DISC)
        self._start_after_insert(auto_play=self.insert_action == "play",
                                 keep_current=True)

    # ── 抓轨线程收尾 ──────────────────────────────────────────────────────
    def _retire_ripper(self, preserve_part: bool = False,
                       timeout: float | None = None) -> bool:
        """停掉旧抓轨线程并等它真退出。

        返回 False = join 超时、线程仍在跑，此时不能动缓存或启动新抓轨器
        （新旧写者会抢同一个 .pcm.part），先冻结换盘、下一帧重试。
        """
        if self.session.retire(preserve_part=preserve_part, timeout=timeout):
            return True
        self._ripper_stuck = True
        self._flash("抓轨线程收尾超时 — 暂缓换盘，正在重试…", 10.0)
        return False

    def _retry_stuck_ripper(self) -> None:
        """join 超时后每帧再等一小会儿，线程退出后恢复换盘。"""
        if not self._ripper_stuck:
            return
        ripper = self.ripper
        if ripper is not None and ripper.is_alive():
            ripper.join(0.05)
        if ripper is not None and ripper.is_alive():
            return
        self._ripper_stuck = False
        self._flash("抓轨线程已退出 — 可以继续换盘", 3.0)

    def _valid_start_number(self, fresh: bool = False,
                            keep_current: bool = False) -> int:
        """返回当前盘上的起始轨。

        fresh=True（刚换上新盘）从第一轨开始；keep_current=True（同盘放回）
        优先当前轨，不让开机的 --track 把用户手动切到的轨覆盖掉。
        """
        numbers = [t.number for t in self.disc.tracks] if self.disc else []
        if not numbers:
            return 1
        if fresh:
            return numbers[0]
        if keep_current and self.track_no in numbers:
            return self.track_no
        if self.args.track in numbers:
            return self.args.track
        if self.track_no in numbers:
            return self.track_no
        return numbers[0]

    def _start_after_insert(self, auto_play: bool, fresh: bool = False,
                            keep_current: bool = False) -> None:
        self.track_no = self._valid_start_number(fresh=fresh,
                                                 keep_current=keep_current)
        self._start_track(self.track_no, paused=not auto_play)
        if not auto_play:
            self.engine.stop_playback()    # 「按空格开始」= 停止态，不是暂停
        if auto_play:
            self._flash("新盘就绪 — 自动播放", 6.0)
        else:
            self._flash("新盘就绪 — 按空格开始", 8.0)

    def _absent_tick(self) -> None:
        """无盘且没有托盘状态机时的兜底：立即停，不再继续播缓存。"""
        self.hint = ""
        if self.engine and self.engine.state in ("playing", "paused", "buffering"):
            self._set_autopaused(False)
            self.engine.stop()

    def _track_cached(self, number: int) -> bool:
        if not self.disc:
            return False
        if self.ripper:
            return self.ripper.is_done(number)
        return self.cache.is_done(self.disc, number) if self.cache else False

    def _start_metadata_lookup(self) -> None:
        """后台补元数据；具体会话归属由 DiscSession 管理。"""
        session = self.session
        session.args = getattr(self, "args", session.args)
        session.start_metadata()

    def _boot_frame(self, msg: str) -> None:
        w, h = self.screen.size()
        line = f"  {msg}"
        rows = [" " * w for _ in range(min(3, h))]
        rows[1] = line
        self.screen.draw(rows)

    def _start_track(self, number: int, start_s: float = 0.0,
                     paused: bool | None = None,
                     transition: str | None = None) -> None:
        assert self.disc and self.cache and self.ripper and self.engine
        # 任何切轨都会换音源；旧盘的 pending Seek 绝不能在新轨上迟到来。
        self._cancel_pending_seek()
        track = self.disc.track(number)
        prev_no = self.track_no
        try:
            prev_track = self.disc.track(prev_no)
        except KeyError:
            prev_track = None
        feedback = self._get_feedback()
        if (transition in ("auto", "next", "prev")
                and prev_track is not None and number != prev_no):
            title, artist = self.cache.title_of(self.disc, prev_no)
            current = TrackSnapshot(
                number=prev_no,
                title=title,
                artist=artist,
                position=float(self.engine.position()),
                duration=float(prev_track.seconds),
            )
            now = self._now()
            visual = feedback.visible_source(current, now)
            if transition == "auto":
                mode = "auto"
            else:
                # 方向语义看本次业务切换；可见源只负责动画从哪里接续。
                mode = "next" if number > prev_no else "prev"
            feedback.begin_track(
                visual.track,
                number,
                mode,
                now=now,
                source_title_fade=visual.title_fade,
                source_bar_fade=visual.bar_fade,
                business_from_no=prev_no,
            )
        else:
            # 重播/装载/同轨选曲不是切轨：取消旧退场，避免过期曲名回闪。
            feedback.reset()
        self.track_no = number
        self.message = ""                        # 换轨/重试时清掉上一轨的错误提示
        # 首播/自动 EOF/断点续播/已缓存命中不需要机械防抖；其余选轨延迟提交。
        immediate = (transition == "auto" or start_s > 0
                     or self._track_cached(number))
        self.session.select_track(number, immediate=immediate)
        self.analyzer.reset()
        media = TrackMedia(self.cache, self.disc, track, self.ripper)
        # 显式 paused 优先（插盘策略）；否则：开机暂停参数或当前就是停止态。
        # 停止态按 n/p/数字/r 只换歌、不启动抓轨/输出，必须等 Space 才开播。
        if paused is None:
            should_pause = (self._boot_paused
                            or getattr(self.engine, "state", "") == "stopped")
        else:
            should_pause = paused
        self._boot_paused = False
        if not should_pause:
            # 第一次离开停止态才启动抓轨器；启动/插盘阶段只 cue 音源。
            self._start_caching()
            self._boot_pause_prompt = False
        if should_pause:
            self.engine.cue(media, start_s)      # 只装音源，不起 pw-cat
        else:
            self.engine.load(media, start_s)
        self._set_autopaused(False)

    # ── 引擎事件 ──────────────────────────────────────────────────────────
    def _on_engine_state(self, state: str) -> None:
        if state == "error" and self.engine:
            self._flash(f"error: {self.engine.error or 'unknown'}", 30.0)

    # ── 主循环 ────────────────────────────────────────────────────────────
    def run(self) -> int:
        """跑主循环；返回进程退出码（0 = 正常退出，1 = 启动失败）。"""
        try:
            self.setup()
        except UnsupportedDiscError as exc:
            self.screen.leave()
            print(f"\n  ✘ {exc}\n")
            print("  提示：只支持 CD-DA 音乐 CD；请换一张音频盘。\n")
            return 1
        except UnreadableDiscError as exc:
            self.screen.leave()
            print(f"\n  ✘ {exc}\n")
            print("  提示：光驱有盘但读不出 CD-DA；确认盘面干净、"
                  "换一张音频 CD 再试。\n")
            return 1
        except (NoMediaError, CddaError) as exc:
            self.screen.leave()
            print(f"\n  ✘ {exc}\n")
            print("  提示：确认光盘已放入、托盘已关闭；或用 --fake 先看效果。\n")
            print("  光驱权限：用 `id -nG` 确认当前用户在 cdrom/optical 组，"
                  "必要时 `sudo usermod -aG optical $USER` 后重新登录。\n")
            return 1
        except PermissionError as exc:
            self.screen.leave()
            print(f"\n  ✘ 没有权限访问光驱：{exc}\n")
            print("  提示：把用户加入 cdrom/optical 组后重新登录，"
                  "或临时用 `sudo asciicd` 验证。\n")
            return 1
        except KeyboardInterrupt:
            self.shutdown()
            return 0
        except Exception:
            try:
                self.shutdown()
            finally:
                self._log_crash()
            raise
        try:
            while self.running:
                t0 = time.monotonic()
                self._handle_keys()
                self._tick()
                self._draw()
                dt = 1.0 / FPS - (time.monotonic() - t0)
                if dt > 0:
                    time.sleep(dt)
        except KeyboardInterrupt:
            self.shutdown()                  # Ctrl-C 也要还终端、清缓存
        except Exception:
            self.shutdown()
            self._log_crash()
            raise
        else:
            self.shutdown()
        return 0

    @staticmethod
    def _log_crash() -> None:
        """崩溃也要把终端还回去，并留一份 traceback 方便查。"""
        import os
        import traceback
        root = os.path.expanduser("~/.cache/asciicd")
        try:
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, "crash.log"), "a", encoding="utf-8") as fh:
                fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                traceback.print_exc(file=fh)
        except OSError:
            pass

    def shutdown(self) -> None:
        try:
            self._cancel_pending_seek()
            if self.monitor:
                self.monitor.stop()
            if self.engine:
                self.engine.stop()
            if self.ripper:
                self.ripper.stop_and_join()
            self._clear_audio_cache()
        finally:
            self._close_cache()
            self._save_theme()
            self.screen.leave()

    def _clear_audio_cache(self) -> None:
        """退出即清本盘 PCM；meta.json 留着，下次没网也能显示曲名。

        必须放在 engine/ripper 都收尾之后 —— 还在读 .pcm 的线程不能被抽走文件。
        清不干净也不该挡住退出。
        """
        try:
            self.session.clear_audio()
        except OSError:
            pass

    # ── 每帧逻辑 ──────────────────────────────────────────────────────────
    def _tick(self) -> None:
        assert self.engine and self.ripper and self.disc
        now = self._now()
        if self.message and now > self._message_until:
            self.message = ""

        # 旧抓轨线程没退出时先别换盘，等它收尾
        self._retry_stuck_ripper()
        # 换盘监听：开仓/关仓/无盘分支都在这里
        self._poll_disc()
        self._update_tray_transition(now)
        if getattr(self, "tray_ui", ""):
            self._cancel_pending_seek()     # 托盘状态机接管：旧 Seek 不能迟到
            self._tray_tick(now)
            return
        if not self.disc_present:
            self._cancel_pending_seek()
            self._absent_tick()
            return

        # 播完一轨 → 下一轨；最后一轨播完就停下（不回绕）
        if self.engine.state == "eof":
            # EOF 是轨边界：这一批 Seek 属于旧轨，直接丢弃，不阻塞自动下一轨。
            self._cancel_pending_seek()
            if self.disc_present and not self._track_cached(self.track_no):
                # 盘已放回、只是缓冲前缀播完：继续抓当前轨并从断点接着播
                resume_s = min(self.engine.position(),
                               max(0.0, self.disc.track(self.track_no).seconds - 0.5))
                self._start_track(self.track_no, start_s=resume_s)
                return
            nxt = self.disc.next_number(self.track_no)
            if nxt is not None:
                self._start_track(nxt, transition="auto")
            else:
                self.engine.stop_playback()
                if self.ripper is not None:
                    # 盘尾也是 stopped：顺手停止主动读盘；SPACE 重播时恢复。
                    self.ripper.pause()
                self._flash(
                    "disc finished — SPACE replay · 1-9 select track · q quit",
                    8.0,
                )

        # 抓轨已永久失败：哪怕喂数据线程正卡在 autopause/暂停分支里等着，也要
        # 立刻收尾当前输出并给出错误，否则界面会一直停在抓轨阶段。
        if (self.engine.state in ("playing", "paused")
                and self.ripper.is_failed(self.track_no)):
            self._cancel_pending_seek()
            reason = self.ripper.failure_reason(self.track_no)
            detail = f"：{reason}" if reason else ""
            self.engine.fail(f"第 {self.track_no} 轨抓轨失败{detail}")

        # 抓轨失败/输出错误不是正常 EOF：保留错误提示，不自动下一轨，也不继续转缓冲动画
        if self.engine.state == "error":
            self._cancel_pending_seek()
            self._set_autopaused(False)
            self.hint = ""

        # 主循环安全点：安静窗口到期后只提交一次真实 Seek。放在 EOF/错误/
        # 停播判断之后，坏盘和换轨边界不会被迟到的播放头跳转越过。
        self._commit_pending_seek(now)

        lead = self._cache_lead()
        avail = self.ripper.available(self.track_no)
        # 这一轨是否已经完整落到缓存：抓完了就不存在「缓冲不够」，放心播到 EOF
        full = avail >= self.disc.track(self.track_no).pcm_bytes
        if self._autopaused or (self.engine.state in ("playing", "paused") and avail == 0):
            self._spin_i = (self._spin_i + 1) % len(self._spin)
            # 缓冲/抓轨状态只在 CACHE 行显示，底框不再放长提示。
            self.hint = ""
        elif self.hint and not self.message:
            self.hint = ""
        # 防跳帧：只有「整轨还没抓完」时才自动缓冲；抓完的轨播到自然结束
        if self.engine.state == "playing" and lead < 1.0 and not full:
            self.engine.pause()
            self._set_autopaused(True)
        elif self._autopaused and (lead >= PREROLL_S or full):
            # 已经暂停过的情况下：整轨抓完了就不用再等 6 秒提前量，直接继续
            self.engine.play()
            self._set_autopaused(False)

        # 抓轨要优先伺候正在播的轨
        if self.engine.state in ("playing", "paused", "buffering"):
            self.ripper.request_priority(self.track_no)

    def _cache_lead(self) -> float:
        assert self.engine and self.ripper
        avail = self.ripper.available(self.track_no)
        pos_bytes = self.engine.position() * BYTES_PER_SEC
        return max(0.0, (avail - pos_bytes) / BYTES_PER_SEC)

    def _state_name(self) -> str:
        assert self.engine
        if self._autopaused:
            return "buffering"
        return self.engine.state

    def _update_idle(self, now: float) -> None:
        """停止/空闲时进入正弦+扫描待机；恢复播放时 0.45s 切回 live。

        暂停（含自动缓冲）不再进待机：画面直接冻结在最后一帧。
        """
        idle_state = self._state_name() in ("stopped", "idle")
        target = 1.0 if idle_state else 0.0
        if target != self._idle_target:
            self._idle_from = self._scope_mix
            self._idle_target = target
            self._idle_t0 = now
            self._idle_dur = 0.55 if target else 0.45
            if target:
                self._idle_start = now
                self._idle_t = 0.0
        p = max(0.0, min(1.0, (now - self._idle_t0) / self._idle_dur))
        e = p * p * (3.0 - 2.0 * p)
        self._scope_mix = self._idle_from + (target - self._idle_from) * e
        if target:
            self._idle_t = now - self._idle_start

    def _make_state(self) -> UIState:
        assert self.disc and self.engine and self.ripper
        track = self.disc.track(self.track_no)
        title, artist = self.cache.title_of(self.disc, self.track_no) if self.cache else (f"Track {self.track_no:02d}", "")
        meta = self.cache.read_meta(self.disc) if self.cache else {}
        album = meta.get("album") or ""
        boot_prompt = (self._boot_pause_prompt
                       and self.engine.state in ("paused", "stopped"))
        now = self._now()
        self._update_idle(now)
        feedback = self._get_feedback()
        feedback.sample(now)
        trans = feedback.transition
        pulse = feedback.pulse
        state = self._state_name()
        # 暂停/停止时复用最后一帧：暂停要完全冻结，停止则交给待机动画
        # （不再跑 FFT、也不滚瀑布；后台 buf / RIP 数字照常更新）
        if self.engine.state in ("paused", "stopped"):
            vis = self._frozen_vis
        else:
            vis = self.analyzer.frame()
            self._frozen_vis = vis
        return UIState(
            device=self.disc.device,
            model=self.disc.model,
            tracks=self.disc.tracks,
            track_no=self.track_no,
            title=title,
            artist=artist,
            album=album,
            # 连续 Seek 时显示待提交的预览目标；真实播放头/缓冲判断
            # （_cache_lead、EOF、autopause）仍只用 engine.position()。
            position=self._display_position(),
            duration=track.seconds,
            state=state,
            volume=self.engine.volume,
            theme=self.theme,
            theme_name=self.theme_name,
            vis=vis,
            cache_lead=self._cache_lead(),
            rip=self.ripper.snapshot(),
            total_seconds=self.disc.total_seconds,
            message=("SPACE 开始播放 · \x1b[4mh\x1b[24melp · q 退出"
                     if boot_prompt else self.message),
            hint=self.hint,
            spinner=self._spin[self._spin_i] if self._autopaused else "",
            minimal=self.layout == "mini",
            layout=self.layout,
            disc_present=self.disc_present,
            tray_state=self.tray_state,
            tray_since=self._tray_since,
            autopause_since=self._autopause_since,
            scope_mix=self._scope_mix,
            idle_t=self._idle_t,
            help=self.help,
            track_transition=trans,
            visual_pulse=pulse,
        )

    def _draw(self) -> None:
        w, h = self.screen.size()
        try:
            tr = self._tray_transition
            if tr is not None:
                rows = squash_rows(tr["source"], tr["target"], tr["p"],
                                   self.theme)
                rows = [fit(r, w) for r in rows[:h]]
                rows += [""] * max(0, h - len(rows))
            elif self.tray_ui:
                phase = (self.tray_ui if self.tray_ui in
                         ("open", "closed_empty", "checking", "reading",
                          "unsupported", "unreadable", "ready")
                         else "ready")
                rows = self._no_disc_rows(phase)
            else:
                rows = self._render_dash_rows()
                self._last_dash_rows = rows
        except Exception as exc:                      # 渲染不能带崩播放
            rows = [fit(f"  render error: {exc}", w)]
            rows += [""] * max(0, h - 1)
        self.screen.draw(rows[:h])

    # ── 连续 Seek 合并 ────────────────────────────────────────────────────
    def _cancel_pending_seek(self) -> None:
        """丢弃尚未提交的 Seek；换轨/停播/弹盘/退出时调用，防止迟到生效。"""
        self._pending_seek_target = None
        self._pending_seek_deadline = 0.0
        self._pending_seek_origin = 0.0
        self._pending_seek_track = None
        self._pending_seek_discid = None

    def _pending_seek_is_current(self) -> bool:
        """pending 是否仍属于当前盘/轨；只做只读校验，不在渲染中改状态。"""
        target = getattr(self, "_pending_seek_target", None)
        if target is None:
            return False
        track = getattr(self, "_pending_seek_track", None)
        if track is not None and track != self.track_no:
            return False
        discid = getattr(self, "_pending_seek_discid", None)
        if discid is not None:
            disc = getattr(self, "disc", None)
            if disc is None or getattr(disc, "discid", None) != discid:
                return False
        return True

    def _seek_limit(self) -> float:
        """当前轨允许 Seek 的上界；与 Engine.seek_to 的裁剪保持一致。"""
        disc = getattr(self, "disc", None)
        if disc is None:
            return 0.0
        try:
            track = disc.track(self.track_no)
        except (KeyError, AttributeError):
            return 0.0
        return max(0.0, float(track.seconds) - 0.05)

    def _display_position(self) -> float:
        """进度条用的显示位置：pending 期间立即显示预览目标。

        只影响 UI；缓冲水位、EOF、自动缓冲判断仍走 Engine 的真实消费量。
        """
        target = getattr(self, "_pending_seek_target", None)
        if target is not None and self._pending_seek_is_current():
            return float(target)
        engine = getattr(self, "engine", None)
        return float(engine.position()) if engine is not None else 0.0

    def _cmd_seek(self, delta: float, now: float | None = None) -> None:
        """←/→：累积到 pending，UI 立刻预览；稳定窗口后只提交一次。"""
        engine = getattr(self, "engine", None)
        if engine is None or getattr(self, "disc", None) is None:
            return
        now = self._now() if now is None else float(now)
        # 上一批已不属于当前盘/轨时先丢弃，绝不让旧目标作用到新音源。
        if (getattr(self, "_pending_seek_target", None) is not None
                and not self._pending_seek_is_current()):
            self._cancel_pending_seek()
        target = getattr(self, "_pending_seek_target", None)
        if target is None:
            base = float(engine.position())
            origin = base
        else:
            base = float(target)
            origin = float(getattr(self, "_pending_seek_origin", base))

        limit = self._seek_limit()
        new_target = max(0.0, min(base + delta, limit))
        total = new_target - origin
        if abs(total) < 0.05:            # 避免 "-0s"；净位移为 0 时提交阶段会跳过
            total = 0.0

        self._pending_seek_target = new_target
        self._pending_seek_deadline = now + SEEK_DEBOUNCE_S
        self._pending_seek_origin = origin
        self._pending_seek_track = self.track_no
        disc = getattr(self, "disc", None)
        self._pending_seek_discid = (getattr(disc, "discid", None)
                                     if disc is not None else None)

        # 视觉反馈按累计净位移显示；方向也看净位移，避免连续反向时提示自相矛盾。
        # seek_pulse 复用同一个 pulse：短按 1 个箭头，长按重复键累计到 3 个。
        direction = 1 if total > 0.0 else -1 if total < 0.0 else 0
        self._get_feedback().seek_pulse(
            f"SEEK {total:+.0f}s",
            direction,
            duration=SEEK_PULSE_S,
            now=now,
        )

    def _commit_pending_seek(self, now: float | None = None, *,
                             force: bool = False) -> bool:
        """在稳定窗口到期后提交一次真实 Seek；返回是否发生跳转。

        只在主循环安全点调用，不 sleep、不起线程。Space 等立即动作可以
        ``force=True`` 先结清上一批，保证“按键后立刻暂停/播放”的落点确定。
        """
        target = getattr(self, "_pending_seek_target", None)
        if target is None:
            return False
        if not self._pending_seek_is_current():
            self._cancel_pending_seek()
            return False
        now = self._now() if now is None else float(now)
        if not force and now < self._pending_seek_deadline:
            return False

        origin = float(getattr(self, "_pending_seek_origin", target))
        self._cancel_pending_seek()
        engine = getattr(self, "engine", None)
        if engine is None:
            return False
        # → 再 ← 净位移为 0：不为了回到原地重启 pw-cat，避免无谓断音。
        if abs(float(target) - origin) <= 0.001:
            return False
        seek_to = getattr(engine, "seek_to", None)
        if seek_to is not None:
            seek_to(float(target))
        else:                            # 兼容极简测试 stub；正式 Engine 走 seek_to
            engine.seek_rel(float(target) - float(engine.position()))
        return True

    # ── 输入 ──────────────────────────────────────────────────────────────
    def _cmd_play_pause(self) -> None:
        """Space：从停止态开播时此刻才启动抓轨/元数据。"""
        # 先结清连续 Seek：暂停/恢复的落点必须是用户最后看到的目标。
        self._commit_pending_seek(force=True)
        before = self.engine.state
        was_started = self.session.started
        self._start_caching()
        self.engine.toggle()
        self._set_autopaused(False)
        self._boot_pause_prompt = False
        after = self.engine.state
        # 已经启动过抓轨、从 stopped/paused 恢复：立即提交当前轨，
        # 不让此前快速选曲留下的 pending 继续压着播放。
        if (was_started and self.ripper is not None
                and after in ("playing", "buffering") and after != before):
            self.ripper.want(self.track_no)
        if after != before and after in ("paused", "playing", "buffering"):
            paused = after == "paused"
            self._get_feedback().show_pulse(
                "paused" if paused else "playing",
                "PAUSED" if paused else "PLAYING",
                duration=STATE_PULSE_S,
                now=self._now(),
            )

    def _cmd_stop(self) -> None:
        # Stop 是全局重置：丢弃尚未提交的 Seek，避免停下后播放头又被挪走。
        self._cancel_pending_seek()
        if self.ripper is not None:
            # 先杀 cdparanoia 停止主动读盘，再收播放输出；不是只取消优先级。
            self.ripper.pause()
        self.engine.stop_playback()
        self._set_autopaused(False)
        self._boot_pause_prompt = False
        self.hint = ""
        self._get_feedback().reset()
        self._flash("已停止 · SPACE 从本轨开头播放", 5.0)

    def _cmd_volume(self, delta: float) -> None:
        self.engine.nudge_volume(delta)

    def _cmd_next(self) -> None:
        nxt = self.disc.next_number(self.track_no)
        if nxt:
            self._start_track(nxt, transition="next")

    def _cmd_prev(self) -> None:
        prv = self.disc.prev_number(self.track_no)
        if prv:
            self._start_track(prv, transition="prev")

    def _cmd_replay(self) -> None:
        self._start_track(self.track_no)

    def _cmd_cycle_layout(self) -> None:
        i = LAYOUTS.index(self.layout) if self.layout in LAYOUTS else -1
        self.layout = LAYOUTS[(i + 1) % len(LAYOUTS)]
        self._flash(f"layout: {self.layout}")

    def _cmd_toggle_layout(self) -> None:
        self.layout = "dash" if self.layout == "mini" else "mini"
        self._flash("layout: mini" if self.layout == "mini" else "layout: dash")

    def _cmd_cycle_theme(self) -> None:
        names = list(THEMES)
        self.theme_name = names[(names.index(self.theme_name) + 1) % len(names)]
        self._save_theme()
        self._flash(f"theme: {self.theme_name}")

    def _cmd_select_digit(self, key: str) -> None:
        n = int(key)
        if any(t.number == n for t in self.disc.tracks):
            if n > self.track_no:
                mode = "next"
            elif n < self.track_no:
                mode = "prev"
            else:
                mode = None
            self._start_track(n, transition=mode)

    def _handle_keys(self) -> None:
        assert self.engine and self.disc
        for key in self.keys.read():
            if key in ("q", "quit"):
                self.running = False
                continue
            if key in ("h", "H"):
                if self.disc_present:
                    self.help = not self.help
                continue
            if not self.disc_present:
                continue                     # 无盘时只留 q；其他键等插盘后再响应
            self.help = False                # 帮助布局里任意键退出，键照常生效
            if key == "space":
                self._cmd_play_pause()
            elif key == "s":
                self._cmd_stop()
            elif key == "right":
                self._cmd_seek(5.0)
            elif key == "left":
                self._cmd_seek(-5.0)
            elif key == "up":
                self._cmd_volume(0.05)
            elif key == "down":
                self._cmd_volume(-0.05)
            elif key == "n":
                self._cmd_next()
            elif key == "p":
                self._cmd_prev()
            elif key == "r":
                self._cmd_replay()
            elif key == "l":
                self._cmd_cycle_layout()
            elif key == "m":
                self._cmd_toggle_layout()
            elif key == "t":
                self._cmd_cycle_theme()
            elif key == "esc":
                pass
            elif key.isdigit() and key != "0":
                self._cmd_select_digit(key)


def selftest(seconds: float = 4.0, speed: float = 24.0) -> int:
    """无终端自测：假盘 → 抓轨 → 出声(pw-cat) → 分析 → 打印统计。

    临时抓轨目录在退出时删掉 —— 一次自测会写下几百 MB PCM，留在 /tmp
    里没人清。
    """
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="asciicd-selftest-")
    try:
        disc = build_fake_disc()
        cache = Cache(tmp)
        ripper = FakeRipper(disc, cache, speed=speed)
        ripper.want(1)
        ripper.start()
        analyzer = Analyzer()
        engine = Engine(on_audio=analyzer.push)
        engine.load(TrackMedia(cache, disc, disc.track(1), ripper))

        frames = 0
        spec_acc = 0.0
        t0 = time.monotonic()
        peak_pos = 0.0
        try:
            while time.monotonic() - t0 < seconds:
                fr = analyzer.frame()
                frames += 1
                spec_acc += float(fr.spectrum.mean())
                peak_pos = max(peak_pos, engine.position())
                time.sleep(1.0 / FPS)
        finally:
            snap = engine.snapshot()
            engine.stop()
            ripper.stop()

        print("── asciicd selftest ─────────────────────────────")
        print(f"  engine state     : {snap['state']}")
        print(f"  position         : {peak_pos:.2f} s")
        print(f"  duration         : {snap['duration']:.2f} s")
        print(f"  frames analysed  : {frames}")
        print(f"  mean spectrum    : {spec_acc / max(1, frames):.3f}")
        ok = (peak_pos > 1.0 and spec_acc / max(1, frames) > 0.02
              and snap["state"] in ("playing", "paused", "eof"))
        if snap["state"] == "error" and snap.get("error"):
            print(f"  错误             : {snap['error']}")
        print(f"  result           : {'OK ✔' if ok else 'FAIL ✘'}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
