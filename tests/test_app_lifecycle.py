"""App 生命周期：启动懒加载、停止态选轨、换盘与恢复。"""
from __future__ import annotations

from app import App

import time
import unittest
from unittest import mock

from cdda.discmon import TRAY_CLOSED_DISC, TRAY_CLOSED_EMPTY
from cdda.fake import build_fake_disc
from config import EJECT_ACTION, INSERT_ACTION
from dsp.analyzer import VisFrame
from ui.feedback import TrackSnapshot, transition_view
from ui.glyphs import strip_ansi, text_width
from ui.layouts import render
from tests.support import (AppEngine, AppMonitor, AppRipper, Keys,
                           make_app)


class TestAppDiscSwap(unittest.TestCase):
    """换盘状态机：弹盘策略、插盘分发、无盘时只留 q。"""

    def _app(self, **kw):
        
        app = App.__new__(App)
        app.engine = kw.get("engine", AppEngine())
        app.ripper = kw.get("ripper", AppRipper())
        app.disc = kw.get("disc", build_fake_disc(durations=(1.0,)))
        app.cache = None
        app.monitor = kw.get("monitor")
        app.disc_present = kw.get("disc_present", True)
        app.disc_state = kw.get("disc_state", "ready")
        app.tray_state = kw.get(
            "tray_state",
            TRAY_CLOSED_DISC if app.disc_present else TRAY_CLOSED_EMPTY)
        app._tray_since = kw.get("tray_since", time.monotonic())
        app._autopause_since = 0.0
        app.eject_action = "stop"
        app.insert_action = kw.get("insert_action", "play")
        app._ripper_pending = kw.get("ripper_pending", False)
        app._ripper_stuck = kw.get("ripper_stuck", False)
        app._caching = False
        app._metadata_seq = -1
        app.track_no = 1
        app.message = ""
        app._message_until = 0.0
        app.hint = ""
        app._autopaused = False
        app._spin = "⠋"
        app._spin_i = 0
        app.layout = "dash"
        app.theme_name = "phosphor"
        app.started = []
        app._start_track = lambda number, start_s=0.0, paused=None, transition=None: (
            app.started.append((number, paused)))
        return app

    def test_s_stops_and_space_restarts(self):
        app = self._app()
        app.keys = Keys(["s"])
        app._handle_keys()
        self.assertEqual(app.engine.stop_playbacks, 1)
        self.assertIn("停止", app.message)
        app.keys = Keys(["space"])
        app._handle_keys()
        self.assertTrue(app.engine.played)

    def test_boot_prompt_shows_underlined_help_without_extra_row(self):
        """开机未播放时在现有底框里提示 h̲elp，不挤占 25 行布局。"""
        app = self._app(engine=AppEngine("stopped"))
        app._boot_pause_prompt = True
        app._scope_mix = app._idle_target = app._idle_from = 1.0
        app._idle_t0 = app._idle_start = time.monotonic()
        app._idle_dur = 0.55
        app._idle_t = 0.0
        app._frozen_vis = VisFrame()
        app.engine.volume = 0.85
        app.help = False

        state = app._make_state()
        self.assertIn("\x1b[4mh\x1b[24melp", state.message)
        rows = render(state, 105, 25)
        self.assertEqual(len(rows), 25)
        self.assertEqual(text_width(rows[-1]), 105)
        self.assertIn("SPACE 开始播放 · help · q 退出", strip_ansi(rows[-1]))

    def test_h_toggles_help_and_any_key_exits(self):
        app = self._app()
        app.help = False
        app.keys = Keys(["h"])
        app._handle_keys()
        self.assertTrue(app.help)
        app.keys = Keys(["h"])
        app._handle_keys()
        self.assertFalse(app.help)
        # 帮助布局里按别的键：退出帮助，键动作照常
        app.keys = Keys(["h", "space"])
        app._handle_keys()
        self.assertFalse(app.help)
        self.assertTrue(app.engine.played)

    def test_l_cycles_layout_again(self):
        app = self._app()
        app.layout = "dash"
        app.keys = Keys(["l"])
        app._handle_keys()
        self.assertEqual(app.layout, "mini")
        app.keys = Keys(["l"])
        app._handle_keys()
        self.assertEqual(app.layout, "dash")

    def test_help_ignored_without_disc(self):
        app = self._app(disc_present=False)
        app.help = False
        app.keys = Keys(["h"])
        app._handle_keys()
        self.assertFalse(app.help)

    def test_autopause_timestamp_entry_and_exit(self):
        app = self._app()
        app._set_autopaused(True)
        first = app._autopause_since
        self.assertGreater(first, 0.0)
        time.sleep(0.01)
        app._set_autopaused(True)        # 重复进入不刷新起点
        self.assertEqual(app._autopause_since, first)
        app._set_autopaused(False)
        self.assertEqual(app._autopause_since, 0.0)

    def test_state_carries_tray_and_autopause_timers(self):
        app = self._app(engine=AppEngine("paused"))
        app._boot_pause_prompt = False
        app._frozen_vis = VisFrame()
        app._scope_mix = app._idle_target = app._idle_from = 0.0
        app._idle_t0 = app._idle_start = time.monotonic()
        app._idle_dur = 0.45
        app._idle_t = 0.0
        app.help = False
        app.engine.volume = 0.85
        app.tray_state = TRAY_CLOSED_DISC
        app._tray_since = 123.0
        app._autopaused = True
        app._autopause_since = 456.0

        state = app._make_state()
        self.assertEqual((state.tray_state, state.tray_since,
                          state.autopause_since),
                         (TRAY_CLOSED_DISC, 123.0, 456.0))

    def test_resume_restarts_when_stopped(self):
        disc = build_fake_disc(durations=(1.0,))
        app = self._app(disc=disc, engine=AppEngine("idle"), insert_action="pause")
        app._make_ripper = lambda disc, cache: AppRipper()
        app._start_after_insert = lambda auto_play, fresh=False, keep_current=False: app.started.append(
            auto_play)
        app._resume_disc(disc)
        self.assertEqual(app.started, [False])

    def test_same_disc_resume_keeps_current_track_over_startup_override(self):
        from types import SimpleNamespace

        disc = build_fake_disc(durations=(1.0,) * 10)
        app = self._app(disc=disc, insert_action="play")
        app.args = SimpleNamespace(track=3)
        app.track_no = 7
        app._make_ripper = lambda disc, cache: AppRipper()
        app._resume_disc(disc)
        self.assertEqual(app.started, [(7, False)],
                         "同盘放回应回到当前第 7 轨，而不是开机的 --track 3")

    def test_load_disc_rebuilds_and_follows_insert_action(self):
        import app as app_module

        class _CacheStub:
            def __init__(self) -> None:
                self.purged: list[str] = []

            def ensure_format(self) -> None:
                pass

            def acquire(self, disc) -> bool:
                return True

            def close(self) -> None:
                pass

            def purge_except(self, disc) -> None:
                self.purged.append(disc.discid)

            def repair_incomplete(self, disc) -> None:
                pass

            def read_meta(self, disc) -> dict:
                return {}

        from types import SimpleNamespace

        new = build_fake_disc(durations=(1.0,))
        events: list[str] = []
        old_ripper = AppRipper(events=events, name="old")
        new_ripper = AppRipper(events=events, name="new")
        app = self._app(disc_present=False, ripper=old_ripper)
        app.args = SimpleNamespace(cache_dir=None, fake=True, track=None,
                                   speed=8.0, no_metadata=True)
        app._make_ripper = lambda disc, cache: new_ripper
        app._start_after_insert = lambda auto_play, fresh=False: app.started.append(
            (auto_play, fresh))
        with mock.patch.object(app_module, "Cache", return_value=_CacheStub()):
            app._load_disc(new)
        self.assertIs(app.disc, new)
        self.assertTrue(app.disc_present)
        self.assertEqual(app.ripper.started, 0, "插盘只读 TOC，不预热抓轨")
        self.assertEqual(app.started, [(True, True)], "新盘必须从第一轨开始")
        self.assertTrue(app.engine.stopped)
        # 换碟时旧盘缓存要清掉（只留新盘）
        self.assertEqual(app.cache.purged, [new.discid])
        # 旧抓轨线程必须先收尾，再启动新线程（防抢缓存文件）
        self.assertEqual(events[:1], ["old:stop_join"])
        self.assertNotIn("new:start", events, "启动停止态不能预热抓轨")

    def test_keys_ignored_without_disc_but_q_quits(self):
        app = self._app(disc_present=False)
        app.keys = Keys(["space", "n", "right"])
        app._handle_keys()
        self.assertFalse(app.engine.played)
        self.assertEqual(app.started, [])
        app.keys = Keys(["q"])
        app._handle_keys()
        self.assertFalse(app.running)

    def test_last_track_eof_enters_stopped_and_can_replay(self):
        disc = build_fake_disc(durations=(1.0, 2.0))
        app = self._app(disc=disc, engine=AppEngine("eof"),
                        ripper=AppRipper({2}))
        app.track_no = 2
        app._tick()
        self.assertEqual(app.engine.stop_playbacks, 1)
        self.assertFalse(app.engine.stopped)
        self.assertEqual(app.engine.state, "stopped")
        self.assertEqual(app.started, [])
        self.assertIn("SPACE replay", app.message)

    def test_stuck_ripper_blocks_swap_until_cleanup(self):
        class _StuckRipper(AppRipper):
            def __init__(self):
                super().__init__()
                self.alive_calls = 0

            def stop_and_join(self, timeout: float = 10.0,
                              preserve_part: bool = False) -> bool:
                self.stopped += 1
                self.events.append(self.name + ":stop_join")
                return False

            def is_alive(self) -> bool:
                self.alive_calls += 1
                return self.alive_calls <= 2

            def join(self, timeout: float | None = None) -> None:
                pass

        old = build_fake_disc(durations=(1.0,))
        new = build_fake_disc(durations=(2.0,))
        app = self._app(disc=old, disc_present=False, ripper=_StuckRipper(),
                        monitor=AppMonitor(True, new))
        self.assertFalse(app._retire_ripper())
        self.assertTrue(app._ripper_stuck)
        app._poll_disc()                       # 卡住时禁止换盘
        self.assertIs(app.disc, old)
        app._retry_stuck_ripper()
        self.assertTrue(app._ripper_stuck)
        app._retry_stuck_ripper()
        self.assertFalse(app._ripper_stuck)

    def test_init_reads_configurable_actions(self):
        from types import SimpleNamespace

        
        def make(**kw):
            args = SimpleNamespace(volume=1.0, theme=None, layout="dash",
                                   minimal=False, paused=False, wait_disc=False,
                                   fake=True, on_eject=None, on_insert=None)
            for key, val in kw.items():
                setattr(args, key, val)
            return App(args)

        self.assertEqual(make().eject_action, EJECT_ACTION)
        self.assertEqual(make().insert_action, INSERT_ACTION)
        self.assertEqual(make(on_eject="stop", on_insert="pause").eject_action, "stop")
        self.assertEqual(make(on_eject="stop", on_insert="pause").insert_action, "pause")




