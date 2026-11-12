"""开仓/关仓状态机、A+ 无盘界面和压缩转场的测试。"""
from __future__ import annotations

import threading
import time
import unittest

from cdda.device import Disc, UnreadableDiscError, UnsupportedDiscError
from cdda.discmon import (DiscMonitor, TRAY_CLOSED_DISC, TRAY_CLOSED_EMPTY,
                          TRAY_OPEN)
from cdda.fake import build_fake_disc
import config
from ui.glyphs import strip_ansi, text_width
from ui.no_disc import render as render_no_disc
from ui.transition import squash


class _Screen:
    def __init__(self, w: int = 105, h: int = 25):
        self.w, self.h = w, h
        self.rows: list[str] = []

    def size(self):
        return self.w, self.h

    def draw(self, rows):
        self.rows = list(rows)


class _Engine:
    def __init__(self, state: str = "playing"):
        self.state = state
        self.stopped = 0

    def stop(self):
        self.stopped += 1
        self.state = "stopped"

    def stop_playback(self):
        self.state = "stopped"

    def position(self):
        return 0.0


class _Ripper:
    def __init__(self):
        self.stopped = 0

    def stop_and_join(self, timeout: float = 10.0,
                      preserve_part: bool = False):
        self.stopped += 1
        return True


class _SlowRipper:
    """模拟 stop_and_join 很久才退出；用来证明开仓首帧不被 join 卡住。"""

    def __init__(self):
        self.calls: list[tuple[float, bool]] = []

    def stop_and_join(self, timeout: float = 10.0,
                      preserve_part: bool = False) -> bool:
        self.calls.append((timeout, preserve_part))
        time.sleep(min(max(timeout, 0.0), 0.2))
        return timeout >= 0.2

    def is_alive(self) -> bool:
        return True


class _Monitor:
    def __init__(self, tray, disc=None, error=None):
        self.tray = tray
        self.disc = disc
        self.error = error

    def set(self, tray, disc=None, error=None):
        self.tray, self.disc, self.error = tray, disc, error

    def tray_snapshot(self):
        return self.tray, time.monotonic(), self.disc

    def error_snapshot(self):
        return self.error


def _rows(tag: str) -> list[str]:
    return [f"{tag:<105}"[:105] for _ in range(25)]


def _make_app(*, tray_ui="", disc_present=True, tray=TRAY_CLOSED_DISC,
              monitor=None, engine=None, ripper=None):
    from app import App
    app = App.__new__(App)
    app.screen = _Screen()
    app.engine = engine or _Engine()
    app.ripper = ripper or _Ripper()
    app.disc = build_fake_disc(durations=(1.0,))
    app.cache = None
    app.theme_name = "phosphor"
    app.monitor = monitor
    app.disc_present = disc_present
    app.disc_state = "ready" if disc_present else "no_disc"
    app.tray_state = tray
    app.tray_ui = tray_ui
    app._tray_transition = None
    app._tray_ready_at = 0.0
    app._last_disc = None
    app._last_dash_rows = _rows("playing")
    app._tray_since = time.monotonic()
    app._ripper_stuck = False
    app._ripper_pending = False
    app._autopaused = False
    app.message = ""
    app._message_until = 0.0
    app.running = True
    app._set_autopaused = lambda on: None
    app._clear_message = lambda: None
    app._retire_ripper = lambda preserve_part=False, timeout=None: True
    app._flash = lambda *a, **k: None
    app._cached_count = lambda disc: 0
    app._render_dash_rows = lambda: _rows("new dash")
    app._load_calls = []
    app._resume_calls = []

    def load(disc):
        app._load_calls.append(disc)
        app.disc = disc
        app.disc_present = True
        app.disc_state = "ready"
    def resume(disc):
        app._resume_calls.append(disc)
        app.disc = disc
        app.disc_present = True
        app.disc_state = "ready"
    app._load_disc = load
    app._resume_disc = resume
    return app


