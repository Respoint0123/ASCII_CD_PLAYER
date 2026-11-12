"""开机画面的帧渲染 + 后台读 TOC 的线程行为。"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from app import _DiscLoader
from cdda.device import (CddaError, NoMediaError, UnreadableDiscError,
                         UnsupportedDiscError)
from cdda.fake import build_fake_disc
from config import THEMES
from ui.boot import (Grid, INTRO_NAMES, BootInfo, render_frame, scene,
                     timeline)
from ui.glyphs import strip_ansi, text_width

THEME = THEMES["phosphor"]


def _frame(t: float, info: BootInfo, w: int = 105, h: int = 25) -> list[str]:
    return render_frame(w, h, THEME, t, info)


def _text(rows: list[str]) -> str:
    return "\n".join(strip_ansi(row) for row in rows)


class TestBootFrame(unittest.TestCase):
    def test_every_row_has_exact_width(self):
        for w, h in ((105, 25), (80, 24), (60, 12), (40, 10)):
            for t in (0.0, 0.1, 0.5, 2.5, 2.86, 3.5):
                rows = _frame(t, BootInfo(ready=True, tracks=16,
                                          total_seconds=3575.0, cached=7), w, h)
                self.assertEqual(len(rows), h)
                for row in rows:
                    self.assertEqual(text_width(row), w, (w, h, t))

    def test_all_four_intros_render(self):
        seen = set()
        for i in range(len(INTRO_NAMES)):
            rows = _frame(0.10, BootInfo(intro=i))
            seen.add(tuple(rows))          # 带颜色比较：背光/闪屏可能同为空格
        self.assertEqual(len(seen), len(INTRO_NAMES))

    def test_lcd_shows_real_disc_data(self):
        info = BootInfo(ready=True, tracks=16, total_seconds=3575.0,
                        cached=7)
        text = _text(_frame(2.50, info))
        self.assertIn("16 TRK", text)
        self.assertIn("59:35", text)
        self.assertIn("MEM 7/16", text)
        self.assertIn("READY", text)
        self.assertNotIn("READING", text)

    def test_toc_is_not_revealed_before_timeline(self):
        info = BootInfo(ready=True, tracks=16, total_seconds=3575.0,
                        cached=7)
        text = _text(_frame(1.50, info))
        self.assertIn("READING", text)
        self.assertIn("-- TRK", text)
        self.assertNotIn("16 TRK", text)

    def test_wait_card(self):
        info = BootInfo(no_disc=True)
        text = _text(_frame(2.0, info))
        self.assertIn("NO DISC", text)
        self.assertIn("Q", text)
        self.assertIn("-- TRK", text)

    def test_unsupported_card_is_explicit(self):
        text = _text(_frame(2.0, BootInfo(unsupported=True)))
        self.assertIn("UNSUPPORTED DISC", text)
        self.assertIn("Q", text)
        self.assertNotIn("NO DISC", text)
        self.assertNotIn("READY", text)

    def test_unreadable_card_is_explicit(self):
        text = _text(_frame(2.0, BootInfo(unreadable=True)))
        self.assertIn("UNREADABLE DISC", text)
        self.assertIn("Q", text)
        self.assertNotIn("NO DISC", text)
        self.assertNotIn("READY", text)

    def test_expands_only_when_ready(self):
        not_ready = _text(_frame(3.20, BootInfo(ready=False, tracks=16)))
        self.assertNotIn("asciicd", not_ready)
        ready = _text(_frame(2.86, BootInfo(ready=True, tracks=16,
                                            total_seconds=3575.0, cached=7)))
        self.assertIn("asciicd", ready)

    def test_hold_expand_keeps_faceplate(self):
        info = BootInfo(ready=True, hold_expand=True, tracks=12,
                        total_seconds=3257.0, cached=10)
        text = _text(_frame(3.40, info))
        self.assertNotIn("asciicd", text)
        self.assertIn("12 TRK", text)
        self.assertIn("54:17", text)

    def test_scan_area_covers_only_lcd(self):
        info = BootInfo(ready=True, tracks=16, total_seconds=3575.0, cached=7)
        g = Grid(105, 25)
        scene(g, 2.5, THEME, info)
        # 面板 80×18 居中 → LCD 78×6，从 (13,4) 到 (90,9)
        self.assertEqual(g.scan_area, (13, 4, 90, 9))

    def test_paused_lcd(self):
        info = BootInfo(ready=True, paused=True, tracks=16,
                        total_seconds=3575.0)
        self.assertIn("PAUSE", _text(_frame(2.5, info)))

    def test_fast_timeline_is_shorter(self):
        self.assertLess(timeline(True)["dash"], timeline(False)["dash"])

    def test_track_bar_reflects_cache(self):
        empty = BootInfo(ready=True, tracks=16, total_seconds=3575.0, cached=0)
        full = BootInfo(ready=True, tracks=16, total_seconds=3575.0, cached=16)
        self.assertNotIn("██", _text(_frame(2.5, empty)))   # 缓存空：没有实心段
        self.assertIn("MEM 16/16", _text(_frame(2.5, full)))


class TestDiscLoader(unittest.TestCase):
    class Args:
        fake = True
        device = None

    def _wait(self, loader: _DiscLoader, timeout: float = 2.0) -> None:
        end = time.monotonic() + timeout
        while not loader.finished() and time.monotonic() < end:
            time.sleep(0.01)

    def test_fake_disc_loaded(self):
        loader = _DiscLoader(self.Args(), wait=False, interval=0.01)
        loader.start()
        self._wait(loader)
        loader.stop()
        self.assertIsNotNone(loader.disc)
        self.assertIsNone(loader.error)

    def test_no_media_stops_when_not_waiting(self):
        args = self.Args()
        args.fake = False
        with mock.patch("cdda.loader.query_disc", side_effect=NoMediaError("no disc")):
            loader = _DiscLoader(args, wait=False, interval=0.01)
            loader.start()
            self._wait(loader)
            loader.stop()
        self.assertIsNone(loader.disc)
        self.assertIsInstance(loader.error, NoMediaError)

    def test_wait_disc_retries_until_disc_appears(self):
        args = self.Args()
        args.fake = False
        disc = build_fake_disc()
        with mock.patch("cdda.loader.query_disc", side_effect=[NoMediaError("x"), disc]):
            loader = _DiscLoader(args, wait=True, interval=0.01)
            loader.start()
            end = time.monotonic() + 2.0
            while loader.disc is None and time.monotonic() < end:
                time.sleep(0.01)
            loader.stop()
        self.assertIs(loader.disc, disc)

    def test_unsupported_without_wait_is_an_error(self):
        args = self.Args()
        args.fake = False
        err = UnsupportedDiscError("data DVD")
        with mock.patch("cdda.loader.query_disc", side_effect=err):
            loader = _DiscLoader(args, wait=False, interval=0.01)
            loader.start()
            self._wait(loader)
            loader.stop()
        self.assertTrue(loader.finished())
        self.assertIsNone(loader.disc)
        self.assertIs(loader.error, err)
        self.assertEqual(loader.state, "unsupported")

    def test_unreadable_without_wait_is_an_error(self):
        args = self.Args()
        args.fake = False
        err = UnreadableDiscError("cannot read TOC")
        with mock.patch("cdda.loader.query_disc", side_effect=err):
            loader = _DiscLoader(args, wait=False, interval=0.01)
            loader.start()
            self._wait(loader)
            loader.stop()
        self.assertTrue(loader.finished())
        self.assertIsNone(loader.disc)
        self.assertIs(loader.error, err)
        self.assertEqual(loader.state, "unreadable")

    def test_unreadable_retries_during_grace(self):
        args = self.Args()
        args.fake = False
        state = {"calls": 0}
        err = UnreadableDiscError("cannot read TOC")

        def query(device=None):
            state["calls"] += 1
            raise err

        with mock.patch("cdda.loader.query_disc", side_effect=query), \
             mock.patch("cdda.loader.DISC_UNREADABLE_S", 10.0):
            loader = _DiscLoader(args, wait=True, interval=0.01,
                                 tray_probe=lambda: "closed-disc")
            loader.start()
            end = time.monotonic() + 2.0
            while state["calls"] < 2 and time.monotonic() < end:
                time.sleep(0.01)
            loader.stop()
        self.assertGreaterEqual(state["calls"], 2)
        self.assertIsNone(loader.unreadable)
        self.assertIs(loader.error, err)

    def test_unreadable_after_grace_waits_for_tray_edge(self):
        args = self.Args()
        args.fake = False
        state = {"tray": "closed-disc", "calls": 0}
        err = UnreadableDiscError("cannot read TOC")

        def query(device=None):
            state["calls"] += 1
            raise err

        with mock.patch("cdda.loader.query_disc", side_effect=query), \
             mock.patch("cdda.loader.DISC_UNREADABLE_S", 0.0):
            loader = _DiscLoader(args, wait=True, interval=0.01,
                                 tray_probe=lambda: state["tray"])
            loader.start()
            end = time.monotonic() + 2.0
            while loader.state != "unreadable" and time.monotonic() < end:
                time.sleep(0.01)
            time.sleep(0.15)
            loader.stop()
        self.assertEqual(state["calls"], 1)
        self.assertIs(loader.unreadable, err)

    def test_retrying_query_stays_reading_while_in_flight(self):
        """第二次 cdparanoia 阻塞期间，loader 不能显示上一轮的 waiting/NO DISC。"""
        args = self.Args()
        args.fake = False
        entered = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def query(device=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CddaError("temporary TOC failure")
            entered.set()
            release.wait(2.0)
            raise CddaError("still failing")

        self.addCleanup(release.set)
        with mock.patch("cdda.loader.query_disc", side_effect=query):
            loader = _DiscLoader(args, wait=True, interval=0.01)
            loader.start()
            self.assertTrue(entered.wait(2.0), "第二次 query 没开始")
            self.assertEqual(loader.state, "reading")
            release.set()
            loader.stop()
            loader._thread.join(timeout=2.0)

    def test_generic_read_failure_keeps_reading_not_no_disc(self):
        """已知音频盘暂时读不出：loader 继续 READING/重试，不冒充无盘。"""
        args = self.Args()
        args.fake = False
        state = {"calls": 0}
        err = CddaError("TOC temporarily unreadable")

        def query(device=None):
            state["calls"] += 1
            raise err

        with mock.patch("cdda.loader.query_disc", side_effect=query):
            loader = _DiscLoader(args, wait=True, interval=0.01)
            loader.start()
            end = time.monotonic() + 2.0
            while state["calls"] < 2 and time.monotonic() < end:
                time.sleep(0.01)
            loader.stop()
        self.assertGreaterEqual(state["calls"], 2)
        self.assertIs(loader.error, err)
        self.assertEqual(loader.state, "reading")
        self.assertIsNone(loader.unreadable)
        self.assertIsNone(loader.unsupported)

    def test_unsupported_wait_does_not_reread_while_tray_is_closed(self):
        args = self.Args()
        args.fake = False
        state = {"tray": "closed-disc", "calls": 0}
        err = UnsupportedDiscError("data DVD")

        def query(device=None):
            state["calls"] += 1
            raise err

        with mock.patch("cdda.loader.query_disc", side_effect=query):
            loader = _DiscLoader(args, wait=True, interval=0.01,
                                 tray_probe=lambda: state["tray"])
            loader.start()
            end = time.monotonic() + 2.0
            while loader.state != "unsupported" and time.monotonic() < end:
                time.sleep(0.01)
            time.sleep(0.15)                 # 超过两次 interval，仍不能重跑
            loader.stop()
        self.assertEqual(state["calls"], 1)
        self.assertIs(loader.error, err)

    def test_tray_edge_clears_unsupported(self):
        args = self.Args()
        args.fake = False
        err = UnsupportedDiscError("data DVD")
        loader = _DiscLoader(args, wait=True, interval=0.001,
                             tray_probe=lambda: "open")
        loader.error = loader.unsupported = err
        loader.state = "unsupported"
        loader._wait_for_tray_change()
        self.assertIsNone(loader.error)
        self.assertIsNone(loader.unsupported)
        self.assertEqual(loader.state, "waiting")

    def test_unsupported_then_audio_cd_loads(self):
        args = self.Args()
        args.fake = False
        disc = build_fake_disc()
        state = {"tray": "closed-disc", "calls": 0}

        def query(device=None):
            state["calls"] += 1
            if state["calls"] == 1:
                raise UnsupportedDiscError("data DVD")
            if state["tray"] != "closed-disc":
                raise NoMediaError("tray open")
            return disc

        with mock.patch("cdda.loader.query_disc", side_effect=query):
            loader = _DiscLoader(args, wait=True, interval=0.01,
                                 tray_probe=lambda: state["tray"])
            loader.start()
            end = time.monotonic() + 2.0
            while loader.state != "unsupported" and time.monotonic() < end:
                time.sleep(0.01)
            state["tray"] = "open"          # 换盘
            end = time.monotonic() + 2.0
            while loader.state == "unsupported" and time.monotonic() < end:
                time.sleep(0.01)
            state["tray"] = "closed-disc"
            end = time.monotonic() + 2.0
            while loader.disc is None and time.monotonic() < end:
                time.sleep(0.01)
            loader.stop()
        self.assertIs(loader.disc, disc)
        self.assertIsNone(loader.error)
        self.assertEqual(loader.state, "ready")


if __name__ == "__main__":
    unittest.main()
