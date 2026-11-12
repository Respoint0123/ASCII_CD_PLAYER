"""DiscSession：盘片资源、抓轨启动、元数据代数与 retire 行为。"""
from __future__ import annotations

import unittest

from cdda.fake import build_fake_disc
from cdda.session import DiscSession
from tests.support import AppRipper, CacheStub, fake_args


class _StuckRipper(AppRipper):
    def stop_and_join(self, timeout: float = 10.0,
                      preserve_part: bool = False) -> bool:
        self.stopped += 1
        self.preserve_part = preserve_part
        self.events.append(self.name + ":stop_join")
        return False


class TestDiscSession(unittest.TestCase):
    def _session(self, ripper=None):
        disc = build_fake_disc(durations=(1.0, 2.0))
        session = DiscSession(fake_args())
        session.prepare(disc, CacheStub(), ripper or AppRipper())
        return session, disc

    def test_prepare_resets_generation_and_started(self):
        session, _ = self._session()
        first = session.seq
        self.assertEqual(session.metadata_seq, -1)
        self.assertFalse(session.started)

        session.prepare(build_fake_disc(durations=(2.0,)), CacheStub(), AppRipper())
        self.assertEqual(session.seq, first + 1)
        self.assertEqual(session.metadata_seq, -1)
        self.assertFalse(session.started)

    def test_ensure_ripper_starts_once_and_wants_track(self):
        session, _ = self._session()
        ripper = session.ripper

        self.assertTrue(session.ensure_ripper(2))
        self.assertTrue(session.ensure_ripper(2))

        self.assertEqual(ripper.started, 1)
        self.assertEqual(ripper.wanted, [2])
        self.assertTrue(session.started)

    def test_select_track_only_wants_after_start(self):
        session, _ = self._session()
        ripper = session.ripper

        session.select_track(2)
        self.assertEqual(ripper.wanted, [])

        session.ensure_ripper(1)
        session.select_track(2)
        self.assertEqual(ripper.wanted, [1])
        self.assertEqual(ripper.requests, [(2, False)])
        session.select_track(3, immediate=True)
        self.assertEqual(ripper.requests, [(2, False), (3, True)])

    def test_claim_metadata_once_per_generation(self):
        session, _ = self._session()
        self.assertTrue(session.claim_metadata())
        self.assertFalse(session.claim_metadata())

        session.prepare(build_fake_disc(durations=(2.0,)), CacheStub(), AppRipper())
        self.assertTrue(session.claim_metadata())

    def test_retire_unstarted_is_safe(self):
        session, _ = self._session()
        self.assertTrue(session.retire())
        self.assertFalse(session.started)

    def test_retire_started_clears_started(self):
        session, _ = self._session()
        session.ensure_ripper(1)
        self.assertTrue(session.retire())
        self.assertFalse(session.started)

    def test_retire_timeout_keeps_started(self):
        session, _ = self._session(ripper=_StuckRipper())
        session.ensure_ripper(1)
        self.assertFalse(session.retire(timeout=0.0))
        self.assertTrue(session.started, "旧写者没退场前不能启动新抓轨")


if __name__ == "__main__":
    unittest.main()