class TestNoDiscScreen(unittest.TestCase):
    def setUp(self):
        self.theme = config.THEMES["phosphor"]

    def test_open_is_not_no_disc(self):
        rows = render_no_disc(105, 25, self.theme, "open", 0.4)
        text = "\n".join(strip_ansi(r) for r in rows)
        self.assertEqual(len(rows), 25)
        self.assertTrue(all(text_width(r) == 105 for r in rows))
        self.assertIn("TRAY OPEN", text)
        self.assertIn("INSERT CD", text)
        self.assertNotIn("NO DISC", text, "开仓时不能断言无盘")

    def test_closed_empty_is_no_disc(self):
        text = "\n".join(strip_ansi(r) for r in
                         render_no_disc(105, 25, self.theme, "closed_empty", 0.4))
        self.assertIn("NO DISC", text)
        self.assertIn("INSERT CD", text)

    def test_checking_scans_and_reading_completes(self):
        for phase, word in (("checking", "CHECKING"), ("reading", "TOC READY")):
            text = "\n".join(strip_ansi(r) for r in
                             render_no_disc(105, 25, self.theme, phase, 0.4))
            self.assertIn(word, text)
            self.assertIn("LEAD-IN", text)
            self.assertNotIn("NO DISC", text)
            self.assertNotIn("%", text, "TOC 等待阶段不显示假百分比")
        checking = "\n".join(strip_ansi(r) for r in
                              render_no_disc(105, 25, self.theme, "checking", 0.4))
        self.assertNotIn("TOC OK", checking)
        reading = "\n".join(strip_ansi(r) for r in
                             render_no_disc(105, 25, self.theme, "reading", 0.4))
        self.assertIn("▓" * 34, reading)

    def test_unsupported_is_explicit_and_not_no_disc(self):
        text = "\n".join(strip_ansi(r) for r in
                         render_no_disc(105, 25, self.theme,
                                        "unsupported", 0.4))
        self.assertIn("UNSUPPORTED", text)
        self.assertIn("AUDIO CD ONLY", text)
        self.assertNotIn("NO DISC", text)
        self.assertNotIn("CHECKING", text)

    def test_unreadable_is_explicit_and_not_no_disc(self):
        text = "\n".join(strip_ansi(r) for r in
                         render_no_disc(105, 25, self.theme,
                                        "unreadable", 0.4))
        self.assertIn("UNREADABLE", text)
        self.assertIn("CHECK DISC", text)
        self.assertNotIn("NO DISC", text)
        self.assertNotIn("CHECKING", text)

    def test_ready_uses_disc_numbers(self):
        text = "\n".join(strip_ansi(r) for r in
                         render_no_disc(105, 25, self.theme, "ready", 0.4,
                                        tracks=14, total_seconds=3690,
                                        cached=2))
        self.assertIn("CD READY", text)
        self.assertIn("14 TRK", text)
        self.assertIn("1:01:30", text)
        self.assertIn("02/14", text)

    def test_scanlines_are_still_rendered(self):
        rows = render_no_disc(105, 25, self.theme, "open", 0.4)
        # 扫描线把 LCD 奇数行压暗；亮行和暗行的 ANSI 序列不应完全一致
        self.assertNotEqual(rows[6], rows[7])


class TestSquash(unittest.TestCase):
    def setUp(self):
        self.theme = config.THEMES["phosphor"]
        self.src = [f"SRC {i}" for i in range(25)]
        self.dst = [f"DST {i}" for i in range(25)]

    def test_endpoints_and_height(self):
        for p in (0.0, 0.5, 1.0):
            rows = squash(self.src, self.dst, p, self.theme)
            self.assertEqual(len(rows), 25)
            self.assertTrue(all(text_width(r) == 6 for r in rows))
        self.assertEqual(strip_ansi(squash(self.src, self.dst, 0, self.theme)[0]).rstrip(),
                         "SRC 0")
        self.assertEqual(strip_ansi(squash(self.src, self.dst, 1, self.theme)[0]).rstrip(),
                         "DST 0")

    def test_edge_is_a_static_line_no_noise(self):
        rows = squash(self.src, self.dst, 0.5, self.theme)
        edges = [strip_ansi(r) for r in rows if set(strip_ansi(r)) == {"─"}]
        self.assertEqual(len(edges), 1, "接缝必须是一条静态亮线")
        self.assertEqual(len(edges[0]), 6)


