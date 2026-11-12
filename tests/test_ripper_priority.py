"""Ripper 优先级防抖：稳定窗口、重复请求、取消与调度安全点。"""
from __future__ import annotations

import tempfile
import time
import unittest

from cdda.cache import Cache
from cdda.fake import build_fake_disc
from cdda.ripper import FakeRipper, RipperBase
from config import RIP_PRIO_DEBOUNCE_S


class _Picker(RipperBase):
    def __init__(self, *args, clock=None, **kwargs):
        super().__init__(*args, clock=clock, **kwargs)

    def _rip_one(self, number: int) -> None:        # pragma: no cover - 不跑线程
        pass


class TestRipperPriorityDebounce(unittest.TestCase):
    def _ripper(self, mode: str = "track"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = [10.0]
        disc = build_fake_disc(durations=(30.0,) * 10)
        ripper = _Picker(disc, Cache(tmp.name), mode=mode, clock=lambda: clock[0])
        ripper.want(1)
        return ripper, clock

    def test_request_waits_for_stable_window_then_commits(self):
        ripper, clock = self._ripper()
        ripper.request_priority(7)
        self.assertEqual(ripper._prio, 1)
        self.assertEqual(ripper._pending_prio, 7)

        clock[0] += RIP_PRIO_DEBOUNCE_S - 0.001
        self.assertIsNone(ripper._pick(), "未到期不能启动新目标")
        self.assertEqual((ripper._prio, ripper._pending_prio), (1, 7))
        self.assertEqual(ripper._effective_priority(), 1)

        clock[0] += 0.002
        self.assertEqual(ripper._pick(), 7)
        self.assertEqual(ripper._prio, 7)
        self.assertIsNone(ripper._pending_prio)

    def test_repeated_same_request_does_not_extend_deadline(self):
        ripper, clock = self._ripper()
        ripper.request_priority(7)
        clock[0] += 0.25
        ripper.request_priority(7)                 # 模拟 App._tick 重复 want
        self.assertEqual(ripper._pending_prio, 7)
        clock[0] += 0.06
        self.assertEqual(ripper._pick(), 7,
                         "相同目标重复请求不能把 300ms 窗口无限推迟")

    def test_rapid_sequence_only_commits_final_target(self):
        ripper, clock = self._ripper()
        ripper.want(2)
        for target, delay in ((7, 0.0), (3, 0.05), (9, 0.05)):
            clock[0] += delay
            ripper.request_priority(target)
        self.assertEqual(ripper._prio, 2)
        self.assertEqual(ripper._pending_prio, 9)
        clock[0] += 0.25
        self.assertIsNone(ripper._pick())
        self.assertEqual(ripper._prio, 2, "窗口内 effective 不能跳到中间目标")
        clock[0] += 0.06
        self.assertEqual(ripper._pick(), 9)
        self.assertEqual(ripper._prio, 9)

    def test_choosing_effective_again_cancels_pending(self):
        ripper, _clock = self._ripper()
        ripper.want(2)
        ripper.request_priority(7)
        ripper.request_priority(2)
        self.assertIsNone(ripper._pending_prio)
        self.assertEqual(ripper._pick(), 2)

    def test_pending_blocks_new_prefetch_until_commit(self):
        ripper, clock = self._ripper(mode="next")
        done = {1, 2}
        ripper.is_done = lambda n: n in done       # type: ignore[method-assign]
        ripper.request_priority(9)
        self.assertIsNone(ripper._pick(),
                          "等待期不能为旧窗口新开预读轨，避免额外寻道")
        clock[0] += RIP_PRIO_DEBOUNCE_S + 0.001
        self.assertEqual(ripper._pick(), 9)

    def test_immediate_request_skips_window(self):
        ripper, _clock = self._ripper()
        ripper.request_priority(7, immediate=True)
        self.assertEqual((ripper._prio, ripper._pending_prio), (7, None))
        self.assertEqual(ripper._pick(), 7)

    def test_cancel_prevents_late_commit(self):
        ripper, clock = self._ripper()
        ripper.want(2)
        ripper.request_priority(7)
        ripper.stop()
        clock[0] += RIP_PRIO_DEBOUNCE_S + 1.0
        self.assertIsNone(ripper._pending_prio)
        self.assertEqual(ripper._effective_priority(), 2)
        self.assertFalse(ripper._commit_pending())

    def test_active_rip_keeps_part_until_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0, 1.0))
            cache = Cache(tmp)
            ripper = FakeRipper(disc, cache, speed=0.2)
            ripper.want(1)
            ripper.start()
            part = cache.part_path(disc, 1)
            try:
                end = time.monotonic() + 3.0
                while time.monotonic() < end and not part.exists():
                    time.sleep(0.01)
                self.assertTrue(part.exists())
                before = part.stat().st_size
                ripper.request_priority(2)
                time.sleep(0.1)
                self.assertTrue(part.exists(), "300ms 窗口内不能删正在写的 .part")
                self.assertGreaterEqual(part.stat().st_size, before)
                self.assertEqual(ripper._prio, 1,
                                 "未到期不能提交中间目标去抢占当前抓轨")
                end = time.monotonic() + 2.0
                while time.monotonic() < end and ripper._prio != 2:
                    time.sleep(0.01)
                self.assertEqual(ripper._prio, 2,
                                 "稳定窗口到期后应提交目标并允许抢占")
            finally:
                self.assertTrue(ripper.stop_and_join(timeout=5))

    def test_pending_wait_wakes_at_deadline_without_busy_loop(self):
        ripper, clock = self._ripper()
        ripper.request_priority(7)
        self.assertAlmostEqual(ripper._pending_wait(1.0),
                               RIP_PRIO_DEBOUNCE_S)
        clock[0] += 0.1
        self.assertAlmostEqual(ripper._pending_wait(1.0), 0.2)
        clock[0] += 0.25
        self.assertEqual(ripper._pending_wait(1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