class TestLazyStartup(unittest.TestCase):
    """插盘只读 TOC；抓轨/元数据要等第一次开播（Space 或自动播放）。"""

    class _Cache:
        def ensure_format(self) -> None:
            pass

        def acquire(self, disc) -> bool:
            return True

        def close(self) -> None:
            pass

        def title_of(self, disc, number: int):
            return f"Track {number:02d}", ""

        def purge_except(self, disc) -> None:
            pass

        def repair_incomplete(self, disc) -> None:
            pass

        def is_done(self, disc, number) -> bool:
            return False

    def _app(self, insert_action="pause"):
        from types import SimpleNamespace

        
        app = App.__new__(App)
        app.args = SimpleNamespace(cache_dir=None, fake=True, device=None,
                                   track=None, speed=8.0, no_metadata=True,
                                   fast_rip=False)
        app.analyzer = mock.Mock()
        app.volume = 0.85
        app.insert_action = insert_action
        app._boot_paused = False
        app._boot_pause_prompt = True
        app._disc_seq = 0
        app._metadata_seq = -1
        app._caching = False
        app.track_no = 1
        app.disc = None
        app.disc_present = False
        app.disc_state = "no_disc"
        app.tray_state = ""
        app._tray_since = 0.0
        app._autopaused = False
        app._autopause_since = 0.0
        app.message = ""
        app._message_until = 0.0
        app.engine = None
        app.ripper = AppRipper()
        app.cache = None
        app._make_ripper = lambda disc, cache: AppRipper()
        app._start_disc_monitor = lambda: None
        app._start_metadata_lookup = mock.Mock()
        return app

    def test_prepare_only_cues_and_defers_rip_and_metadata(self):
        import app as app_module

        disc = build_fake_disc(durations=(1.0, 2.0))
        app = self._app(insert_action="pause")
        engine = AppEngine("idle")
        with mock.patch.object(app_module, "Cache", return_value=self._Cache()), \
             mock.patch.object(app_module, "Engine", return_value=engine):
            app._prepare_after_disc(disc, hold=True)

        self.assertEqual(app.ripper.started, 0, "插盘阶段不能启动抓轨")
        self.assertFalse(app._caching)
        self.assertEqual(app._start_metadata_lookup.call_count, 0)
        self.assertIsNotNone(engine.cued, "应装好音源，等 Space 直接开播")
        self.assertIsNone(engine.loaded, "不能提前创建 pw-cat 输出")
        self.assertEqual(engine.state, "stopped")

    def test_auto_play_starts_rip_and_metadata_at_handoff(self):
        import app as app_module

        disc = build_fake_disc(durations=(1.0, 2.0))
        app = self._app(insert_action="play")
        engine = AppEngine("idle")
        with mock.patch.object(app_module, "Cache", return_value=self._Cache()), \
             mock.patch.object(app_module, "Engine", return_value=engine):
            app._prepare_after_disc(disc, hold=True)
            self.assertEqual(app.ripper.started, 0)
            app._begin_playback()

        self.assertEqual(app.ripper.started, 1)
        self.assertEqual(app.ripper.wanted, [1])
        self.assertEqual(app._start_metadata_lookup.call_count, 1)
        self.assertTrue(engine.played)
        self.assertFalse(app._boot_pause_prompt)

    def test_space_starts_lazy_cache_then_toggles(self):
        app = self._app(insert_action="pause")
        app.cache = self._Cache()
        app.disc = build_fake_disc(durations=(1.0, 2.0))
        app.disc_present = True
        app.engine = AppEngine("stopped")
        app.keys = Keys(["space"])

        app._handle_keys()

        self.assertEqual(app.ripper.started, 1)
        self.assertEqual(app.ripper.wanted, [1])
        self.assertEqual(app._start_metadata_lookup.call_count, 1)
        self.assertTrue(app.engine.played)

    def test_start_caching_is_idempotent(self):
        app = self._app(insert_action="pause")
        app.cache = self._Cache()
        app.disc = build_fake_disc(durations=(1.0, 2.0))
        app.track_no = 2

        app._start_caching()
        app._start_caching()

        self.assertEqual(app.ripper.started, 1)
        self.assertEqual(app.ripper.wanted, [2])
        self.assertEqual(app._start_metadata_lookup.call_count, 1)

    def _stopped_app(self, track_no: int = 1):
        app = self._app(insert_action="pause")
        app.cache = self._Cache()
        app.disc = build_fake_disc(durations=(1.0, 1.0, 1.0))
        app.disc_present = True
        app.engine = AppEngine("stopped")
        app.track_no = track_no
        app.help = False
        return app

    def test_stopped_track_selection_only_cues_until_space(self):
        for key, start_track, expected in (
                ("n", 1, 2), ("p", 2, 1), ("2", 1, 2), ("r", 2, 2)):
            with self.subTest(key=key):
                app = self._stopped_app(start_track)
                app.keys = Keys([key])
                app._handle_keys()

                self.assertEqual(app.track_no, expected)
                self.assertEqual(app.ripper.started, 0,
                                 "停止态选轨不能启动抓轨")
                self.assertEqual(app._start_metadata_lookup.call_count, 0)
                self.assertIsNotNone(app.engine.cued)
                self.assertIsNone(app.engine.loaded,
                                  "停止态选轨不能启动 pw-cat")
                self.assertEqual(app.engine.state, "stopped")

                app.keys = Keys(["space"])
                app._handle_keys()
                self.assertEqual(app.ripper.started, 1)
                self.assertEqual(app._start_metadata_lookup.call_count, 1)
                self.assertTrue(app.engine.played)

    def test_track_selection_after_stop_stays_stopped(self):
        app = self._stopped_app(track_no=1)
        app._start_caching()                  # 模拟之前已经播过
        app.engine.state = "playing"
        app.keys = Keys(["s"])
        app._handle_keys()
        self.assertEqual(app.engine.state, "stopped")

        app.keys = Keys(["n"])
        app._handle_keys()

        self.assertEqual(app.track_no, 2)
        self.assertEqual(app.ripper.started, 1, "线程已经在跑，不该重复 start")
        self.assertIsNotNone(app.engine.cued)
        self.assertIsNone(app.engine.loaded, "s 之后切轨仍应保持停止")
        self.assertEqual(app.engine.state, "stopped")