class TestTrayStateMachine(unittest.TestCase):
    def test_slow_ripper_does_not_block_open_animation(self):
        from app import App

        slow = _SlowRipper()
        app = _make_app(ripper=slow)
        app._retire_ripper = App._retire_ripper.__get__(app, App)
        app._flash = lambda *a, **k: None
        t0 = time.monotonic()
        app._on_tray_open()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.05, "开仓不能同步等抓轨线程 join")
        self.assertEqual(slow.calls, [(0.0, True)])
        self.assertTrue(app._ripper_stuck)
        self.assertEqual(app.tray_ui, "open")
        self.assertIsNotNone(app._tray_transition)
        self.assertEqual(app.engine.stopped, 1)

    def test_open_stops_and_freezes_playing_frame(self):
        app = _make_app()
        app._on_tray_open()
        self.assertEqual(app.tray_ui, "open")
        self.assertFalse(app.disc_present)
        self.assertEqual(app.engine.stopped, 1)
        self.assertEqual(app._tray_transition["source"], _rows("playing"),
                         "压缩源必须是最后一帧 playing")
        target = "\n".join(strip_ansi(r) for r in app._tray_transition["target"])
        self.assertIn("TRAY OPEN", target)
        self.assertNotIn("NO DISC", target)

    def test_open_without_cached_frame_renders_source(self):
        app = _make_app()
        app._last_dash_rows = None
        app._on_tray_open()
        self.assertEqual(app._tray_transition["source"], _rows("new dash"))

    def test_open_from_closed_empty_skips_squash(self):
        app = _make_app(tray_ui="closed_empty", disc_present=False,
                        tray=TRAY_CLOSED_EMPTY)
        app._on_tray_open()
        self.assertEqual(app.tray_ui, "open")
        self.assertIsNone(app._tray_transition)
        app._draw()
        self.assertIn("TRAY OPEN", "\n".join(
            strip_ansi(r) for r in app.screen.rows))

    def test_open_transition_finishes_on_open_card(self):
        app = _make_app()
        app._on_tray_open()
        app._tray_transition["started"] -= 1.0     # 转场已结束
        app._update_tray_transition(time.monotonic())
        self.assertIsNone(app._tray_transition)
        app._draw()
        text = "\n".join(strip_ansi(r) for r in app.screen.rows)
        self.assertIn("TRAY OPEN", text)
        self.assertNotIn("NO DISC", text)

    def test_close_empty_stays_on_no_disc(self):
        app = _make_app(tray_ui="open", disc_present=False, tray=TRAY_OPEN)
        app._on_tray_closed_empty()
        self.assertEqual(app.tray_ui, "closed_empty")
        app._draw()
        text = "\n".join(strip_ansi(r) for r in app.screen.rows)
        self.assertIn("NO DISC", text)
        self.assertNotIn("NOW PLAYING", text)

    def test_close_disc_branches_new_and_returns(self):
        old = build_fake_disc(durations=(1.0,))
        new = build_fake_disc(durations=(2.0,))
        app = _make_app(tray_ui="open", disc_present=False, tray=TRAY_OPEN)
        app.disc = old
        app._last_disc = old
        app._begin_tray_check()
        self.assertIn("CHECKING", "\n".join(
            strip_ansi(r) for r in app._no_disc_rows("checking")))
        app._on_tray_disc(new)
        self.assertEqual(app._load_calls, [new])
        self.assertEqual(app.tray_ui, "reading")
        app._tray_ready_at = 0.0
        app._tray_tick(time.monotonic())
        self.assertEqual(app.tray_ui, "ready")
        app._tray_ready_at = 0.0
        app._tray_tick(time.monotonic())
        self.assertEqual(app.tray_ui, "return")
        self.assertIsNotNone(app._tray_transition)
        app._tray_transition["started"] -= 1.0
        app._update_tray_transition(time.monotonic())
        self.assertEqual(app.tray_ui, "")
        app._draw()
        self.assertIn("new dash", "\n".join(strip_ansi(r) for r in app.screen.rows))

    def test_close_same_disc_resumes(self):
        old = build_fake_disc(durations=(1.0,))
        again = Disc(device=old.device, tracks=old.tracks,
                     leadout_lba=old.leadout_lba)
        app = _make_app(tray_ui="open", disc_present=False, tray=TRAY_OPEN)
        app.disc = old
        app._last_disc = old
        app._on_tray_disc(again)
        self.assertEqual(app._resume_calls, [again])
        self.assertEqual(app.tray_ui, "reading")

    def test_poll_open_then_close_empty(self):
        monitor = _Monitor(TRAY_CLOSED_DISC)
        app = _make_app(monitor=monitor)
        monitor.set(TRAY_OPEN, error=None)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "open")
        self.assertFalse(app.disc_present)
        monitor.set(TRAY_CLOSED_EMPTY)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "closed_empty")
        app._draw()
        self.assertIn("NO DISC", "\n".join(
            strip_ansi(r) for r in app.screen.rows))

    def test_poll_open_then_close_new_disc(self):
        new = build_fake_disc(durations=(2.0,))
        monitor = _Monitor(TRAY_CLOSED_DISC)
        app = _make_app(monitor=monitor)
        monitor.set(TRAY_OPEN)
        app._poll_disc()
        monitor.set(TRAY_CLOSED_DISC, new)
        app._poll_disc()
        self.assertEqual(app._load_calls, [new])
        self.assertEqual(app.tray_ui, "reading")

    def test_poll_normal_to_closed_empty_fallback(self):
        monitor = _Monitor(TRAY_CLOSED_DISC)
        app = _make_app(monitor=monitor)
        monitor.set(TRAY_CLOSED_EMPTY)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "closed_empty")
        self.assertFalse(app.disc_present)

    def test_fast_swap_without_open_edge_goes_checking(self):
        """app 的防御分支；真实 DiscMonitor 无法在没有边沿时发现换盘，见下。"""
        old = build_fake_disc(durations=(1.0,))
        new = build_fake_disc(durations=(2.0,))
        monitor = _Monitor(TRAY_CLOSED_DISC, new)
        app = _make_app(monitor=monitor)
        app.disc = old
        app.disc_present = True
        app._poll_disc()
        self.assertEqual(app._load_calls, [new])
        self.assertEqual(app.tray_ui, "reading")
        self.assertEqual(app.engine.stopped, 1)

    def test_small_window_compact_no_faceplate(self):
        app = _make_app()
        app.screen = _Screen(60, 12)
        rows = app._no_disc_rows("open")
        self.assertEqual(len(rows), 12)
        self.assertIn("TRAY OPEN", "\n".join(strip_ansi(r) for r in rows))
        self.assertNotIn("┌", "\n".join(strip_ansi(r) for r in rows))

    def test_tick_drives_open_and_closed_empty(self):
        monitor = _Monitor(TRAY_CLOSED_DISC)
        app = _make_app(monitor=monitor)
        monitor.set(TRAY_OPEN)
        app._tick()
        self.assertEqual(app.tray_ui, "open")
        self.assertEqual(app.engine.stopped, 1)
        monitor.set(TRAY_CLOSED_EMPTY)
        app._tick()
        self.assertEqual(app.tray_ui, "closed_empty")
        self.assertFalse(app.disc_present)

    def test_real_monitor_supported_swap_after_closed_empty(self):
        disc_a = build_fake_disc(durations=(1.0,))
        disc_b = build_fake_disc(durations=(2.0,))
        state = {"tray": TRAY_CLOSED_DISC, "disc": disc_a}

        def probe():
            return state["tray"]

        def read():
            return state["disc"]

        monitor = DiscMonitor(probe, read)
        monitor.prime(TRAY_CLOSED_DISC, disc_a)
        app = _make_app(monitor=monitor)
        app.disc = disc_a
        app.tray_state = TRAY_CLOSED_DISC
        app.disc_present = True
        app._last_disc = None

        state["tray"] = TRAY_CLOSED_EMPTY
        monitor.poll_once()
        app._poll_disc()
        self.assertEqual(app.tray_ui, "closed_empty")
        self.assertIs(app._last_disc, disc_a)

        state["tray"] = TRAY_CLOSED_DISC
        state["disc"] = disc_b
        monitor.poll_once()
        app._poll_disc()
        self.assertEqual(app._load_calls, [disc_b])
        self.assertEqual(app.tray_ui, "reading")

    def test_real_monitor_needs_closed_empty_edge(self):
        disc_a = build_fake_disc(durations=(1.0,))
        disc_b = build_fake_disc(durations=(2.0,))
        state = {"tray": TRAY_CLOSED_DISC, "disc": disc_a}
        reads: list[int] = []

        def probe():
            return state["tray"]

        def read():
            reads.append(1)
            return state["disc"]

        monitor = DiscMonitor(probe, read)
        monitor.prime(TRAY_CLOSED_DISC, disc_a)
        monitor.poll_once()
        self.assertIs(monitor.tray_snapshot()[2], disc_a)
        self.assertEqual(reads, [])
        # 纯 closed-disc -> closed-disc 的快速换盘：DRIVE_STATUS 轮询看不到
        state["disc"] = disc_b
        monitor.poll_once()
        self.assertIs(monitor.tray_snapshot()[2], disc_a)
        self.assertEqual(reads, [], "没有 open/closed-empty 边沿时不应重新读 TOC")
        # 只要看到一次 closed-empty，再关仓就能重新发现
        state["tray"] = TRAY_CLOSED_EMPTY
        monitor.poll_once()
        self.assertIsNone(monitor.tray_snapshot()[2])
        state["tray"] = TRAY_CLOSED_DISC
        monitor.poll_once()
        self.assertIs(monitor.tray_snapshot()[2], disc_b)
        self.assertEqual(reads, [1])

    def test_closed_empty_remembers_last_disc_for_same_reinsert(self):
        old = build_fake_disc(durations=(1.0,))
        again = Disc(device=old.device, tracks=old.tracks,
                     leadout_lba=old.leadout_lba)
        app = _make_app(tray_ui="open", disc_present=False,
                        tray=TRAY_CLOSED_EMPTY)
        app.disc = old
        app._last_disc = None
        app._on_tray_closed_empty()
        self.assertIs(app._last_disc, old)
        app._begin_tray_check()
        app._on_tray_disc(again)
        self.assertEqual(app._resume_calls, [again])

    def test_unknown_tray_keeps_state(self):
        app = _make_app(monitor=_Monitor(None))
        app._poll_disc()
        self.assertEqual(app.tray_ui, "")
        self.assertTrue(app.disc_present)

    def test_unreadable_stops_without_loading(self):
        error = UnreadableDiscError("有盘但读不出 TOC")
        monitor = _Monitor(TRAY_CLOSED_DISC, None, error=error)
        app = _make_app(tray_ui="checking", disc_present=False,
                        tray=TRAY_CLOSED_DISC, monitor=monitor)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "unreadable")
        self.assertEqual(app.disc_state, "unreadable")
        self.assertFalse(app.disc_present)
        self.assertEqual(app.engine.stopped, 1)
        self.assertEqual(app._load_calls, [], "读不出的盘不能启动装盘流程")
        self.assertIn("UNREADABLE", "\n".join(
            strip_ansi(r) for r in app._no_disc_rows("unreadable")))

    def test_unsupported_stops_without_loading(self):
        error = UnsupportedDiscError("数据盘，只支持音乐 CD")
        monitor = _Monitor(TRAY_CLOSED_DISC, None, error=error)
        app = _make_app(tray_ui="checking", disc_present=False,
                        tray=TRAY_CLOSED_DISC, monitor=monitor)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "unsupported")
        self.assertEqual(app.disc_state, "unsupported")
        self.assertFalse(app.disc_present)
        self.assertEqual(app.engine.stopped, 1)
        self.assertEqual(app._load_calls, [], "数据盘不能启动装盘流程")
        self.assertIn("UNSUPPORTED", "\n".join(
            strip_ansi(r) for r in app._no_disc_rows("unsupported")))

    def test_real_monitor_data_disc_then_audio_recovers(self):
        disc_a = build_fake_disc(durations=(1.0,))
        state = {"tray": TRAY_CLOSED_DISC, "data": False}

        def probe():
            return state["tray"]

        def read():
            if state["data"]:
                raise UnsupportedDiscError("data DVD")
            return disc_a

        monitor = DiscMonitor(probe, read)
        monitor.prime(TRAY_CLOSED_DISC, disc_a)
        app = _make_app(monitor=monitor)
        app.disc = disc_a
        app._last_disc = None

        state["tray"] = TRAY_OPEN
        monitor.poll_once()
        app._poll_disc()
        self.assertEqual(app.tray_ui, "open")

        state["data"] = True
        state["tray"] = TRAY_CLOSED_DISC
        monitor.poll_once()
        app._poll_disc()
        self.assertEqual(app.tray_ui, "unsupported")
        self.assertEqual(app._load_calls, [])
        self.assertEqual(app._resume_calls, [])

        state["tray"] = TRAY_OPEN
        monitor.poll_once()
        app._poll_disc()
        self.assertEqual(app.tray_ui, "open")

        state["data"] = False
        state["tray"] = TRAY_CLOSED_DISC
        monitor.poll_once()
        app._poll_disc()
        self.assertEqual(app._resume_calls, [disc_a])
        self.assertEqual(app.tray_ui, "reading")

    def test_unsupported_then_audio_disc_recovers(self):
        error = UnsupportedDiscError("数据盘，只支持音乐 CD")
        monitor = _Monitor(TRAY_CLOSED_DISC, None, error=error)
        app = _make_app(tray_ui="checking", disc_present=False,
                        tray=TRAY_CLOSED_DISC, monitor=monitor)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "unsupported")

        monitor.set(TRAY_OPEN, error=None)
        app._poll_disc()
        self.assertEqual(app.tray_ui, "open")
        self.assertIsNone(monitor.error_snapshot())

        new = build_fake_disc(durations=(2.0,))
        monitor.set(TRAY_CLOSED_DISC, new)
        app._poll_disc()
        self.assertEqual(app._load_calls, [new])
        self.assertEqual(app.tray_ui, "reading")

    def test_closed_disc_without_toc_stays_checking(self):
        app = _make_app(tray_ui="open", disc_present=False, tray=TRAY_OPEN,
                        monitor=_Monitor(TRAY_CLOSED_DISC, None))
        app._poll_disc()
        self.assertEqual(app.tray_ui, "checking")
        checking = "\n".join(
            strip_ansi(r) for r in app._no_disc_rows("checking"))
        self.assertIn("CHECKING", checking)
        self.assertIn("LEAD-IN", checking)

    def test_query_in_progress_shows_checking_before_toc_returns(self):
        """DiscMonitor 必须在阻塞读 TOC 前发布 closed-disc/None。"""
        disc = build_fake_disc(durations=(1.0,))
        started = threading.Event()
        release = threading.Event()

        def read():
            started.set()
            release.wait(2.0)
            return disc

        monitor = DiscMonitor(lambda: TRAY_CLOSED_DISC, read)
        monitor.prime(TRAY_OPEN, None)
        app = _make_app(tray_ui="open", disc_present=False, tray=TRAY_OPEN,
                        monitor=monitor)
        worker = threading.Thread(target=monitor.poll_once)
        worker.start()
        try:
            self.assertTrue(started.wait(2.0), "read_disc 没有开始")
            tray, _, pending = monitor.tray_snapshot()
            self.assertEqual(tray, TRAY_CLOSED_DISC)
            self.assertIsNone(pending)
            self.assertEqual(monitor.snapshot(), (True, None))
            app._poll_disc()
            self.assertEqual(app.tray_ui, "checking")
            checking = "\n".join(
                strip_ansi(r) for r in app._no_disc_rows("checking"))
            self.assertIn("CHECKING", checking)
            self.assertIn("LEAD-IN", checking)
        finally:
            release.set()
            worker.join(2.0)
        self.assertFalse(worker.is_alive(), "read_disc 没有退出")
        app._poll_disc()
        self.assertEqual(app.tray_ui, "reading")
        reading = "\n".join(
            strip_ansi(r) for r in app._no_disc_rows("reading"))
        self.assertIn("TOC READY", reading)


if __name__ == "__main__":
    unittest.main()
