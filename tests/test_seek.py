"""连续 Seek 合并：预览、安静窗口提交与生命周期边界。"""
from __future__ import annotations

import unittest
from unittest import mock

from cdda.fake import build_fake_disc
from config import (BYTES_PER_SEC, SEEK_ARROW_3_S, SEEK_DEBOUNCE_S,
                    SEEK_HOLD_GAP_S, SEEK_HOLD_RELEASE_S)
from dsp.analyzer import VisFrame
from tests.support import AppEngine, Keys, make_app
from ui.glyphs import strip_ansi
from ui.layouts import render


class TestPendingSeek(unittest.TestCase):
    def _app(self, *, state: str = "playing", position: float = 10.0,
             durations: tuple[float, ...] = (30.0,) * 10,
             clock: list[float] | None = None):
        clock = clock if clock is not None else [100.0]
        app = make_app(disc=build_fake_disc(durations=durations),
                       engine=AppEngine(state))
        app._clock = lambda: clock[0]
        app._idle_t0 = app._idle_start = clock[0]
        app.engine.pos = position
        app.engine.volume = 0.85
        app.analyzer.frame.return_value = VisFrame()
        return app, clock

    def test_preview_is_immediate_but_output_is_not_restarted(self):
        app, _clock = self._app()
        app._cmd_seek(5.0)
        self.assertEqual(app.engine.seeks, [])
        self.assertAlmostEqual(app.engine.position(), 10.0)
        self.assertAlmostEqual(app._pending_seek_target, 15.0)
        self.assertAlmostEqual(app._display_position(), 15.0)
        self.assertAlmostEqual(app._make_state().position, 15.0)
        self.assertEqual(app.feedback.pulse.label, "SEEK +5s")

    def test_quiet_window_commits_final_target_only_once(self):
        app, clock = self._app()
        app._cmd_seek(5.0, now=100.0)

        clock[0] = 100.0 + SEEK_DEBOUNCE_S - 0.001
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [])

        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertTrue(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [15.0])
        self.assertIsNone(app._pending_seek_target)

        clock[0] += 5.0
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [15.0], "同一批只提交一次")

    def test_rapid_sequence_accumulates_from_pending_not_stale_head(self):
        app, clock = self._app()
        for at, delta in ((100.00, 5.0), (100.05, 5.0),
                          (100.10, 5.0), (100.15, -5.0)):
            clock[0] = at
            app._cmd_seek(delta)
        self.assertAlmostEqual(app._pending_seek_target, 20.0,
                               msg="→→→← 应从累计目标得到净 +10s")
        self.assertEqual(app.feedback.pulse.label, "SEEK +10s")
        self.assertEqual(app.feedback.pulse.direction, 1)
        self.assertEqual(app.engine.seeks, [])

        clock[0] = 100.15 + SEEK_DEBOUNCE_S - 0.001
        self.assertFalse(app._commit_pending_seek())
        clock[0] = 100.15 + SEEK_DEBOUNCE_S
        self.assertTrue(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [20.0])

    def test_same_frame_key_batch_accumulates(self):
        app, _clock = self._app()
        app.keys = Keys(["right", "right", "right", "left"])
        app._handle_keys()
        self.assertAlmostEqual(app._pending_seek_target, 20.0)
        self.assertEqual(app.engine.seeks, [])
        self.assertEqual(app.feedback.pulse.label, "SEEK +10s")

    def test_net_zero_sequence_does_not_restart_output(self):
        app, clock = self._app()
        app._cmd_seek(5.0, now=100.0)
        app._cmd_seek(-5.0, now=100.1)
        self.assertAlmostEqual(app._pending_seek_target, 10.0)
        self.assertEqual(app.feedback.pulse.label, "SEEK +0s")
        self.assertEqual(app.feedback.pulse.direction, 0)

        clock[0] = 100.1 + SEEK_DEBOUNCE_S
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [], "净位移为 0 不该白断一次声音")

    def test_clamps_at_track_bounds(self):
        app, clock = self._app(position=29.0, durations=(30.0,) * 3)
        app._cmd_seek(5.0)
        self.assertAlmostEqual(app._pending_seek_target, 29.95)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertTrue(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [29.95])

        app, clock = self._app(position=0.0, durations=(30.0,) * 3)
        app._cmd_seek(-5.0)
        self.assertAlmostEqual(app._pending_seek_target, 0.0)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [])

    def test_tick_sampling_does_not_extend_deadline(self):
        app, clock = self._app()
        app.ripper.avail = app.disc.track(1).pcm_bytes
        app._cmd_seek(5.0, now=100.0)

        for at in (100.05, 100.10, 100.17):
            clock[0] = at
            app._tick()
        self.assertEqual(app.engine.seeks, [],
                         "重复帧不能把 trailing-edge 窗口无限推迟")

        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        app._tick()
        self.assertEqual(app.engine.seeks, [15.0])
        clock[0] += 0.5
        app._tick()
        self.assertEqual(app.engine.seeks, [15.0])

    def _pulse_row(self, app) -> str:
        state = app._make_state()
        rows = [strip_ansi(row) for row in render(state, 105, 25)]
        return next(row for row in rows if "NOW PLAYING" in row)

    def test_seek_arrow_grows_on_hold_and_decays_on_release(self):
        app, clock = self._app()
        app._cmd_seek(5.0, now=100.0)
        row = self._pulse_row(app)
        self.assertIn("» SEEK", row)
        self.assertNotIn("»»", row, "短按只显示一个箭头")

        for _ in range(12):
            clock[0] += 0.03
            app.keys = Keys(["right"])
            app._handle_keys()
        self.assertGreaterEqual(app.feedback.pulse.hold, SEEK_ARROW_3_S)
        row = self._pulse_row(app)
        self.assertIn("»»» SEEK", row, "长按应逐渐增加到 3 个箭头")

        last = app.feedback.pulse.last_input_at
        clock[0] = last + SEEK_HOLD_GAP_S + SEEK_HOLD_RELEASE_S * 0.5
        row = self._pulse_row(app)
        self.assertIn("»» SEEK", row, "松开后先回落到 2 个箭头")

        clock[0] = last + SEEK_HOLD_GAP_S + SEEK_HOLD_RELEASE_S
        row = self._pulse_row(app)
        self.assertIn("» SEEK", row, "最后回落到 1 个箭头")
        self.assertNotIn("»»", row)

    def test_paused_seek_keeps_paused_target(self):
        app, clock = self._app(state="paused")
        app._cmd_seek(5.0)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertTrue(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [15.0])
        self.assertEqual(app.engine.state, "paused")
        self.assertFalse(app.engine.played)

    def test_stopped_seek_only_moves_head(self):
        app, clock = self._app(state="stopped")
        app._cmd_seek(5.0)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertTrue(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [15.0])
        self.assertEqual(app.engine.state, "stopped")

    def test_space_flushes_pending_before_toggle(self):
        app, _clock = self._app(state="paused")
        app._cmd_seek(5.0)
        app._cmd_play_pause()
        self.assertEqual(app.engine.seeks, [15.0],
                         "Space 前必须先结清用户最后看到的 Seek 目标")
        self.assertIsNone(app._pending_seek_target)
        self.assertEqual(app.engine.state, "playing")

    def test_space_pauses_at_seek_target_not_old_head(self):
        app, _clock = self._app(state="playing")
        app._cmd_seek(5.0)
        app._cmd_play_pause()
        self.assertEqual(app.engine.seeks, [15.0])
        self.assertEqual(app.engine.state, "paused")
        self.assertIsNone(app._pending_seek_target)

    def test_old_pending_rejected_when_disc_or_track_changes(self):
        app, clock = self._app()
        app._cmd_seek(5.0)
        changed = build_fake_disc(durations=(40.0,) * 3)
        app.session.disc = changed                 # 防御：绕过正常换盘流程
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertFalse(app._commit_pending_seek())
        self.assertIsNone(app._pending_seek_target)
        self.assertEqual(app.engine.seeks, [])

        app, clock = self._app()
        app._cmd_seek(5.0)
        app.track_no = 2                           # 防御：业务轨号被直接改掉
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertFalse(app._commit_pending_seek())
        self.assertIsNone(app._pending_seek_target)
        self.assertEqual(app.engine.seeks, [])

    def test_stop_discards_pending(self):
        app, clock = self._app()
        app._cmd_seek(5.0)
        app._cmd_stop()
        self.assertIsNone(app._pending_seek_target)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S + 1.0
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [])

    def test_track_switch_discards_pending(self):
        app, clock = self._app()
        app._cmd_seek(5.0)
        app._start_track(2)
        self.assertIsNone(app._pending_seek_target)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S + 1.0
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [])

    def test_eject_discards_pending(self):
        app, clock = self._app()
        app._cmd_seek(5.0)
        app._on_tray_open()
        self.assertIsNone(app._pending_seek_target)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S + 1.0
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [])

    def test_shutdown_discards_pending(self):
        app, clock = self._app()
        app._cmd_seek(5.0)
        app.screen = mock.Mock()
        app._save_theme = mock.Mock()
        app._clear_audio_cache = mock.Mock()
        app.shutdown()
        self.assertIsNone(app._pending_seek_target)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S + 1.0
        self.assertFalse(app._commit_pending_seek())
        self.assertEqual(app.engine.seeks, [])

    def test_cache_lead_and_eof_use_real_head_not_preview(self):
        app, _clock = self._app()
        app.ripper.avail = 11 * BYTES_PER_SEC
        app._cmd_seek(10.0)
        self.assertAlmostEqual(app._cache_lead(), 1.0, places=2,
                               msg="缓冲水位不能被预览目标伪造")
        self.assertEqual(app.engine.state, "playing",
                         "预览目标不能提前制造 EOF/暂停")

    def test_seek_into_unripped_region_waits_instead_of_eof(self):
        app, clock = self._app(position=10.0, durations=(30.0,))
        app.ripper.avail = 11 * BYTES_PER_SEC       # 只抓到 11s
        app._cmd_seek(10.0)                          # 目标 20s，尚未抓出
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        app._tick()

        self.assertEqual(app.engine.seeks, [20.0])
        self.assertTrue(app._autopaused,
                        "目标落在未抓取区应进入缓冲等待，而不是 EOF")
        self.assertTrue(app.engine.paused)

    def test_buffering_seek_resumes_when_cache_catches_up(self):
        app, clock = self._app(state="paused", position=10.0,
                               durations=(30.0,))
        app._set_autopaused(True)
        app.ripper.avail = 25 * BYTES_PER_SEC
        app._cmd_seek(5.0)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        app._tick()
        self.assertEqual(app.engine.seeks, [15.0])
        self.assertTrue(app.engine.played)
        self.assertFalse(app._autopaused)

    def test_auto_eof_discards_pending_before_next_track(self):
        app, clock = self._app(position=29.9, durations=(30.0, 30.0))
        app.ripper.done = {1}
        app.engine.state = "eof"
        app._cmd_seek(5.0)
        app._tick()
        self.assertIsNone(app._pending_seek_target)
        self.assertEqual(app.track_no, 2)
        self.assertEqual(app.engine.seeks, [])
        clock[0] = 100.0 + SEEK_DEBOUNCE_S + 1.0
        self.assertFalse(app._commit_pending_seek())

    def test_seek_after_commit_uses_new_head_as_origin(self):
        app, clock = self._app()
        app._cmd_seek(5.0, now=100.0)
        clock[0] = 100.0 + SEEK_DEBOUNCE_S
        self.assertTrue(app._commit_pending_seek())
        app._cmd_seek(5.0, now=200.0)
        self.assertAlmostEqual(app._pending_seek_target, 20.0)
        self.assertAlmostEqual(app._pending_seek_origin, 15.0)
        self.assertEqual(app.feedback.pulse.label, "SEEK +5s")


if __name__ == "__main__":
    unittest.main()