class TestStartTrackFallback(unittest.TestCase):
    """--track 超出新盘轨数时回退首轨，不能 KeyError 崩主循环。"""

    def _app(self, disc, args_track, track_no=1):
        from types import SimpleNamespace

        
        app = App.__new__(App)
        app.disc = disc
        app.args = SimpleNamespace(track=args_track)
        app.track_no = track_no
        app.started = []
        app._start_track = lambda number, start_s=0.0, paused=None, transition=None: (
            app.started.append((number, paused)))
        return app

    def test_default_insert_action_is_pause(self):
        self.assertEqual(INSERT_ACTION, "pause", "插入碟后默认停在 stopped，不自动播放")

    def test_invalid_track_falls_back_to_first(self):
        app = self._app(build_fake_disc(durations=(1.0, 2.0)), args_track=9, track_no=9)
        self.assertEqual(app._valid_start_number(), 1)

    def test_valid_track_kept(self):
        app = self._app(build_fake_disc(durations=(1.0, 2.0)), args_track=2)
        self.assertEqual(app._valid_start_number(), 2)

    def test_invalid_args_but_current_valid_kept(self):
        app = self._app(build_fake_disc(durations=(1.0, 2.0)), args_track=9, track_no=2)
        self.assertEqual(app._valid_start_number(), 2)

    def test_insert_after_swap_uses_fallback_track(self):
        app = self._app(build_fake_disc(durations=(1.0, 2.0)), args_track=9, track_no=9)
        app._start_after_insert(auto_play=True, fresh=True)
        self.assertEqual(app.started, [(1, False)])

    def test_insert_paused_enters_stopped(self):
        app = self._app(build_fake_disc(durations=(1.0, 2.0)), args_track=None)
        app.engine = AppEngine("paused")
        app._start_after_insert(auto_play=False)
        self.assertEqual(app.engine.stop_playbacks, 1)
        self.assertEqual(app.started, [(1, True)])




