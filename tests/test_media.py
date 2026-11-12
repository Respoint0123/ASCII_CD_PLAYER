"""非 CD-DA 介质识别：不碰光驱，用 mock 覆盖分类、退出码和重试边界。"""
from __future__ import annotations

import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdda.device import (CDS_AUDIO, CDS_DATA_1, CDS_MIXED, CddaError,
                         MEDIA_AUDIO, MEDIA_DATA, MEDIA_MIXED, MEDIA_UNKNOWN,
                         MediaInfo, NoMediaError, UnreadableDiscError,
                         UnsupportedDiscError, _tray_has_disc,
                         classify_media, probe_media, query_disc)  # noqa: E402
from cdda.discmon import (DiscMonitor, TRAY_CLOSED_DISC, TRAY_CLOSED_EMPTY,
                          TRAY_OPEN)  # noqa: E402
from cdda.fake import build_fake_disc  # noqa: E402

AUDIO_TOC = """
CDROM sensed: HL-DT-ST DVDRAM GUD0N

Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    20973 [04:39.48]        0 [00:00.00]    no   no  2
  2.    17652 [03:55.27]    20973 [04:39.48]    no   no  2
TOTAL  38625 [08:34.75]         (audio only)
"""

MIXED_TOC = """
CDROM sensed: HL-DT-ST DVDRAM GUD0N

Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  2.      100 [00:01.25]      150 [00:02.00]    no   no  2
  3.      100 [00:01.25]      250 [00:03.25]    no   no  2
TOTAL   200 [00:02.50]         (audio only)
"""


class TestClassifyMedia(unittest.TestCase):
    def test_audio_toc_is_supported(self):
        info = classify_media(CDS_AUDIO, [(1, 0, 0x00), (2, 20973, 0x00)])
        self.assertEqual(info.kind, MEDIA_AUDIO)
        self.assertEqual((info.audio_tracks, info.data_tracks), (2, 0))
        self.assertEqual(info.audio_numbers, (1, 2))
        self.assertEqual(info.audio_starts, ((1, 0), (2, 20973)))
        self.assertTrue(info.supported)

    def test_pure_data_toc_is_rejected(self):
        info = classify_media(CDS_DATA_1, [(1, 0, 0x04)])
        self.assertEqual(info.kind, MEDIA_DATA)
        self.assertEqual(info.data_tracks, 1)
        self.assertTrue(info.is_data)
        self.assertFalse(info.supported)

    def test_mixed_mode_keeps_audio_only(self):
        info = classify_media(CDS_MIXED, [(1, 0, 0x00), (2, 20973, 0x04)])
        self.assertEqual(info.kind, MEDIA_MIXED)
        self.assertEqual((info.audio_tracks, info.data_tracks), (1, 1))
        self.assertEqual(info.audio_numbers, (1,))
        self.assertTrue(info.supported)

    def test_status_fallback_when_toc_unreadable(self):
        self.assertEqual(classify_media(CDS_DATA_1).kind, MEDIA_DATA)
        self.assertEqual(classify_media(CDS_MIXED).kind, MEDIA_MIXED)
        self.assertEqual(classify_media(CDS_AUDIO).kind, MEDIA_AUDIO)
        self.assertEqual(classify_media(None).kind, MEDIA_UNKNOWN)

    def test_data_status_wins_over_ambiguous_audio_toc(self):
        """状态明确是数据盘时，不能被 TOC 控制位洗成音频。"""
        info = classify_media(CDS_DATA_1, [(1, 0, 0x00)])
        self.assertEqual(info.kind, MEDIA_DATA)
        self.assertFalse(info.supported)


class TestProbeMedia(unittest.TestCase):
    def _probe(self, status, ctrl):
        def ioctl(fd, request, arg):
            if request == 0x5327:        # CDROM_DISC_STATUS
                return status
            if request == 0x5305:        # CDROMREADTOCHDR
                arg[:] = bytes([1, 1])
                return 0
            if request == 0x5306:        # CDROMREADTOCENTRY
                arg[1] = ctrl << 4
                arg[2] = 1               # CDROM_LBA
                struct.pack_into("<i", arg, 4, 0)
                return 0
            raise OSError("unexpected ioctl")

        with mock.patch("cdda.device.sys.platform", "linux"), \
             mock.patch("cdda.device.os.open", return_value=7), \
             mock.patch("cdda.device.os.close"), \
             mock.patch("cdda.device.fcntl.ioctl", side_effect=ioctl):
            return probe_media("/dev/sr0")

    def test_data_toc(self):
        info = self._probe(CDS_DATA_1, 0x04)
        self.assertEqual(info.kind, MEDIA_DATA)

    def test_audio_toc(self):
        info = self._probe(CDS_AUDIO, 0x00)
        self.assertEqual(info.kind, MEDIA_AUDIO)

    def test_drive_status_distinguishes_empty_and_present(self):
        for status, expected in ((4, True), (1, False), (2, False), (3, None)):
            with mock.patch("cdda.device.sys.platform", "linux"), \
                 mock.patch("cdda.device.os.open", return_value=7), \
                 mock.patch("cdda.device.os.close"), \
                 mock.patch("cdda.device.fcntl.ioctl", return_value=status):
                self.assertIs(_tray_has_disc("/dev/sr0"), expected, status)

    def test_ioctl_failure_is_unknown_not_unsupported(self):
        with mock.patch("cdda.device.sys.platform", "linux"), \
             mock.patch("cdda.device.os.open", return_value=7), \
             mock.patch("cdda.device.os.close"), \
             mock.patch("cdda.device.fcntl.ioctl", side_effect=OSError("EINVAL")):
            info = probe_media("/dev/sr0")
        self.assertEqual(info.kind, MEDIA_UNKNOWN)


