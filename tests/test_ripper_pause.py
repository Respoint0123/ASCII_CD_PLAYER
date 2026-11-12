"""Stop 暂停缓存：杀抓轨子进程、线程挂起，Space 恢复后继续。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

from cdda.cache import Cache
from cdda.fake import build_fake_disc
from cdda.ripper import FakeRipper, Ripper, RipperBase


def _wait_for(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


class _ScriptRipper(Ripper):
    """用可控的假 cdparanoia 脚本验证 pause 真的杀掉子进程。"""

    def __init__(self, disc, cache, script: str):
        super().__init__(disc, cache, "/dev/null")
        self.script = script
        self.retry_delay = 0.0

    def _cmd(self, number: int) -> list[str]:
        return [sys.executable, self.script]


class _RaceRipper(RipperBase):
    """第一次抓轨模拟“被 pause 杀掉后抛 rc=-9”，用于验证 run() 的代数锁存。"""

    def __init__(self, disc, cache):
        super().__init__(disc, cache, name="race-ripper")
        self.entered = threading.Event()
        self.release = threading.Event()
        self.retried = threading.Event()
        self.calls = 0
        self.retry_delay = 0.0

    def _rip_one(self, number: int) -> None:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait(2.0)
            raise RuntimeError("simulated cdparanoia rc=-9")
        self.retried.set()
        while not self._stop_event.is_set():
            time.sleep(0.01)


class TestRipperPause(unittest.TestCase):
    def test_pause_kills_child_and_keeps_thread(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pid_file = os.path.join(tmp.name, "child.pid")
        script = os.path.join(tmp.name, "slow_cdparanoia.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "import os, sys, time\n"
                f"with open({pid_file!r}, 'w') as fh:\n"
                "    fh.write(str(os.getpid()))\n"
                "sys.stdout.buffer.write(b'\\x00' * 4000)\n"
                "sys.stdout.buffer.flush()\n"
                "time.sleep(30)\n"
            )
        disc = build_fake_disc(durations=(30.0,))
        cache = Cache(os.path.join(tmp.name, "cache"))
        ripper = _ScriptRipper(disc, cache, script)
        self.addCleanup(lambda: (ripper.stop(), ripper.join(timeout=5)))
        ripper.start()

        self.assertTrue(_wait_for(lambda: ripper._proc is not None))
        child = ripper._proc
        self.assertIsNotNone(child)
        self.assertTrue(_wait_for(lambda: os.path.exists(pid_file)))

        ripper.pause()
        self.assertTrue(_wait_for(lambda: ripper._proc is None))
        self.assertTrue(_wait_for(lambda: ripper.is_paused()))
        self.assertTrue(ripper.is_alive(), "暂停要保留线程，Space 才能恢复")
        self.assertIsNotNone(child.poll(), "cdparanoia 子进程必须被收掉")
        self.assertEqual(ripper.attempts_of(1), 0, "暂停不能算一次坏盘失败")

        self.assertTrue(ripper.stop_and_join(timeout=5))

    def test_quick_pause_resume_is_not_a_failure(self):
        """pause 后立刻 resume：旧 attempt 的 rc=-9 不能消耗 RIP_ATTEMPTS。"""
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(30.0,))
            ripper = _RaceRipper(disc, Cache(tmp))
            self.addCleanup(lambda: (ripper.stop(), ripper.join(timeout=5)))
            ripper.start()
            self.assertTrue(ripper.entered.wait(2.0))
            generation = ripper._pause_generation_snapshot()

            ripper.pause()
            ripper.resume()
            self.assertEqual(ripper._pause_generation_snapshot(), generation + 1,
                             "resume 不能清掉 pause 的取消代数")
            ripper.release.set()

            self.assertTrue(ripper.retried.wait(2.0),
                            "被取消的旧 attempt 之后应重新抓同一轨")
            self.assertEqual(ripper.attempts_of(1), 0)
            self.assertFalse(ripper.is_failed(1))
            self.assertEqual(ripper.status["errors"]["e"], 0)

    def test_quick_pause_resume_restarts_real_child(self):
        """真实 Ripper：pause/resume 紧挨着，旧子进程收尾后能重新起新 cdparanoia。"""
        with tempfile.TemporaryDirectory() as tmp:
            count_file = os.path.join(tmp, "starts.txt")
            script = os.path.join(tmp, "slow_cdparanoia.py")
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(
                    "import os, sys, time\n"
                    f"try:\n"
                    f"    n = int(open({count_file!r}).read())\n"
                    "except Exception:\n"
                    "    n = 0\n"
                    f"with open({count_file!r}, 'w') as fh:\n"
                    "    fh.write(str(n + 1))\n"
                    "sys.stdout.buffer.write(b'\\x00' * 4000)\n"
                    "sys.stdout.buffer.flush()\n"
                    "time.sleep(30)\n"
                )
            disc = build_fake_disc(durations=(30.0,))
            cache = Cache(os.path.join(tmp, "cache"))
            ripper = _ScriptRipper(disc, cache, script)
            self.addCleanup(lambda: (ripper.stop(), ripper.join(timeout=5)))
            ripper.start()

            def starts() -> int:
                try:
                    with open(count_file, encoding="utf-8") as fh:
                        return int(fh.read())
                except (OSError, ValueError):
                    return 0

            self.assertTrue(_wait_for(lambda: starts() >= 1))
            ripper.pause()
            ripper.resume()
            self.assertTrue(_wait_for(lambda: starts() >= 2),
                            "旧 attempt 收尾后必须重新启动抓轨")
            self.assertEqual(ripper.attempts_of(1), 0,
                             "快速 s→Space 不能把 kill 记成坏盘")
            self.assertTrue(ripper.stop_and_join(timeout=5))

    def test_fake_ripper_pause_resume_does_not_count_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(2.0,))
            cache = Cache(tmp)
            ripper = FakeRipper(disc, cache, speed=0.2)
            self.addCleanup(lambda: (ripper.stop(), ripper.join(timeout=5)))
            part = cache.part_path(disc, 1)
            ripper.start()
            self.assertTrue(_wait_for(lambda: part.exists()))

            ripper.pause()
            self.assertTrue(_wait_for(lambda: ripper.is_paused()))
            self.assertTrue(ripper.is_alive())
            self.assertTrue(_wait_for(lambda: not part.exists()),
                            "暂停时半成品要被丢掉，恢复后重抓")

            ripper.resume()
            self.assertTrue(_wait_for(lambda: not ripper.is_paused()))
            self.assertTrue(_wait_for(lambda: part.exists()))
            self.assertEqual(ripper.attempts_of(1), 0,
                             "pause 不是抓轨失败，不能消耗重试次数")

            self.assertTrue(ripper.stop_and_join(timeout=5))
            self.assertFalse(ripper.is_paused())


if __name__ == "__main__":
    unittest.main()