class TestAppFixtureTransitions(unittest.TestCase):
    """用统一 fixture 覆盖 issue #14 要求的启动/选轨/换盘状态转换。"""

    def test_paused_overrides_auto_play(self):
        app = make_app(insert_action="play", boot_paused=True, disc_present=False)
        app._make_ripper = lambda disc, cache: app.ripper
        with mock.patch("app.Cache", return_value=app.cache), \
             mock.patch("app.Engine", return_value=app.engine):
            app._prepare_after_disc(app.session.disc, hold=False)

        self.assertEqual(app.ripper.started, 0, "--paused 必须压过 --on-insert play")
        self.assertEqual(app._start_metadata_lookup.call_count, 0)
        self.assertEqual(app.engine.state, "stopped")
        app.disc_present = True
        app.keys = Keys(["space"])
        app._handle_keys()
        self.assertEqual(app.ripper.started, 1)
        self.assertEqual(app._start_metadata_lookup.call_count, 1)

    def test_repeated_space_does_not_start_twice(self):
        app = make_app(disc_present=True)
        app.keys = Keys(["space", "space", "space"])
        app._handle_keys()
        self.assertEqual(app.ripper.started, 1)

    def test_default_pause_insert_only_reads_toc(self):
        app = make_app(insert_action="pause", disc_present=False)
        with mock.patch("app.Cache", return_value=app.cache), \
             mock.patch("app.Engine", return_value=app.engine):
            app._prepare_after_disc(app.session.disc, hold=True)
        self.assertEqual(app.ripper.started, 0)
        self.assertEqual(app._start_metadata_lookup.call_count, 0)
        self.assertIsNone(app.engine.loaded)
        self.assertEqual(app.engine.state, "stopped")

    def test_auto_play_starts_once_after_handoff(self):
        app = make_app(insert_action="play", disc_present=False)
        with mock.patch("app.Cache", return_value=app.cache), \
             mock.patch("app.Engine", return_value=app.engine):
            app._prepare_after_disc(app.session.disc, hold=True)
            self.assertEqual(app.ripper.started, 0)
            app._begin_playback()
        self.assertEqual(app.ripper.started, 1)
        self.assertEqual(app._start_metadata_lookup.call_count, 1)
        self.assertTrue(app.engine.played)

    def test_stopped_selection_never_starts_resources(self):
        app = make_app(disc_present=True)
        for key in ("n", "p", "2", "r"):
            with self.subTest(key=key):
                app = make_app(disc_present=True)
                app.keys = Keys([key])
                app._handle_keys()
                self.assertEqual(app.ripper.started, 0)
                self.assertEqual(app._start_metadata_lookup.call_count, 0)
                self.assertIsNone(app.engine.loaded)
                self.assertEqual(app.engine.state, "stopped")