class TestQueryDisc(unittest.TestCase):
    def _patched(self, proc):
        return (
            mock.patch("cdda.device.os.path.exists", return_value=True),
            mock.patch("cdda.device.probe_media",
                       return_value=MediaInfo(MEDIA_UNKNOWN)),
            mock.patch("cdda.device.subprocess.run", return_value=proc),
        )

    def test_data_media_rejected_before_cdparanoia(self):
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_DATA, 0, 1)), \
             mock.patch("cdda.device.subprocess.run") as run:
            with self.assertRaises(UnsupportedDiscError) as ctx:
                query_disc("/dev/sr0")
            run.assert_not_called()
        self.assertIn("只支持音乐 CD", str(ctx.exception))

    def test_nonzero_exit_with_track_like_output_never_makes_disc(self):
        proc = mock.Mock(returncode=1, stdout=AUDIO_TOC, stderr="")
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_AUDIO)), \
             mock.patch("cdda.device.subprocess.run", return_value=proc), \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertNotIsInstance(ctx.exception, UnsupportedDiscError)
        self.assertIn("退出码 1", str(ctx.exception))

    def test_audio_toc_rc_zero_is_parsed(self):
        proc = mock.Mock(returncode=0, stdout=AUDIO_TOC, stderr="")
        patches = self._patched(proc)
        with patches[0], patches[1], patches[2]:
            disc = query_disc("/dev/sr0")
        self.assertEqual([t.number for t in disc.tracks], [1, 2])
        self.assertEqual(disc.leadout_lba, 38625)

    def test_mixed_mode_only_exposes_audio_tracks(self):
        proc = mock.Mock(returncode=0, stdout=AUDIO_TOC, stderr="")
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_MIXED, 2, 1)), \
             mock.patch("cdda.device.subprocess.run", return_value=proc):
            disc = query_disc("/dev/sr0")
        self.assertEqual([t.number for t in disc.tracks], [1, 2])

    def test_mixed_toc_count_mismatch_is_rejected(self):
        """cdparanoia 若多列出数据轨，不能拿数量对不上的 TOC 当音轨。"""
        proc = mock.Mock(returncode=0, stdout=AUDIO_TOC, stderr="")
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_MIXED, 1, 1)), \
             mock.patch("cdda.device.subprocess.run", return_value=proc):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertIn("不一致", str(ctx.exception))

    def test_data_hint_without_tracks_is_unsupported(self):
        proc = mock.Mock(returncode=0, stdout="data track detected", stderr="")
        patches = self._patched(proc)
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(UnsupportedDiscError):
                query_disc("/dev/sr0")

    def test_audio_media_with_no_tracks_stays_retryable(self):
        proc = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_AUDIO)), \
             mock.patch("cdda.device.subprocess.run", return_value=proc), \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertNotIsInstance(ctx.exception, NoMediaError)
        self.assertNotIsInstance(ctx.exception, UnreadableDiscError)
        self.assertNotIsInstance(ctx.exception, UnsupportedDiscError)

    def test_no_media_hint_with_open_tray_is_no_media(self):
        proc = mock.Mock(returncode=1, stdout="",
                         stderr="Unable to open disc")
        patches = self._patched(proc)
        with patches[0], patches[1], patches[2], \
             mock.patch("cdda.device._tray_has_disc", return_value=False):
            with self.assertRaises(NoMediaError):
                query_disc("/dev/sr0")

    def test_closed_disc_unidentifiable_is_unreadable(self):
        """托盘明确有盘时，"Is there an audio CD?" 不能当成无盘。"""
        proc = mock.Mock(
            returncode=1, stdout="",
            stderr="Unable to open disc. Is there an audio CD in the drive?")
        patches = self._patched(proc)
        with patches[0], patches[1], patches[2], \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(UnreadableDiscError) as ctx:
                query_disc("/dev/sr0")
        self.assertNotIsInstance(ctx.exception, NoMediaError)
        self.assertNotIsInstance(ctx.exception, UnsupportedDiscError)
        self.assertIn("无法识别", str(ctx.exception))

    def test_known_audio_toc_failure_stays_retryable(self):
        """已确认 AUDIO/MIXED 的短暂 TOC 故障不能被 sticky 成 UNREADABLE。"""
        proc = mock.Mock(returncode=1, stdout="",
                         stderr="Unable to read table of contents")
        for media in (MediaInfo(MEDIA_AUDIO), MediaInfo(MEDIA_MIXED, 1, 1)):
            with self.subTest(kind=media.kind), \
                 mock.patch("cdda.device.os.path.exists", return_value=True), \
                 mock.patch("cdda.device.probe_media", return_value=media), \
                 mock.patch("cdda.device.subprocess.run", return_value=proc), \
                 mock.patch("cdda.device._tray_has_disc", return_value=True):
                with self.assertRaises(CddaError) as ctx:
                    query_disc("/dev/sr0")
            self.assertNotIsInstance(ctx.exception, NoMediaError)
            self.assertNotIsInstance(ctx.exception, UnreadableDiscError)
            self.assertNotIsInstance(ctx.exception, UnsupportedDiscError)

    def test_unknown_media_unknown_tray_is_unreadable(self):
        """连托盘状态都读不到时也不能无限 CHECKING 或冒充无盘。"""
        proc = mock.Mock(returncode=1, stdout="",
                         stderr="Unable to open disc")
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_UNKNOWN)), \
             mock.patch("cdda.device.subprocess.run", return_value=proc), \
             mock.patch("cdda.device._tray_has_disc", return_value=None):
            with self.assertRaises(UnreadableDiscError):
                query_disc("/dev/sr0")

    def test_unknown_timeout_with_closed_tray_is_unreadable(self):
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_UNKNOWN)), \
             mock.patch("cdda.device.subprocess.run",
                        side_effect=__import__("subprocess").TimeoutExpired(
                            "cdparanoia", 90)), \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(UnreadableDiscError):
                query_disc("/dev/sr0")

    def test_known_audio_timeout_stays_retryable(self):
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_AUDIO)), \
             mock.patch("cdda.device.subprocess.run",
                        side_effect=__import__("subprocess").TimeoutExpired(
                            "cdparanoia", 90)), \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertNotIsInstance(ctx.exception, NoMediaError)
        self.assertNotIsInstance(ctx.exception, UnreadableDiscError)

    def test_timeout_with_open_tray_is_no_media(self):
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_UNKNOWN)), \
             mock.patch("cdda.device.subprocess.run",
                        side_effect=__import__("subprocess").TimeoutExpired(
                            "cdparanoia", 90)), \
             mock.patch("cdda.device._tray_has_disc", return_value=False):
            with self.assertRaises(NoMediaError):
                query_disc("/dev/sr0")

    def test_closed_disc_nontoc_output_is_unreadable(self):
        proc = mock.Mock(returncode=1, stdout="", stderr="I/O error")
        patches = self._patched(proc)
        with patches[0], patches[1], patches[2], \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(UnreadableDiscError):
                query_disc("/dev/sr0")

    def test_known_audio_io_failure_stays_retryable(self):
        proc = mock.Mock(returncode=1, stdout="", stderr="I/O error")
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media",
                        return_value=MediaInfo(MEDIA_AUDIO)), \
             mock.patch("cdda.device.subprocess.run", return_value=proc), \
             mock.patch("cdda.device._tray_has_disc", return_value=True):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertNotIsInstance(ctx.exception, UnreadableDiscError)
        self.assertNotIsInstance(ctx.exception, UnsupportedDiscError)

    def test_track_numbers_must_match_toc(self):
        """数量相同但编号不同（数据轨 #1 被误列为音轨）也必须拒绝。"""
        proc = mock.Mock(returncode=0, stdout=AUDIO_TOC, stderr="")
        media = MediaInfo(MEDIA_MIXED, 2, 1,
                          audio_numbers=(2, 3),
                          audio_starts=((2, 20973), (3, 38625)))
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media", return_value=media), \
             mock.patch("cdda.device.subprocess.run", return_value=proc):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertIn("音轨编号", str(ctx.exception))

    def test_track_numbers_and_lbas_must_match_toc(self):
        proc = mock.Mock(returncode=0, stdout=AUDIO_TOC, stderr="")
        media = MediaInfo(MEDIA_AUDIO, 2, 0,
                          audio_numbers=(1, 2),
                          audio_starts=((1, 0), (2, 99999)))
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media", return_value=media), \
             mock.patch("cdda.device.subprocess.run", return_value=proc):
            with self.assertRaises(CddaError) as ctx:
                query_disc("/dev/sr0")
        self.assertIn("起始 LBA", str(ctx.exception))

    def test_mixed_toc_skips_leading_data_track(self):
        """数据轨 #1 不出现，音轨 #2/#3 的编号和 LBA 都匹配才接受。"""
        proc = mock.Mock(returncode=0, stdout=MIXED_TOC, stderr="")
        media = MediaInfo(MEDIA_MIXED, 2, 1,
                          audio_numbers=(2, 3),
                          audio_starts=((2, 150), (3, 250)))
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media", return_value=media), \
             mock.patch("cdda.device.subprocess.run", return_value=proc):
            disc = query_disc("/dev/sr0")
        self.assertEqual([t.number for t in disc.tracks], [2, 3])

    def test_matching_toc_track_numbers_and_lbas_are_accepted(self):
        proc = mock.Mock(returncode=0, stdout=AUDIO_TOC, stderr="")
        media = MediaInfo(MEDIA_AUDIO, 2, 0,
                          audio_numbers=(1, 2),
                          audio_starts=((1, 0), (2, 20973)))
        with mock.patch("cdda.device.os.path.exists", return_value=True), \
             mock.patch("cdda.device.probe_media", return_value=media), \
             mock.patch("cdda.device.subprocess.run", return_value=proc):
            disc = query_disc("/dev/sr0")
        self.assertEqual([t.number for t in disc.tracks], [1, 2])


