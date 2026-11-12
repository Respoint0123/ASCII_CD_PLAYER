"""TrayState：托盘边沿与装载阶段的单一更新入口。"""
from __future__ import annotations

import time
import unittest

from app_state import TrayPhase, TrayState


class TestTrayState(unittest.TestCase):
    def test_set_tray_reports_edges_only(self):
        tray = TrayState()
        self.assertTrue(tray.set_tray("open", 12.0))
        self.assertEqual((tray.tray, tray.since), ("open", 12.0))
        self.assertFalse(tray.set_tray("open", 99.0))
        self.assertEqual(tray.since, 12.0)

    def test_enter_updates_related_fields_together(self):
        tray = TrayState()
        tray.enter(TrayPhase.READING, present=True, state="ready",
                   ready_at=5.0, transition=None)
        self.assertEqual(tray.ui, "reading")
        self.assertTrue(tray.disc_present)
        self.assertEqual(tray.disc_state, "ready")
        self.assertEqual(tray.ready_at, 5.0)
        self.assertIsNone(tray.transition)

    def test_enter_preserves_last_disc_unless_explicit(self):
        old = object()
        tray = TrayState(last_disc=old)
        tray.enter(TrayPhase.CHECKING, present=False, state="checking")
        self.assertIs(tray.last_disc, old)

        tray.enter(TrayPhase.OPEN, present=False, state="tray_open",
                   last_disc=None)
        self.assertIsNone(tray.last_disc)

    def test_enter_main_clears_transition(self):
        tray = TrayState(phase=TrayPhase.RETURN, transition={"p": 0.0},
                         ready_at=time.monotonic() + 1)
        tray.enter_main()
        self.assertEqual(tray.ui, "")
        self.assertIsNone(tray.transition)
        self.assertEqual(tray.ready_at, 0.0)


if __name__ == "__main__":
    unittest.main()