class TestTrackTransitionState(unittest.TestCase):
    """切轨过渡只记录旧轨快照，不改变播放/抓轨生命周期。"""

    def _app(self, track_no: int = 1):
        disc = build_fake_disc(durations=(120.0, 180.0, 240.0))
        app = make_app(disc=disc, engine=AppEngine("stopped"),
                       track_no=track_no)
        app.engine.volume = 0.85
        app.engine.pos = 40.0
        return app

    def test_modes_snapshot_old_track(self):
        for mode, start, target, old_no, old_dur in (
                ("next", 1, 2, 1, 120.0),
                ("auto", 1, 2, 1, 120.0),
                ("prev", 2, 1, 2, 180.0)):
            with self.subTest(mode=mode):
                app = self._app(start)
                app._start_track(target, paused=True, transition=mode)
                tt = app._track_transition
                self.assertIsNotNone(tt)
                self.assertEqual(tt.mode, mode)
                self.assertEqual(tt.old_track_no, old_no)
                self.assertEqual(tt.old_title, f"Track {old_no:02d}")
                self.assertEqual(tt.old_position, 40.0)
                self.assertEqual(tt.old_duration, old_dur)
                self.assertEqual(app.track_no, target)

    def test_start_track_without_transition_clears(self):
        app = self._app(1)
        app._start_track(2, paused=True, transition="next")
        self.assertIsNotNone(app._track_transition)
        app._start_track(2, paused=True)
        self.assertIsNone(app._track_transition)

    def test_commands_pass_transition_modes(self):
        app = self._app(2)
        calls = []
        app._start_track = lambda number, start_s=0.0, paused=None, transition=None: (
            calls.append((number, transition)))
        app._cmd_next()
        app._cmd_prev()
        self.assertEqual(calls, [(3, "next"), (1, "prev")])

    def test_make_state_passes_elapsed_and_expires(self):
        app = self._app(1)
        app._start_track(2, paused=True, transition="next")
        app._track_transition.since = time.monotonic() - 0.2
        app._frozen_vis = VisFrame()
        now = time.monotonic()
        app._idle_target = app._idle_from = app._scope_mix = 1.0
        app._idle_t0 = app._idle_start = now
        app._idle_dur = 0.55
        app._idle_t = 0.0
        state = app._make_state()
        self.assertIsNotNone(state.track_transition)
        self.assertGreaterEqual(state.track_transition.elapsed, 0.19)

        app._track_transition.since = time.monotonic() - 1.2
        state = app._make_state()
        self.assertIsNone(state.track_transition)
        self.assertIsNone(app._track_transition)