class TestMonitorUnsupported(unittest.TestCase):
    def test_unsupported_sticks_until_tray_edge(self):
        state = {"tray": TRAY_CLOSED_DISC, "calls": 0}

        def read():
            state["calls"] += 1
            raise UnsupportedDiscError("data DVD")

        mon = DiscMonitor(lambda: state["tray"], read, interval=0.01)
        mon.prime(TRAY_CLOSED_EMPTY, None)
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, None))
        self.assertIsInstance(mon.error_snapshot(), UnsupportedDiscError)
        mon.poll_once()                  # 同一张数据盘：不重复读 TOC
        self.assertEqual(state["calls"], 1)

        state["tray"] = TRAY_OPEN
        mon.poll_once()
        self.assertIsNone(mon.error_snapshot())
        self.assertEqual(mon.snapshot(), (False, None))
        state["tray"] = TRAY_CLOSED_DISC
        mon.poll_once()                  # 换盘后重新判断
        self.assertEqual(state["calls"], 2)

    def test_unreadable_retries_during_grace_then_sticks(self):
        state = {"calls": 0}

        def read():
            state["calls"] += 1
            raise UnreadableDiscError("cannot identify")

        mon = DiscMonitor(lambda: TRAY_CLOSED_DISC, read, interval=0.01)
        mon.prime(TRAY_CLOSED_EMPTY, None)
        with mock.patch("cdda.discmon.DISC_UNREADABLE_S", 10.0):
            mon.poll_once()
            self.assertIsNone(mon.error_snapshot())
            mon.poll_once()
            self.assertIsNone(mon.error_snapshot())
        self.assertEqual(state["calls"], 2)
        with mock.patch("cdda.discmon.DISC_UNREADABLE_S", 0.0):
            mon.poll_once()
        self.assertIsInstance(mon.error_snapshot(), UnreadableDiscError)
        with mock.patch("cdda.discmon.DISC_UNREADABLE_S", 0.0):
            mon.poll_once()                  # sticky：不再重读
        self.assertEqual(state["calls"], 3)

    def test_unreadable_clears_on_tray_edge(self):
        state = {"tray": TRAY_CLOSED_DISC, "calls": 0}

        def read():
            state["calls"] += 1
            raise UnreadableDiscError("cannot identify")

        mon = DiscMonitor(lambda: state["tray"], read, interval=0.01)
        mon.prime(TRAY_CLOSED_EMPTY, None)
        with mock.patch("cdda.discmon.DISC_UNREADABLE_S", 0.0):
            mon.poll_once()
        self.assertIsInstance(mon.error_snapshot(), UnreadableDiscError)
        state["tray"] = TRAY_OPEN
        mon.poll_once()
        self.assertIsNone(mon.error_snapshot())

    def test_transient_error_is_not_sticky(self):
        state = {"calls": 0}
        disc = build_fake_disc(durations=(1.0,))

        def read():
            state["calls"] += 1
            if state["calls"] == 1:
                raise CddaError("not ready")
            return disc

        mon = DiscMonitor(lambda: TRAY_CLOSED_DISC, read, interval=0.01)
        mon.prime(TRAY_CLOSED_EMPTY, None)
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, None))
        self.assertIsNone(mon.error_snapshot())
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, disc))


if __name__ == "__main__":
    unittest.main()
