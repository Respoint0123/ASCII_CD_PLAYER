"""视觉反馈协调器：可中断、可重定向、纯绝对时间采样。"""
from __future__ import annotations

import unittest

from config import (SEEK_HOLD_GAP_S, SEEK_HOLD_TAIL_S,
                    TRACK_HINT_S, TRACK_TRANSITION_S)
from ui.feedback import TrackSnapshot, VisualFeedback, transition_view


def _snap(number: int, position: float = 0.0, duration: float = 180.0):
    return TrackSnapshot(
        number=number, title=f"Track {number:02d}", artist="Artist",
        position=position, duration=duration,
    )


class TestVisualFeedback(unittest.TestCase):
    def _feedback(self, now: float = 10.0):
        clock = [now]
        return VisualFeedback(clock=lambda: clock[0]), clock

    def test_begin_records_source_and_keeps_group_origin(self):
        fb, clock = self._feedback()
        first = fb.begin_track(_snap(2, 40.0, 120.0), 7, "next", now=10.0)
        self.assertEqual(
            (first.mode, first.origin_no, first.old_track_no,
             first.old_position, first.old_duration),
            ("next", 2, 2, 40.0, 120.0),
        )
        self.assertEqual(first.elapsed, 0.0)

        clock[0] = 10.1
        visible = fb.visible_source(_snap(7), clock[0])
        second = fb.begin_track(
            visible.track, 3, "next", now=clock[0],
            source_title_fade=visible.title_fade,
            source_bar_fade=visible.bar_fade)
        self.assertEqual((second.mode, second.origin_no, second.old_track_no),
                         ("next", 2, 2),
                         "屏幕还停在 02 时重定向，退场源仍是当前可见的 02")
        self.assertAlmostEqual(second.old_position, 66.666, places=2)
        self.assertAlmostEqual(second.source_title_fade, 1 / 3, places=3)
        clock[0] = 10.2
        visible = fb.visible_source(_snap(3), clock[0])
        third = fb.begin_track(
            visible.track, 9, "next", now=clock[0],
            source_title_fade=visible.title_fade,
            source_bar_fade=visible.bar_fade)
        self.assertEqual((third.origin_no, third.old_track_no), (2, 2))
        self.assertEqual(fb.transition.old_track_no, 2)

    def test_direction_rebase_keeps_badge_and_bar_consistent(self):
        fb, clock = self._feedback()
        fb.begin_track(_snap(2, 0.0, 120.0), 7, "next", now=10.0)
        clock[0] = 10.0 + TRACK_TRANSITION_S * 0.58
        visible = fb.visible_source(_snap(7, 0.0, 180.0), clock[0])
        self.assertEqual(visible.track.number, 7)
        self.assertGreater(visible.title_fade, 0.0)
        tt = fb.begin_track(
            visible.track, 3, "prev", now=clock[0],
            source_title_fade=visible.title_fade,
            source_bar_fade=visible.bar_fade)
        self.assertEqual((tt.mode, tt.origin_no, tt.old_track_no),
                         ("prev", 7, 7),
                         "方向反转且中间轨已可见时，从 07 重新起算")

    def test_retarget_starts_from_current_visible_position_and_fade(self):
        fb, clock = self._feedback()
        tt = fb.begin_track(_snap(2, 60.0, 120.0), 7, "next", now=10.0)
        clock[0] = 10.0 + TRACK_TRANSITION_S * 0.25
        before = transition_view(tt, _snap(7, 0.0, 180.0),
                                 clock[0] - tt.since)
        self.assertEqual(before.track.number, 2)
        self.assertAlmostEqual(before.track.position, 90.0)
        self.assertAlmostEqual(before.title_fade, 0.5)
        self.assertAlmostEqual(before.bar_fade, 0.0)

        new = fb.begin_track(
            before.track, 3, "next", now=clock[0],
            source_title_fade=before.title_fade,
            source_bar_fade=before.bar_fade)
        after = transition_view(new, _snap(3, 0.0, 180.0), 0.0)
        self.assertEqual(after.track.number, 2)
        self.assertAlmostEqual(after.track.position, 90.0,
                               msg="重定向后首帧不能把进度条从 75% 拉回 0%")
        self.assertAlmostEqual(after.title_fade, 0.5)
        later = transition_view(new, _snap(3, 0.0, 180.0),
                                TRACK_TRANSITION_S * 0.25)
        self.assertAlmostEqual(later.track.position, 105.0)
        self.assertAlmostEqual(later.title_fade, 0.75)

    def test_retarget_after_midpoint_preserves_visible_target_fade(self):
        fb, _ = self._feedback()
        tt = fb.begin_track(_snap(2, 0.0, 120.0), 7, "next", now=10.0)
        at = 10.0 + TRACK_TRANSITION_S * 0.58
        before = transition_view(tt, _snap(7, 0.0, 180.0), at - tt.since)
        self.assertEqual(before.track.number, 7)
        self.assertAlmostEqual(before.track.position, 0.0)
        self.assertGreater(before.title_fade, 0.0)

        new = fb.begin_track(
            before.track, 3, "prev", now=at,
            source_title_fade=before.title_fade,
            source_bar_fade=before.bar_fade)
        after = transition_view(new, _snap(3, 0.0, 180.0), 0.0)
        self.assertEqual(after.track.number, 7)
        self.assertAlmostEqual(after.title_fade, before.title_fade,
                               msg="标题透明度不能在重定向帧跳变")

    def test_origin_resets_after_hint_expires_or_round_trip(self):
        fb, clock = self._feedback()
        fb.begin_track(_snap(2), 7, "next", now=10.0)
        clock[0] = 10.0 + TRACK_HINT_S
        fb.sample(clock[0])
        self.assertIsNone(fb.transition)

        fb.begin_track(_snap(9), 4, "prev", now=clock[0])
        self.assertEqual(fb.transition.origin_no, 9)

        fb2, _ = self._feedback()
        fb2.begin_track(_snap(2), 7, "next", now=10.0)
        fb2.retarget_track(_snap(7), 2, "prev", now=10.1)
        self.assertEqual(fb2.transition.origin_no, 7,
                         "绕回起点不能生成 02→02 的失效徽标")

    def test_transition_elapsed_is_absolute_and_expires(self):
        fb, _ = self._feedback()
        start = 20.0
        tt = fb.begin_track(_snap(1), 2, "auto", now=start)
        fb.sample(start + 0.25)
        self.assertIsNotNone(fb.transition)
        self.assertAlmostEqual(tt.elapsed, 0.25)
        self.assertLess(TRACK_TRANSITION_S, TRACK_HINT_S)
        fb.sample(start + TRACK_HINT_S - 0.001)
        self.assertIsNotNone(fb.transition)
        fb.sample(start + TRACK_HINT_S)
        self.assertIsNone(fb.transition)

    def test_pulse_cancels_track_and_latest_pulse_wins(self):
        fb, _ = self._feedback()
        fb.begin_track(_snap(1), 2, "next", now=10.0)
        seek = fb.show_pulse("seek", "SEEK +5s", duration=0.22,
                        direction=1, now=10.1)
        self.assertIsNone(fb.transition)
        self.assertEqual((seek.kind, seek.label, seek.direction),
                         ("seek", "SEEK +5s", 1))

        pause = fb.show_pulse("paused", "PAUSED", duration=0.13, now=10.15)
        self.assertEqual(pause.label, "PAUSED")
        self.assertIs(fb.pulse, pause)

    def test_pulse_expires_without_wall_clock_sleep(self):
        fb, _ = self._feedback()
        pulse = fb.show_pulse("seek", "SEEK -5s", duration=0.22,
                         direction=-1, now=30.0)
        fb.sample(30.10)
        self.assertIs(fb.pulse, pulse)
        self.assertAlmostEqual(pulse.elapsed, 0.10)
        fb.sample(30.22)
        self.assertIsNone(fb.pulse)

    def test_track_change_clears_old_pulse_and_reset_clears_all(self):
        fb, _ = self._feedback()
        fb.show_pulse("seek", "SEEK +5s", duration=0.22, now=10.0)
        fb.begin_track(_snap(1), 2, "next", now=10.1)
        self.assertIsNone(fb.pulse)

        fb.show_pulse("paused", "PAUSED", duration=0.13, now=10.2)
        fb.reset()
        self.assertIsNone(fb.transition)
        self.assertIsNone(fb.pulse)

    def test_seek_pulse_hold_accumulates_and_resets_after_gap(self):
        fb, _ = self._feedback()
        first = fb.seek_pulse("SEEK +5s", 1, now=10.0)
        self.assertEqual((first.hold, first.direction), (0.0, 1))

        second = fb.seek_pulse("SEEK +10s", 1, now=10.03)
        self.assertIs(second, first, "连续键复用同一个提示，不能重置动画")
        self.assertGreater(second.hold, 0.0)
        self.assertEqual(second.label, "SEEK +10s")

        held = second.hold
        third = fb.seek_pulse("SEEK +15s", 1, now=10.06)
        self.assertGreater(third.hold, held)

        after_gap = fb.seek_pulse(
            "SEEK +5s", 1, now=10.06 + SEEK_HOLD_GAP_S + 0.01)
        self.assertIs(after_gap, first)
        self.assertEqual(after_gap.hold, 0.0, "超时后按新短按处理")
        self.assertAlmostEqual(after_gap.since, 10.06 + SEEK_HOLD_GAP_S + 0.01)

    def test_held_seek_pulse_lives_until_release_tail(self):
        fb, _ = self._feedback()
        fb.seek_pulse("SEEK +5s", 1, now=10.0)
        fb.seek_pulse("SEEK +10s", 1, now=10.03)
        fb.sample(10.03 + SEEK_HOLD_GAP_S + SEEK_HOLD_TAIL_S - 0.001)
        self.assertIsNotNone(fb.pulse)
        fb.sample(10.03 + SEEK_HOLD_GAP_S + SEEK_HOLD_TAIL_S + 0.001)
        self.assertIsNone(fb.pulse)

    def test_round_trip_can_settle_when_visible_source_is_target(self):
        fb, clock = self._feedback()
        fb.begin_track(_snap(2, 60.0, 120.0), 7, "next", now=10.0)
        clock[0] = 10.0 + TRACK_TRANSITION_S * 0.25
        visible = fb.visible_source(_snap(7, 0.0, 180.0), clock[0])
        self.assertEqual(visible.track.number, 2)
        tt = fb.begin_track(
            visible.track, 2, "prev", now=clock[0],
            source_title_fade=visible.title_fade,
            source_bar_fade=visible.bar_fade,
            business_from_no=7)
        self.assertIsNotNone(tt, "07→02 时屏幕恰好还在 02，也要做回落")
        self.assertEqual((tt.mode, tt.origin_no, tt.old_track_no),
                         ("prev", 7, 2))
        after = transition_view(tt, _snap(2, 0.0, 120.0), 0.0)
        self.assertAlmostEqual(after.track.position, visible.track.position)
        later = transition_view(tt, _snap(2, 0.0, 120.0),
                                TRACK_TRANSITION_S * 0.25)
        self.assertLess(later.track.position, after.track.position)

    def test_real_same_track_selection_still_does_not_start(self):
        fb, _ = self._feedback()
        self.assertIsNone(fb.begin_track(
            _snap(2), 2, "next", now=10.0, business_from_no=2))

    def test_same_track_or_unknown_mode_does_not_start(self):
        fb, _ = self._feedback()
        self.assertIsNone(fb.begin_track(_snap(2), 2, "next", now=10.0))
        self.assertIsNone(fb.begin_track(_snap(2), 3, "bogus", now=10.0))
        self.assertIsNone(fb.transition)


if __name__ == "__main__":
    unittest.main()