class TestTrackFeedbackLifecycle(unittest.TestCase):
    """数字/连续切轨/Seek/Pause/Stop/Eject 的视觉中断规则。"""

    def _app(self, tracks: int = 9, state: str = "stopped"):
        durations = tuple(60.0 + i * 10 for i in range(tracks))
        app = make_app(disc=build_fake_disc(durations=durations),
                       engine=AppEngine(state), track_no=2)
        app.engine.pos = 12.0
        return app

    def test_digit_direction_and_same_track(self):
        app = self._app()
        app._cmd_select_digit("7")
        tt = app._track_transition
        self.assertEqual((app.track_no, tt.mode, tt.old_track_no, tt.origin_no),
                         (7, "next", 2, 2))
        # 等第一段退场过半，07 已经真正上屏，再回 02 才是可见的 prev。
        app._track_transition.since -= 0.35
        app._cmd_select_digit("2")
        tt = app._track_transition
        self.assertEqual((app.track_no, tt.mode, tt.old_track_no, tt.origin_no),
                         (2, "prev", 7, 7),
                         "07 已可见时从当前屏重新起算，避免 02→02 徽标")
        app._cmd_select_digit("2")
        self.assertEqual(app.track_no, 2)
        self.assertIsNone(app._track_transition,
                          "同轨选曲不启动新的切轨过渡")

    def test_rapid_reverse_uses_business_direction_and_visible_source(self):
        app = self._app()
        app._cmd_select_digit("7")
        app._track_transition.since -= 0.15
        before = app.feedback.visible_source(
            TrackSnapshot(number=7, title="Track 07", artist="",
                          position=0.0, duration=70.0))
        app._cmd_select_digit("3")
        tt = app._track_transition
        self.assertEqual((app.track_no, tt.mode, tt.old_track_no, tt.origin_no),
                         (3, "prev", 2, 7),
                         "07→03 按业务方向排空；起点仍是屏幕可见的 02")
        after = transition_view(
            tt,
            TrackSnapshot(number=3, title="Track 03", artist="",
                          position=0.0, duration=80.0),
            0.0,
        )
        self.assertAlmostEqual(after.track.position, before.track.position,
                               delta=0.05,
                               msg="反向切轨首帧也从 02 的可见进度接续")
        later = transition_view(
            tt,
            TrackSnapshot(number=3, title="Track 03", artist="",
                          position=0.0, duration=80.0),
            0.15,
        )
        self.assertLess(later.track.position, after.track.position,
                        "业务方向 prev 应从可见进度往 0 排空")
        app._cmd_select_digit("9")
        tt = app._track_transition
        self.assertEqual((app.track_no, tt.mode, tt.old_track_no, tt.origin_no),
                         (9, "next", 2, 7),
                         "03→09 继续累计，徽标用当前业务起点 07→09")
        self.assertEqual(app.engine.cued[1], 0.0)
        self.assertEqual(app.engine.state, "stopped")

    def test_rapid_round_trip_settles_from_visible_progress(self):
        app = self._app()
        app._cmd_select_digit("7")
        app._track_transition.since -= 0.15
        before = app.feedback.visible_source(
            TrackSnapshot(number=7, title="Track 07", artist="",
                          position=0.0, duration=70.0))
        app._cmd_select_digit("2")
        tt = app._track_transition
        self.assertIsNotNone(tt, "07→02 不能因为屏幕还显示 02 就直接跳回 0%")
        self.assertEqual((app.track_no, tt.mode, tt.old_track_no, tt.origin_no),
                         (2, "prev", 2, 7))
        after = transition_view(
            tt,
            TrackSnapshot(number=2, title="Track 02", artist="",
                          position=0.0, duration=60.0),
            0.0,
        )
        self.assertEqual(after.track.number, 2)
        self.assertAlmostEqual(after.track.position, before.track.position,
                               delta=0.05,
                               msg="回到 02 的首帧必须接着屏幕上已有的进度")
        later = transition_view(
            tt,
            TrackSnapshot(number=2, title="Track 02", artist="",
                          position=0.0, duration=60.0),
            0.15,
        )
        self.assertLess(later.track.position, after.track.position,
                        "来回后进度应从可见位置排空，而不是直接跳 0%")

    def test_rapid_sequence_rebases_after_intermediate_is_visible(self):
        app = self._app()
        app._cmd_select_digit("7")
        app._track_transition.since -= 0.35
        app._cmd_select_digit("3")
        tt = app._track_transition
        self.assertEqual((app.track_no, tt.mode, tt.old_track_no, tt.origin_no),
                         (3, "prev", 7, 7),
                         "07 已显示后方向反转，徽标和进度条都从 07 起算")

    def test_manual_selection_requests_debounced_priority(self):
        app = self._app(state="playing")
        app.session.started = True
        app._cmd_select_digit("7")
        self.assertEqual(app.ripper.requests, [(7, False)])
        app._cmd_select_digit("3")
        self.assertEqual(app.ripper.requests, [(7, False), (3, False)])

    def test_auto_eof_cached_and_resume_are_immediate(self):
        app = self._app(state="playing")
        app.session.started = True
        app.ripper.done = {3}
        app._start_track(3, transition="auto")
        self.assertEqual(app.ripper.requests, [(3, True)])

        app = self._app(state="playing")
        app.session.started = True
        app.ripper.done = {4}
        app._start_track(4)
        self.assertEqual(app.ripper.requests, [(4, True)],
                         "已缓存轨不增加机械防抖")

        app = self._app(state="playing")
        app.session.started = True
        app._start_track(2, start_s=5.0)
        self.assertEqual(app.ripper.requests, [(2, True)],
                         "缓冲断点续播立即提交当前轨")

    def test_play_resume_commits_current_priority_immediately(self):
        app = self._app(state="paused")
        app.session.started = True
        app._cmd_play_pause()
        self.assertEqual(app.engine.state, "playing")
        self.assertEqual(app.ripper.wanted, [2],
                         "从 paused/stopped 恢复播放绕过待提交窗口")

    def test_stop_cancels_pending_selection(self):
        app = self._app(state="playing")
        app.session.started = True
        app._cmd_select_digit("7")
        app._cmd_stop()
        self.assertEqual(app.ripper.cancelled, 1)

    def test_stop_pauses_ripper_and_play_resumes_it(self):
        app = self._app(state="playing")
        app.session.started = True
        app._cmd_stop()
        self.assertTrue(app.ripper.is_paused(),
                        "s 必须挂起抓轨并杀子进程，不能只取消优先级")
        self.assertEqual(app.ripper.pause_calls, 1)

        app._cmd_play_pause()
        self.assertFalse(app.ripper.is_paused())
        self.assertEqual(app.ripper.resumed, 1)
        self.assertTrue(app.engine.played)

    def test_stop_before_first_play_resumes_on_space(self):
        app = self._app(state="stopped")
        app.session.started = False
        app._cmd_stop()
        self.assertTrue(app.ripper.is_paused())
        self.assertEqual(app.ripper.started, 0)

        app._cmd_play_pause()
        self.assertFalse(app.ripper.is_paused())
        self.assertEqual(app.ripper.started, 1, "Space 才启动第一次抓轨")

    def test_stopped_track_selection_keeps_cache_paused(self):
        app = self._app(state="stopped")
        app.session.started = False
        app._cmd_stop()
        app._cmd_select_digit("2")
        self.assertTrue(app.ripper.is_paused())
        self.assertEqual(app.ripper.started, 0,
                         "停在 stopped 时选轨不能偷偷转光驱")

    def test_seek_previews_then_commits_and_cancels_transition(self):
        app = self._app(state="playing")
        app._start_track(3, transition="next")
        self.assertIsNotNone(app._track_transition)
        app._cmd_seek(5.0)
        self.assertIsNone(app._track_transition)
        self.assertEqual(app.engine.seeks, [], "Seek 不能立刻重启输出")
        self.assertAlmostEqual(app.engine.position(), 12.0)
        self.assertAlmostEqual(app._display_position(), 17.0)
        pulse = app.feedback.pulse
        self.assertEqual((pulse.kind, pulse.label, pulse.direction),
                         ("seek", "SEEK +5s", 1))
        app._commit_pending_seek(force=True)
        self.assertEqual(app.engine.seeks, [17.0])
        self.assertAlmostEqual(app.engine.position(), 17.0)

    def test_pause_cancels_transition_and_shows_state_pulse(self):
        app = self._app(state="playing")
        app._start_track(3, transition="next")
        app._cmd_play_pause()
        self.assertIsNone(app._track_transition)
        self.assertEqual(app.engine.state, "paused")
        pulse = app.feedback.pulse
        self.assertEqual((pulse.kind, pulse.label), ("paused", "PAUSED"))

    def test_make_state_carries_latest_pulse_and_expires_it(self):
        app = self._app(state="playing")
        app._cmd_seek(5.0)
        app.engine.volume = 0.85
        now = time.monotonic()
        app._idle_target = app._idle_from = app._scope_mix = 1.0
        app._idle_t0 = app._idle_start = now
        app._idle_dur = 0.55
        app._idle_t = 0.0
        app._frozen_vis = VisFrame()
        app.analyzer.frame.return_value = VisFrame()
        state = app._make_state()
        self.assertIsNone(state.track_transition)
        self.assertEqual(state.visual_pulse.label, "SEEK +5s")
        self.assertAlmostEqual(state.position, 17.0)

        app.feedback.pulse.since = time.monotonic() - 0.5
        state = app._make_state()
        self.assertIsNone(state.visual_pulse)

    def test_stop_and_eject_clear_track_and_pulse(self):
        app = self._app(state="playing")
        app._start_track(3, transition="next")
        app._cmd_seek(5.0)
        app._cmd_stop()
        self.assertIsNone(app.feedback.transition)
        self.assertIsNone(app.feedback.pulse)

        app = self._app(state="playing")
        app._start_track(3, transition="next")
        app._cmd_seek(-5.0)
        app._on_tray_open()
        self.assertIsNone(app.feedback.transition)
        self.assertIsNone(app.feedback.pulse)


class TestCacheInstallIsolation(unittest.TestCase):
    """P0①：同一张盘已经被别的实例写着时，本次改用实例私有的缓存。"""

    def test_busy_shared_cache_falls_back_to_isolated(self):
        import tempfile

        from cdda.cache import Cache, cache_root_for

        disc = build_fake_disc(durations=(1.0,))
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(cache_dir=tmp)
            root = cache_root_for(app.args)
            holder = Cache(root)
            holder.ensure_format()
            self.assertTrue(holder.acquire(disc))
            part = holder.part_path(disc, 1)
            part.write_bytes(b"live")
            try:
                cache = app._install_cache(disc)
                try:
                    self.assertFalse(cache.shared,
                                     "抢不到共享锁必须换成隔离缓存")
                    self.assertNotEqual(cache.root, root)
                    self.assertTrue(part.exists(), "隔离缓存不能动别人的 .part")
                finally:
                    cache.close()
            finally:
                holder.close()

    def test_fake_mode_gets_its_own_subdir(self):
        import pathlib
        import tempfile

        from cdda.cache import cache_root_for

        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(cache_dir=tmp)
            cache = app._install_cache(build_fake_disc(durations=(1.0,)))
            try:
                self.assertEqual(cache.root, cache_root_for(app.args))
                self.assertEqual(cache.root.parent, pathlib.Path(tmp))
                self.assertEqual(cache.root.name, "fake")
            finally:
                cache.close()
