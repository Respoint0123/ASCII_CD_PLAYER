"""核心单元测试：不碰光驱、不碰终端，纯逻辑。"""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio.engine import Engine                      # noqa: E402
from audio.media import MediaFailed, MediaStalled, TrackMedia  # noqa: E402
from cdda.cache import Cache                      # noqa: E402
from cdda.device import CddaError, Disc, Track, parse_toc  # noqa: E402
from cdda.fake import MusicSynth, build_fake_disc  # noqa: E402
from cdda.discmon import (CDS_DISC_OK, CDS_DRIVE_NOT_READY, CDS_NO_DISC,
                          CDS_NO_INFO, CDS_TRAY_OPEN, DiscMonitor,
                          TRAY_CLOSED_DISC, TRAY_CLOSED_EMPTY, TRAY_OPEN,
                          drive_has_disc, drive_tray_state)  # noqa: E402
from cdda.ripper import FakeRipper, Ripper, RipperBase  # noqa: E402
from config import (META_VERSION, RIP_ATTEMPTS,  # noqa: E402
                    RIP_MODE, SEEK_ARROW_3_S, SEEK_HOLD_GAP_S,
                    SEEK_HOLD_MAX_S, SEEK_HOLD_RELEASE_S, THEMES)
from services.deps import check_dependencies, format_report, required_ok  # noqa: E402
from dsp.analyzer import Analyzer, VisFrame        # noqa: E402
from ui.glyphs import (RESET, bg, braille_scope, fg, fit, strip_ansi,  # noqa: E402
                       text_width, time_fmt, spectrum_line, waterfall_line)
from ui.feedback import (TrackSnapshot, VisualPulse,  # noqa: E402
                         transition_view)
from ui.layouts import TrackTransition, UIState, render  # noqa: E402

SAMPLE_TOC = """
using cdda library version: 10.2
Checking /dev/sr0 for cdrom...
CDROM sensed: HL-DT-ST DVDRAM GUD0N

Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    20973 [04:39.48]        0 [00:00.00]    no   no  2
  2.    17652 [03:55.27]    20973 [04:39.48]    no   no  2
  3.    24120 [05:21.45]    38625 [08:35.00]    no   no  2
TOTAL  62745 [13:56.45]         (audio only)
"""


class TestToc(unittest.TestCase):
    def test_parse(self):
        tracks, total = parse_toc(SAMPLE_TOC)
        self.assertEqual([t.number for t in tracks], [1, 2, 3])
        self.assertEqual([t.start_lba for t in tracks], [0, 20973, 38625])
        self.assertEqual(total, 62745)
        self.assertAlmostEqual(tracks[0].seconds, 20973 / 75, places=3)
        self.assertEqual(tracks[0].pcm_bytes, 20973 * 2352)

    def test_discid_isolates_discs(self):
        tracks, total = parse_toc(SAMPLE_TOC)
        a = Disc(device="/dev/sr0", tracks=tracks, leadout_lba=total)
        b = Disc(device="/dev/sr0", tracks=[Track(1, 0, 100)], leadout_lba=100)
        self.assertNotEqual(a.discid, b.discid)
        self.assertEqual(a.discid, Disc("/dev/sr1", tracks, total).discid)

    def test_next_prev(self):
        tracks, total = parse_toc(SAMPLE_TOC)
        d = Disc(device="x", tracks=tracks, leadout_lba=total)
        self.assertEqual(d.next_number(1), 2)
        self.assertIsNone(d.next_number(3))
        self.assertIsNone(d.prev_number(1))


class TestAnalyzer(unittest.TestCase):
    def _pcm(self, freqs, seconds=1.0, amp=0.5, sr=44100):
        t = np.arange(int(sr * seconds), dtype=np.float32) / sr
        sig = np.zeros_like(t)
        for f in freqs:
            sig += amp * np.sin(2 * np.pi * f * t) / len(freqs)
        stereo = np.stack([sig, sig], axis=1)
        return (stereo * 32767).astype("<i2").tobytes()

    def test_tone_shows_up(self):
        a = Analyzer()
        a.push(self._pcm([440.0]))
        fr = a.frame()
        self.assertGreater(fr.spectrum.max(), 0.3)
        self.assertGreater(fr.rms_db[0], -20)
        peak_bin = int(np.argmax(fr.spectrum))
        self.assertTrue(26 <= peak_bin <= 31, f"440Hz 落在 bin {peak_bin}")

    def test_silence_is_quiet(self):
        a = Analyzer()
        a.push(b"\x00\x00\x00\x00" * 44100)
        fr = a.frame()
        self.assertLess(fr.rms_db[0], -60)
        self.assertLess(fr.spectrum.max(), 0.2)

    def test_empty_frames_safe(self):
        a = Analyzer()
        fr = a.frame()
        self.assertEqual(len(fr.spectrum), 64)
        self.assertTrue(fr.silent)
        self.assertTrue(np.all(fr.spectrum == 0))


class TestGlyphs(unittest.TestCase):
    def test_width_and_fit(self):
        self.assertEqual(text_width("abc"), 3)
        self.assertEqual(text_width("曲目"), 4)
        self.assertEqual(text_width(fit("曲目", 6)), 6)
        self.assertEqual(text_width(fit("\x1b[31mabc\x1b[0m", 5)), 5)
        self.assertEqual(time_fmt(3725), "1:02:05")
        self.assertEqual(time_fmt(65), "01:05")

    def test_braille_shape(self):
        sig = np.sin(np.linspace(0, 20, 4096)).astype(np.float32)
        rows = braille_scope(sig, cells=40, rows=4)
        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertEqual(text_width(r), 40)
        self.assertTrue(any(ch.strip() for ch in rows))

    def test_braille_silence(self):
        rows = braille_scope(np.zeros(4096, np.float32), cells=10, rows=4)
        self.assertEqual(rows, [" " * 10] * 4)

    def test_spectrum_width(self):
        vals = np.linspace(0, 1, 64, dtype=np.float32)
        line = spectrum_line(vals, 80, THEMES["phosphor"], vals)
        self.assertEqual(text_width(line), 80)
        self.assertEqual(text_width(waterfall_line(vals, 60, THEMES["amber"])), 60)


def make_ui_state(**kw) -> UIState:
    disc = build_fake_disc()
    st = UIState(
        device="/dev/sr0", model="HL-DT-ST DVDRAM GUD0N",
        tracks=disc.tracks, track_no=1, title="さくら", artist="森山直太朗",
        position=12.0, duration=disc.tracks[0].seconds, state="playing",
        volume=0.85, theme=THEMES["phosphor"], vis=VisFrame(),
        cache_lead=12.5, rip={"track": 1, "pct": 55.0, "speed": 3.2,
                              "errors": {"V": 0, "!": 1, "e": 2, "+": 0}},
        total_seconds=disc.total_seconds,
    )
    st.__dict__.update(kw)
    for attr, val in kw.items():
        setattr(st, attr, val)
    return st


class TestOutputErrorMessages(unittest.TestCase):
    """P1③：缺 pw-cat 和 PipeWire 会话没起来要给出不同的下一步。"""

    def test_missing_pw_cat(self):
        from audio.engine import _launch_error, _output_error
        self.assertIn("找不到 pw-cat", _launch_error(FileNotFoundError("pw-cat")))
        with mock.patch("audio.engine.shutil.which", return_value=None):
            self.assertIn("找不到 pw-cat", _output_error())

    def test_pipewire_session_not_running(self):
        from audio.engine import _output_error
        with mock.patch("audio.engine.shutil.which", return_value="/usr/bin/pw-cat"), \
             mock.patch("audio.engine.pipewire_socket", return_value=None):
            message = _output_error()
        self.assertIn("PipeWire 会话", message)
        self.assertIn("systemctl", message)


class TestExternalText(unittest.TestCase):
    """P1②：CD-TEXT / 网络 / 旧缓存里的文字不可信，进 UI 前必须压成单行。"""

    def test_one_line_strips_controls_and_ansi(self):
        from textfmt import one_line
        self.assertEqual(one_line("夜の\nドライブ"), "夜の ドライブ")
        self.assertEqual(one_line("\x1b[31m红色\x1b[0m"), "红色")
        self.assertEqual(one_line("a\tb\r\nc"), "a b c")
        self.assertEqual(one_line("\u3000 \u200b"), "")
        self.assertEqual(one_line(None), "")
        self.assertEqual(one_line(123), "123")

    def test_one_line_truncates_long_titles(self):
        from textfmt import one_line
        self.assertEqual(len(one_line("长" * 500)), 200)

    def test_fit_never_emits_newlines(self):
        row = fit("abc\ndef", 10)
        self.assertNotIn("\n", row)
        self.assertEqual(text_width(row), 10)

    def test_metadata_titles_are_sanitized(self):
        from services.metadata import _release_meta, _titles
        titles = _titles([{"number": "1", "title": "A\nB"},
                          {"number": "2", "title": "C\x1b[31mD"}])
        self.assertEqual(titles, {"1": "A B", "2": "CD"})
        meta = _release_meta({"title": "专辑\n二", "artist-credit": [
            {"name": "歌手\n名"}]}, titles)
        self.assertEqual(meta["album"], "专辑 二")
        self.assertEqual(meta["artist"], "歌手 名")

    def test_user_agent_has_version_and_public_url(self):
        from config import PROJECT_URL, VERSION
        from services.metadata import USER_AGENT
        self.assertIn(VERSION, USER_AGENT)
        self.assertIn(PROJECT_URL, USER_AGENT)
        self.assertIn("ASCII_CD_PLAYER", PROJECT_URL, "公开仓地址")
        self.assertNotIn("github.com/local", USER_AGENT)


class TestLayouts(unittest.TestCase):
    def _state(self, **kw):
        return make_ui_state(**kw)

    def test_dash_80x24(self):
        rows = render(self._state(), 80, 24)
        self.assertLessEqual(len(rows), 24)
        for r in rows:
            self.assertLessEqual(text_width(r), 80)

    def test_dash_120x40(self):
        rows = render(self._state(), 120, 40)
        self.assertLessEqual(len(rows), 40)

    def test_default_note_in_bottom_border(self):
        """默认布局没有 KEYS 行：提示嵌进底框，不加行、不挤掉内容。"""
        for h in (25, 63):
            plain = render(self._state(layout="dash"), 117, h)
            for kw in ({"hint": "缓冲中 0.8s / 6s（抓轨 2.0x）"},
                       {"message": "disc finished — looping"}):
                noted = render(self._state(layout="dash", **kw), 117, h)
                self.assertEqual(len(noted), len(plain))
                visible = [strip_ansi(r) for r in noted]
                border = [v for v in visible if v.startswith("└")]
                self.assertEqual(len(border), 1)
                self.assertIn(kw.get("hint") or kw.get("message"), border[0])
                self.assertTrue(any("OSCILLOSCOPE" in v for v in visible))
                self.assertFalse(any("KEYS" in v for v in visible))

    def test_help_note_replaces_keys(self):
        """帮助布局里，瞬时提示顶掉 KEYS 行，不额外占行。"""
        for h in (25, 63):
            plain = render(self._state(layout="dash", help=True), 117, h)
            noted = render(self._state(layout="dash", help=True,
                                       hint="缓冲中 0.8s / 6s（抓轨 2.0x）"), 117, h)
            self.assertEqual(len(noted), len(plain))
            visible = [strip_ansi(r) for r in noted]
            self.assertTrue(any("STATUS" in v for v in visible))
            self.assertFalse(any("KEYS" in v for v in visible))

    def test_dash_keys_underlined(self):
        """帮助布局的按键行：键位嵌在动作词里靠下划线强调。"""
        rows = render(self._state(layout="dash", help=True), 117, 60)
        keys = [r for r in rows if "play/pause" in strip_ansi(r)]
        self.assertEqual(len(keys), 1)
        row = keys[0]
        self.assertIn("\x1b[4m", row)
        self.assertIn("\x1b[24m", row)
        self.assertNotIn(fg(THEMES["phosphor"]["accent"]), row)
        self.assertNotIn("\x1b[1m", row)
        self.assertLessEqual(text_width(row), 117)
        self.assertIn("\x1b[4ml\x1b[24mayout", row)
        self.assertIn("\x1b[4mh\x1b[24melp", row)
        self.assertIn("\x1b[4mm\x1b[24mini", row)
        self.assertIn("\x1b[4mt\x1b[24mheme", row)
        self.assertIn("\x1b[4mq\x1b[24muit", row)
        self.assertNotIn("\x1b[4m□", row)
        self.assertIn("□ play/pause", strip_ansi(row))
        self.assertIn("play/pause  ←/→", strip_ansi(row))

    def test_help_keys_include_stop_and_replay(self):
        """105 列帮助布局也要放得下 s 停止 / r 重播。"""
        rows = render(self._state(layout="dash", help=True), 105, 25)
        row = next(r for r in rows if "play/pause" in strip_ansi(r))
        self.assertIn("\x1b[4ms\x1b[24mtop", row)
        self.assertIn("\x1b[4mr\x1b[24meplay", row)
        self.assertLessEqual(text_width(row), 105)

    def test_stop_fades_level_to_silence(self):
        """停止待机时电平淡到 -90；暂停冻结时保持原读数。"""
        live = VisFrame(rms_db=(-18.0, -17.0))
        stopped = [strip_ansi(r) for r in render(
            self._state(state="stopped", scope_mix=1.0, idle_t=2.0, vis=live), 117, 25)]
        paused = [strip_ansi(r) for r in render(
            self._state(state="paused", scope_mix=0.0, vis=live), 117, 25)]
        self.assertTrue(any("-90.0 dB" in r for r in stopped))
        self.assertFalse(any("-18.0 dB" in r for r in stopped))
        self.assertTrue(any("-18.0 dB" in r for r in paused))

    def test_spectrum_rows_default_two_help_one(self):
        default = [strip_ansi(r) for r in
                   render(self._state(layout="dash", scope_mix=1.0, idle_t=2.0),
                          117, 25)]
        help_rows = [strip_ansi(r) for r in
                     render(self._state(layout="dash", scope_mix=1.0,
                                        idle_t=2.0, help=True), 117, 25)]
        for rows, bars, wf in ((default, 2, 4), (help_rows, 1, 3)):
            i = next(i for i, r in enumerate(rows) if "SPECTRUM" in r)
            j = i + 1
            while "└" not in rows[j]:
                j += 1
            self.assertEqual(j - i - 1, bars)
            k = j + 1
            while "OSCILLOSCOPE" not in rows[k]:
                k += 1
            self.assertEqual(k - j - 1, wf)

    def test_minimal_narrow(self):
        rows = render(self._state(), 52, 12)
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(text_width(r), 52)

    def test_tiny_terminal_no_crash(self):
        rows = render(self._state(), 30, 6)
        self.assertTrue(rows)

    def test_mini_entry(self):
        rows = render(self._state(layout="mini"), 100, 40)
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 8)      # mini 是 3–5 行
        self.assertIn("asciicd", strip_ansi(rows[0]))


class TestDashBlockLayout(unittest.TestCase):
    """P2：高度不够时按整块裁剪，不能把标题和内容拆散、不能越界。"""

    @staticmethod
    def _vis() -> VisFrame:
        fr = VisFrame()
        fr.spectrum = (np.abs(np.sin(np.linspace(0, 6, 64))) * 0.8).astype(np.float32)
        fr.scope_l = fr.scope_r = (np.sin(np.linspace(0, 8, 1024)) * 0.5).astype(np.float32)
        fr.waterfall = [fr.spectrum * 0.6, fr.spectrum * 0.4, fr.spectrum * 0.2]
        return fr

    def _state(self, **kw):
        kw.setdefault("vis", self._vis())
        return make_ui_state(**kw)

    @staticmethod
    def _plain(rows: list[str]) -> list[str]:
        return [strip_ansi(r) for r in rows]

    def test_all_heights_fit_and_no_orphan_sections(self):
        for h in range(14, 61):
            rows = render(self._state(), 117, h)
            plain = self._plain(rows)
            self.assertLessEqual(len(rows), h, f"h={h} 行数越界")
            for row in rows:
                self.assertLessEqual(text_width(row), 117, f"h={h} 行宽越界")
            for i, row in enumerate(plain):
                if "├─" not in row:
                    continue
                nxt = plain[i + 1] if i + 1 < len(plain) else ""
                self.assertTrue(nxt.startswith("│"), f"h={h} 第 {i} 行分隔线后没有内容")
                self.assertNotIn("├─", nxt, f"h={h} 第 {i} 行分隔线后又是分隔线")

    def test_oscilloscope_title_and_waveform_together(self):
        for h in range(14, 61):
            plain = self._plain(render(self._state(), 117, h))
            has_title = any("OSCILLOSCOPE" in row for row in plain)
            has_wave = any(any("\u2800" <= ch <= "\u28ff" for ch in row)
                           for row in plain)
            self.assertEqual(has_title, has_wave, f"h={h} 示波器标题与波形不同在")

    def test_25_rows_keeps_every_section(self):
        plain = self._plain(render(self._state(), 117, 25))
        self.assertEqual(len(plain), 25)
        for name in ("NOW PLAYING", "SPECTRUM", "OSCILLOSCOPE", "LEVEL",
                     "DISC CACHE"):
            self.assertTrue(any(name in row for row in plain), f"25 行缺 {name}")
        self.assertFalse(any("KEYS" in row for row in plain))
        help_rows = self._plain(render(self._state(help=True), 117, 25))
        self.assertTrue(any("KEYS" in row for row in help_rows))

    def test_short_layout_drops_whole_blocks(self):
        plain14 = self._plain(render(self._state(), 117, 14))
        self.assertFalse(any("OSCILLOSCOPE" in row for row in plain14))
        for name in ("NOW PLAYING", "SPECTRUM", "DISC CACHE", "LEVEL"):
            self.assertTrue(any(name in row for row in plain14), f"14 行缺 {name}")

    def test_status_note_never_overflows_tight_heights(self):
        for h in (14, 19, 21, 25):
            rows = render(self._state(hint="缓冲中 0.8s / 6s（抓轨 2.0x）"), 117, h)
            visible = self._plain(rows)
            self.assertLessEqual(len(rows), h, f"h={h} 越界")
            self.assertTrue(any("缓冲中" in row for row in visible),
                            f"h={h} 提示丢了")
            self.assertFalse(any("KEYS" in row for row in visible))

    def test_cache_row_shows_phases_buffer_and_error_map(self):
        st = make_ui_state(
            state="buffering", cache_lead=2.1,
            rip={"track": 1, "pct": 42, "speed": 3.2,
                 "errors": {"V": 0, "!": 0, "+": 0, "e": 0},
                 "phase": "read"})
        rows = [strip_ansi(row) for row in render(st, 105, 25)]
        cache = next(row for row in rows if "DISC CACHE" in row)
        i = rows.index(cache)
        line = rows[i + 1]
        for word in ("read", "verify", "overlap", "BUF", "AUTO"):
            self.assertIn(word, line)
        self.assertLessEqual(text_width(render(st, 105, 25)[i + 1]), 105)

        st = make_ui_state(
            state="paused", cache_lead=2.4,
            rip={"track": 1, "pct": 47, "speed": 1.2,
                 "errors": {"V": 2, "!": 1, "+": 4, "e": 1},
                 "phase": "read",
                 "map": [0, 0, 0, 3, 0, 0, 0, 1, 2, 0,
                         0, 0, 0, 0, 1, 0, 0, 1, 0, 0]})
        rows = [strip_ansi(row) for row in render(st, 105, 25)]
        line = rows[next(i for i, row in enumerate(rows)
                          if "DISC CACHE" in row) + 1]
        for word in ("V2", "!1", "+4", "retry 1/3", "MAP"):
            self.assertIn(word, line)

    def test_cache_row_shows_paused_when_stopped(self):
        st = make_ui_state(
            state="stopped", cache_lead=0.0,
            rip={"track": None, "stage": "paused", "pct": 0.0, "speed": 0.0,
                 "errors": {"V": 0, "!": 0, "+": 0, "e": 0}})
        rows = [strip_ansi(row) for row in render(st, 105, 25)]
        line = rows[next(i for i, row in enumerate(rows)
                          if "DISC CACHE" in row) + 1]
        self.assertIn("RIP PAUSED", line)
        self.assertIn("buf", line)

    def test_buffer_waterline_survives_read_errors(self):
        """缓冲中读盘出错：BUF/AUTO 不能被 MAP 分支抢掉。

        stage="" 是普通读错误；stage="error" 表示还会重试，二者都不该
        抢掉水位；只有最终 stage="failed" 才允许切到错误/MAP 行。
        """
        for stage in ("", "error"):
            with self.subTest(stage=stage):
                st = make_ui_state(
                    state="buffering", cache_lead=2.4,
                    rip={"track": 1, "pct": 47, "speed": 1.2,
                         "errors": {"V": 2, "!": 1, "+": 4, "e": 1},
                         "phase": "read", "stage": stage,
                         "map": [0, 0, 0, 3, 0, 0, 0, 1, 2, 0,
                                 0, 0, 0, 0, 1, 0, 0, 1, 0, 0]})
                rows = [strip_ansi(row) for row in render(st, 105, 25)]
                line = rows[next(i for i, row in enumerate(rows)
                                  if "DISC CACHE" in row) + 1]
                for word in ("V2", "!1", "+4", "AUTO"):
                    self.assertIn(word, line)
                self.assertIn("buf", line.lower())
                self.assertIn("2.4/6.0s", line)
                if stage == "error":
                    self.assertIn("ERROR", line)
                    self.assertIn("retry 1/3", line)

        # 最终放弃才允许抢回错误/MAP 行。
        st = make_ui_state(
            state="buffering", cache_lead=2.4,
            rip={"track": 1, "pct": 47, "speed": 1.2,
                 "errors": {"V": 2, "!": 1, "+": 4, "e": 3},
                 "phase": "read", "stage": "failed",
                 "map": [0] * 19 + [3]})
        rows = [strip_ansi(row) for row in render(st, 105, 25)]
        line = rows[next(i for i, row in enumerate(rows)
                          if "DISC CACHE" in row) + 1]
        self.assertIn("FAILED", line)
        self.assertIn("MAP", line)
        self.assertNotIn("AUTO", line)

    def test_error_map_keeps_tail_when_narrow(self):
        """84 列把 20 桶压成 12 格时，最后一格坏道仍要出现。"""
        st = make_ui_state(
            state="paused", cache_lead=6.0,
            rip={"track": 1, "pct": 47, "speed": 1.2,
                 "errors": {"V": 1, "!": 1, "+": 4, "e": 1},
                 "phase": "read", "map": [0] * 19 + [3]})
        rows = [strip_ansi(row) for row in render(st, 84, 25)]
        line = rows[next(i for i, row in enumerate(rows)
                          if "DISC CACHE" in row) + 1]
        self.assertIn("MAP", line)
        tail = line.split("MAP", 1)[1].split("│", 1)[0].rstrip()
        self.assertEqual(len(tail[-12:]), 12)
        self.assertEqual(tail[-1], "█")
        self.assertNotIn("█", tail[-12:-1])

    def test_short_phase_names_highlight_actual_phase(self):
        """84 列用 read/ver/ovl 缩写时，verify/overlap 要真正高亮。"""
        accent = fg(THEMES["phosphor"]["accent"])
        dim = fg(THEMES["phosphor"]["dim"])
        for phase, short in (("verify", "ver"), ("overlap", "ovl")):
            st = make_ui_state(
                state="paused", cache_lead=6.0,
                rip={"track": 1, "pct": 60, "speed": 2.0,
                     "errors": {"V": 0, "!": 0, "+": 0, "e": 0},
                     "phase": phase})
            rows = render(st, 84, 25)
            line = rows[next(i for i, row in enumerate(rows)
                              if "DISC CACHE" in row) + 1]
            self.assertIn(accent + short + RESET, line)
            self.assertIn(dim + "read" + RESET, line)
            self.assertNotIn(accent + "read" + RESET, line)

    def test_default_layout_has_no_keys(self):
        rows = self._plain(render(
            self._state(layout="dash", scope_mix=1.0, idle_t=2.0), 117, 25))
        self.assertEqual(len(rows), 25)
        self.assertFalse(any("KEYS" in row for row in rows))
        self.assertFalse(any("play/pause" in row for row in rows))
        for name in ("NOW PLAYING", "SPECTRUM", "OSCILLOSCOPE", "LEVEL",
                     "DISC CACHE"):
            self.assertTrue(any(name in row for row in rows), f"默认缺 {name}")
        self.assertEqual(rows[-1], "└" + "─" * (text_width(rows[-1]) - 2) + "┘")
        self.assertTrue(any("-46" in row for row in rows), "默认纵轴缺 -46 下限")
        i = next(i for i, row in enumerate(rows) if "OSCILLOSCOPE" in row)
        lvl = next(j for j in range(i + 1, len(rows)) if "LEVEL" in rows[j])
        self.assertEqual(lvl - i - 1, 6, "默认示波器仍为 6 行")

    def test_mini_never_exceeds_height(self):
        for h in range(5, 41):
            rows = render(self._state(layout="mini"), 100, h)
            self.assertLessEqual(len(rows), h, f"mini h={h} 越界")


class TestTrackTransitionUI(unittest.TestCase):
    THEME = THEMES["phosphor"]

    def _state(self, *, mode="next", elapsed=0.15, current_no=3,
               current_title="さくら（独唱）", current_duration=252.0,
               old_no=2, old_title="夏の終わり", old_duration=228.0,
               old_position=108.0):
        st = make_ui_state(
            track_no=current_no, title=current_title,
            artist="森山直太朗", position=0.0, duration=current_duration,
            state="playing", cache_lead=6.0)
        st.tracks = [object()] * 16
        st.track_transition = TrackTransition(
            mode=mode, old_track_no=old_no, old_title=old_title,
            old_artist="森山直太朗", old_position=old_position,
            old_duration=old_duration, elapsed=elapsed)
        return st

    @staticmethod
    def _now(st):
        rows = [strip_ansi(row) for row in render(st, 105, 25)]
        i = next(i for i, row in enumerate(rows) if "NOW PLAYING" in row)
        return rows[i], rows[i + 1], rows[i + 2]

    def test_auto_next_keeps_full_bar_then_fades(self):
        st = self._state(mode="auto", elapsed=0.29, old_position=228.0)
        sep, title, prog = self._now(st)
        self.assertIn("▸ NEXT 02/16 → 03/16", sep)
        self.assertNotIn("AUTO", sep, "自动/手动统一用 NEXT，不靠文字区分")
        self.assertIn("100%", prog)
        self.assertIn("stay 00:00", prog)
        self.assertGreaterEqual(prog.count("█"), 60)
        raw = next(row for row in render(st, 105, 25)
                   if "NOW PLAYING" in strip_ansi(row))
        self.assertIn(bg(self.THEME["accent"]), raw)

        st.track_transition.elapsed = 0.31
        sep, title, prog = self._now(st)
        self.assertIn("03", title)
        self.assertIn("さくら", title)
        self.assertIn("0%", prog)

        st.track_transition.elapsed = 0.80
        sep, _, prog = self._now(st)
        self.assertIn("▸ NEXT 02/16 → 03/16", sep)
        self.assertIn("0%", prog)

        st.track_transition.elapsed = 1.10
        sep, _, _ = self._now(st)
        self.assertNotIn("NEXT", sep)

    def test_manual_next_fills_bar_and_time(self):
        st = self._state(mode="next", elapsed=0.15, old_position=108.0)
        sep, title, prog = self._now(st)
        self.assertIn("NEXT 02/16 → 03/16", sep)
        self.assertIn("02:48 / 03:48", title)
        self.assertIn("74%", prog)
        self.assertIn("stay 01:00", prog)

        st.track_transition.elapsed = 0.31
        _, title, prog = self._now(st)
        self.assertIn("03", title)
        self.assertIn("0%", prog)

    def test_manual_prev_drains_bar_and_time(self):
        st = self._state(
            mode="prev", elapsed=0.15, current_no=2,
            current_title="夏の終わり", current_duration=228.0,
            old_no=3, old_title="さくら（独唱）", old_duration=252.0,
            old_position=96.0)
        sep, title, prog = self._now(st)
        self.assertIn("PREV 03/16 → 02/16", sep)
        self.assertIn("00:48 / 04:12", title)
        self.assertIn("19%", prog)
        self.assertIn("stay 03:24", prog)

        st.track_transition.elapsed = 0.31
        _, title, prog = self._now(st)
        self.assertIn("02", title)
        self.assertIn("0%", prog)

    def test_transition_rows_fit_responsive_widths(self):
        for w in (66, 80, 105, 117):
            st = self._state(mode="next", elapsed=0.15)
            rows = render(st, w, 25)
            self.assertEqual(len(rows), 25)
            for row in rows:
                self.assertLessEqual(text_width(row), w, f"w={w} 行宽越界")

    def test_rebased_badge_and_bar_use_the_same_direction(self):
        st = self._state(mode="prev", elapsed=0.15, current_no=3,
                         current_title="03 已显示", old_no=7,
                         old_title="07 可见", old_duration=210.0,
                         old_position=42.0)
        st.track_transition.origin_no = 7
        sep, title, prog = self._now(st)
        self.assertIn("▸ PREV 07/16 → 03/16", sep)
        self.assertIn("07 可见", title)
        self.assertIn("10%", prog,
                      "07→03 应排空；徽标数字与进度条方向一致")

    def test_round_trip_to_visible_track_drains_instead_of_jumping(self):
        before_st = self._state(
            mode="next", elapsed=0.15, current_no=7,
            current_title="07 尚未显示", current_duration=210.0,
            old_no=2, old_title="02 正在显示", old_duration=60.0,
            old_position=12.0)
        before_sep, before_title, before_prog = self._now(before_st)
        self.assertIn("NEXT 02/16 → 07/16", before_sep)
        self.assertIn("02 正在显示", before_title)

        tt = before_st.track_transition
        visual = transition_view(
            tt,
            TrackSnapshot(number=7, title="07 尚未显示", artist="",
                          position=0.0, duration=210.0),
            tt.elapsed,
        )
        after_st = make_ui_state(
            track_no=2, title="Track 02", artist="", position=0.0,
            duration=60.0, state="playing", cache_lead=6.0)
        after_st.tracks = [object()] * 16
        after_st.track_transition = TrackTransition(
            mode="prev", origin_no=7,
            old_track_no=2, old_title="02 正在显示", old_artist="",
            old_position=visual.track.position,
            old_duration=60.0,
            source_title_fade=visual.title_fade,
            source_bar_fade=visual.bar_fade,
            elapsed=0.0,
        )
        after_sep, after_title, after_prog = self._now(after_st)
        self.assertIn("PREV 07/16 → 02/16", after_sep,
                      "回到可见轨不能生成 02→02 徽标")
        self.assertIn("02 正在显示", after_title)
        self.assertEqual(strip_ansi(before_prog), strip_ansi(after_prog),
                         "首帧进度要从可见位置接续，不能跳到 0%")
        after_st.track_transition.elapsed = 0.15
        later_prog = self._now(after_st)[2]
        self.assertNotEqual(strip_ansi(after_prog), strip_ansi(later_prog))
        self.assertLess(float(re.search(r"(\d+)%", strip_ansi(later_prog)).group(1)),
                        float(re.search(r"(\d+)%", strip_ansi(after_prog)).group(1)))

    def test_business_prev_with_visible_02_drains_and_rebases_badge(self):
        st = self._state(mode="prev", elapsed=0.15, current_no=3,
                         current_title="03 目标", old_no=2,
                         old_title="02 可见", old_duration=60.0,
                         old_position=48.0)
        st.track_transition.origin_no = 7
        sep, title, prog = self._now(st)
        self.assertIn("PREV 07/16 → 03/16", sep)
        self.assertIn("02 可见", title)
        self.assertIn("40%", prog,
                      "业务方向 07→03 排空，即使可见起点还是 02")

    def test_retarget_renders_continuous_progress_and_title(self):
        st = self._state(mode="next", elapsed=0.15, current_no=3,
                         current_title="さくら（独唱）", current_duration=252.0,
                         old_no=2, old_title="夏の終わり", old_duration=228.0,
                         old_position=108.0)
        before_sep, before_title, before_prog = self._now(st)
        self.assertIn("NEXT 02/16 → 03/16", before_sep)
        self.assertIn("02:48 / 03:48", before_title)
        self.assertIn("74%", before_prog)

        tt = st.track_transition
        visual = transition_view(
            tt,
            TrackSnapshot(number=st.track_no, title=st.title,
                          artist=st.artist, position=st.position,
                          duration=st.duration),
            tt.elapsed,
        )
        st.track_transition = TrackTransition(
            mode="next", origin_no=tt.origin_no,
            old_track_no=visual.track.number, old_title=visual.track.title,
            old_artist=visual.track.artist,
            old_position=visual.track.position,
            old_duration=visual.track.duration,
            source_title_fade=visual.title_fade,
            source_bar_fade=visual.bar_fade,
            elapsed=0.0,
        )
        after_sep, after_title, after_prog = self._now(st)
        self.assertIn("NEXT 02/16 → 03/16", after_sep)
        self.assertEqual((strip_ansi(before_title), strip_ansi(before_prog)),
                         (strip_ansi(after_title), strip_ansi(after_prog)),
                         "重定向首帧不能把 75%→0%，也不能换回未显示过的曲名")
        later = transition_view(
            st.track_transition,
            TrackSnapshot(number=st.track_no, title=st.title,
                          artist=st.artist, position=st.position,
                          duration=st.duration),
            0.15,
        )
        self.assertGreater(later.track.position, visual.track.position)
        self.assertGreater(later.title_fade, visual.title_fade)

    def test_rapid_switch_badge_uses_group_origin_and_latest_target(self):
        st = self._state(mode="next", elapsed=0.15, current_no=9,
                         current_title="最終曲", old_no=3,
                         old_title="07 の次", old_duration=200.0,
                         old_position=0.0)
        st.track_transition.origin_no = 2
        sep, title, prog = self._now(st)
        self.assertIn("▸ NEXT 02/16 → 09/16", sep)
        self.assertIn("07 の次", title,
                      "退场源应是最新业务轨，不回闪本轮更早的旧曲名")
        self.assertIn("0%", prog)


class TestVisualPulseUI(unittest.TestCase):
    def _state(self, kind="seek", label="SEEK +5s", elapsed=0.05,
               direction=1, hold=0.0, idle=0.0, **kw):
        st = make_ui_state(
            track_no=4, title="Group 4", artist="Artist", position=42.0,
            duration=240.0, state=kw.pop("state", "playing"),
            cache_lead=kw.pop("cache_lead", 6.0))
        st.visual_pulse = VisualPulse(
            kind=kind, label=label, elapsed=elapsed, duration=0.22,
            direction=direction, hold=hold, idle=idle)
        return st

    def _rows(self, st, w=105, h=25):
        return [strip_ansi(row) for row in render(st, w, h)]

    def test_seek_pulse_replaces_now_playing_separator(self):
        st = self._state(elapsed=0.02)
        rows = self._rows(st)
        sep = next(row for row in rows if "NOW PLAYING" in row)
        self.assertIn("» SEEK +5s", sep)
        self.assertTrue(any("00:42 / 04:00" in row for row in rows),
                        "提示期间仍显示真实播放头，不插值")
        self.assertEqual(len(rows), 25)
        raw = next(row for row in render(st, 105, 25)
                   if "NOW PLAYING" in strip_ansi(row))
        self.assertIn(bg(THEMES["phosphor"]["accent"]), raw)

    def test_seek_short_press_stays_one_arrow(self):
        st = self._state(elapsed=0.20)
        rows = self._rows(st)
        sep = next(row for row in rows if "NOW PLAYING" in row)
        self.assertIn("» SEEK +5s", sep)
        self.assertNotIn("»»", sep,
                         "短按不能按时间自己长出更多箭头")

    def test_seek_hold_grows_to_three_then_decays(self):
        st = self._state(hold=SEEK_HOLD_MAX_S)
        sep = next(row for row in self._rows(st) if "NOW PLAYING" in row)
        self.assertIn("»»» SEEK +5s", sep)

        st = self._state(hold=SEEK_ARROW_3_S)
        sep = next(row for row in self._rows(st) if "NOW PLAYING" in row)
        self.assertIn("»»» SEEK +5s", sep)

        # 松开后先回落到 2 个，再回到 1 个，最后才消失。
        st = self._state(hold=SEEK_HOLD_MAX_S,
                         idle=SEEK_HOLD_GAP_S + SEEK_HOLD_RELEASE_S * 0.5)
        sep = next(row for row in self._rows(st) if "NOW PLAYING" in row)
        self.assertIn("»» SEEK +5s", sep)

        st = self._state(hold=SEEK_HOLD_MAX_S,
                         idle=SEEK_HOLD_GAP_S + SEEK_HOLD_RELEASE_S)
        sep = next(row for row in self._rows(st) if "NOW PLAYING" in row)
        self.assertIn("» SEEK +5s", sep)
        self.assertNotIn("»»", sep)

        st = self._state(kind="paused", label="PAUSED", elapsed=0.02,
                         direction=0, state="paused")
        rows = self._rows(st)
        sep = next(row for row in rows if "NOW PLAYING" in row)
        self.assertIn("‖ PAUSED", sep)
        self.assertIn("00:42 / 04:00",
                      next(row for row in rows if "♪ Group 4" in row))

    def test_no_pulse_returns_plain_separator(self):
        st = self._state()
        st.visual_pulse = None
        sep = next(row for row in self._rows(st) if "NOW PLAYING" in row)
        self.assertNotIn("SEEK", sep)

    def test_pulse_fits_responsive_widths_and_mini(self):
        for w in (66, 80, 84, 105, 117):
            st = self._state(elapsed=0.05)
            rows = render(st, w, 25)
            self.assertEqual(len(rows), 25)
            for row in rows:
                self.assertLessEqual(text_width(row), w, f"w={w} 行宽越界")
        st = self._state(elapsed=0.05)
        st.layout = st.minimal = False
        mini = [strip_ansi(row) for row in render(st, 80, 8)]
        self.assertTrue(any("SEEK +5s" in row for row in mini))
        for row in render(st, 80, 8):
            self.assertLessEqual(text_width(row), 80)


class TestThemes(unittest.TestCase):
    KEYS = ("spec_lo", "spec_hi", "scope", "text", "dim", "accent",
            "warn", "err", "bar", "bg_bar")

    def test_all_themes_complete(self):
        for name, theme in THEMES.items():
            for key in self.KEYS:
                self.assertIn(key, theme, f"{name} 缺 {key}")

    def test_jpop_present(self):
        self.assertIn("jpop", THEMES)
        self.assertEqual(THEMES["jpop"]["scope"], (255, 170, 225))


class TestPrefs(unittest.TestCase):
    def test_roundtrip(self):
        import prefs
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "asciicd", "state.json")
            self.assertEqual(prefs.load(p), {})
            self.assertTrue(prefs.save({"theme": "jpop"}, p))
            self.assertEqual(prefs.load(p), {"theme": "jpop"})

    def test_missing_or_corrupt_is_ignored(self):
        import prefs
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "state.json")
            self.assertEqual(prefs.load(p), {})
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertEqual(prefs.load(p), {})
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("[1, 2]")
            self.assertEqual(prefs.load(p), {})


class TestCache(unittest.TestCase):
    def test_available_and_done(self):
        disc = build_fake_disc(durations=(1.0,))
        expected = disc.track(1).pcm_bytes
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            cache.ensure_dir(disc)                    # 路径助手不再顺手建目录
            self.assertEqual(cache.available_bytes(disc, 1), 0)
            part = cache.part_path(disc, 1)
            part.write_bytes(b"\x00" * 1764)
            self.assertEqual(cache.available_bytes(disc, 1), 1764)
            self.assertFalse(cache.is_done(disc, 1))
            cache.pcm_path(disc, 1).write_bytes(b"\x00" * expected)
            cache.mark_done(disc, 1)
            self.assertTrue(cache.is_done(disc, 1))
            self.assertEqual(cache.title_of(disc, 3), ("Track 03", ""))


class TestCachePurge(unittest.TestCase):
    """换碟清缓存：删别的盘的 PCM，但曲名缓存（meta.json）留着；别误伤 fontcache。"""

    def test_purge_drops_other_audio_but_keeps_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            keep = build_fake_disc(durations=(1.0,))
            other = build_fake_disc(durations=(2.0,))
            cache.ensure_dir(keep)
            cache.ensure_dir(other)
            (cache.disc_dir(keep) / "01.pcm").write_bytes(b"k")
            (cache.disc_dir(other) / "01.pcm").write_bytes(b"o")
            cache.write_meta(other, {"album": "旧盘专辑", "tracks": {"1": "旧曲"}})
            bystander = pathlib.Path(tmp) / "fontcache"
            bystander.mkdir()

            cache.purge_except(keep)

            self.assertTrue(cache.disc_dir(keep).exists())
            self.assertFalse((cache.disc_dir(other) / "01.pcm").exists())
            self.assertTrue(cache.meta_path(other).exists(),
                            "meta.json 留着，没网也能显示以前查过的曲名")
            self.assertEqual(cache.read_meta(other)["album"], "旧盘专辑")
            self.assertTrue(bystander.exists(), "缓存根目录里的非 discid 目录不能删")

    def test_purge_removes_empty_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            keep = build_fake_disc(durations=(1.0,))
            other = build_fake_disc(durations=(2.0,))
            cache.ensure_dir(other)
            (cache.disc_dir(other) / "01.pcm").write_bytes(b"o")

            cache.purge_except(keep)

            self.assertFalse(cache.disc_dir(other).exists(), "没有 meta 的空壳顺手收掉")

    def test_purge_on_missing_root_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(pathlib.Path(tmp) / "not-yet")
            cache.purge_except(build_fake_disc(durations=(1.0,)))   # 不抛

    def test_clear_removes_disc_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            disc = build_fake_disc(durations=(1.0,))
            cache.ensure_dir(disc)
            cache.mark_done(disc, 1)
            cache.clear(disc)
            self.assertFalse(cache.disc_dir(disc).exists())

    def test_path_helpers_do_not_touch_disk(self):
        """路径计算不该建目录：UI 每帧查缓存，mkdir 白做几百次/秒。"""
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            disc = build_fake_disc(durations=(1.0,))
            cache.pcm_path(disc, 1)
            cache.part_path(disc, 1)
            cache.done_path(disc, 1)
            cache.meta_path(disc)
            cache.available_bytes(disc, 1)
            cache.read_meta(disc)
            self.assertFalse(cache.disc_dir(disc).exists())


class TestCacheSafety(unittest.TestCase):
    """P0①：缓存根里可能住着别人的文件，也可能有第二个实例在写同一张盘。"""

    def test_ensure_format_keeps_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            other = root / "other-project"
            other.mkdir()
            (other / "sample.pcm").write_bytes(b"keep")
            (root / "notes.txt").write_text("keep")
            disc = build_fake_disc(durations=(1.0,))
            cache = Cache(tmp)
            cache.ensure_dir(disc)
            old = cache.disc_dir(disc) / "01.pcm"
            old.write_bytes(b"old-format")
            (root / "format.json").write_text('{"pcm_version": 1}')

            cache.ensure_format()

            self.assertTrue((other / "sample.pcm").exists(),
                            "非 discid 目录里的同名文件不能删")
            self.assertTrue((root / "notes.txt").exists())
            self.assertFalse(old.exists(), "旧格式的 PCM 要重抓")
            self.assertFalse(cache.disc_dir(disc).exists(), "只剩空壳就收掉")

    def test_ensure_format_creates_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "not-yet"
            Cache(root).ensure_format()
            self.assertTrue((root / "format.json").exists())

    def test_ensure_format_recovers_from_non_dict_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "format.json").write_text("[]")
            Cache(tmp).ensure_format()          # 不抛 AttributeError
            self.assertIn("pcm_version", (root / "format.json").read_text())

    def test_foreign_dir_with_discid_name_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            disc = build_fake_disc(durations=(1.0,))
            foreign = root / disc.discid
            foreign.mkdir()
            (foreign / "README.md").write_text("not ours")
            (foreign / "01.pcm").write_bytes(b"x")
            cache = Cache(tmp)

            cache.ensure_format()
            self.assertFalse(cache.acquire(disc), "不能接管不属于我们的目录")
            self.assertTrue((foreign / "01.pcm").exists())
            self.assertFalse((foreign / ".asciicd-cache").exists())

    def test_corrupt_meta_json_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            disc = build_fake_disc(durations=(1.0,))
            cache.ensure_dir(disc)
            path = cache.meta_path(disc)
            for i, payload in enumerate(("[]", "[1, 2]", "{not json")):
                path.write_text(payload)
                os.utime(path, (1000 + i, 1000 + i))   # mtime 是秒级，配对递增
                self.assertEqual(cache.read_meta(disc), {})
            path.write_text('{"meta_version": %d, "album": "A\\nB"}'
                            % META_VERSION)
            os.utime(path, (2000, 2000))
            self.assertEqual(cache.read_meta(disc)["album"], "A B")

    def test_purge_skips_disc_dir_locked_by_another_instance(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            keep = build_fake_disc(durations=(1.0,))
            busy = build_fake_disc(durations=(2.0,))
            cache.ensure_dir(busy)
            part = cache.disc_dir(busy) / "01.pcm.part"
            part.write_bytes(b"active")
            fd = os.open(cache.disc_dir(busy) / ".lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                cache.purge_except(keep)
            finally:
                os.close(fd)
            self.assertTrue(part.exists(), "别的实例正在写的 .part 不能删")

    def test_purge_holds_disc_lock_until_files_are_gone(self):
        """#25：检查和删除必须在同一把锁里。

        旧实现先探一下锁、马上放开，再删文件；另一实例正好在这个空档拿到
        盘锁开始抓轨，它写下的 .pcm.part 就会被当成旧缓存删掉。这里在删除
        动作进行中开第二个实例去抢同一把锁：必须抢不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            keep = build_fake_disc(durations=(1.0,))
            busy = build_fake_disc(durations=(2.0,))
            cache = Cache(tmp)
            cache.ensure_format()                  # 先有 format.json 和根锁
            cache.ensure_dir(busy)
            (cache.disc_dir(busy) / "01.pcm").write_bytes(b"stale")

            seen = {}
            real_remove = cache._remove_audio

            def remove_between_check_and_delete(path):
                other = Cache(tmp)
                # 另一实例此刻启动，想拿这张盘的锁开抓
                seen["acquired"] = other.acquire(busy)
                if seen["acquired"]:
                    other.part_path(busy, 1).write_bytes(b"active")
                real_remove(path)
                other.close()

            with mock.patch.object(cache, "_remove_audio",
                                   remove_between_check_and_delete):
                cache.purge_except(keep)

            self.assertFalse(seen["acquired"],
                             "清理期间不能把盘锁让给别的实例")
            self.assertFalse((cache.disc_dir(busy) / "01.pcm").exists(),
                             "旧盘的 PCM 还是要清掉")

    def test_format_migration_skips_disc_in_use(self):
        """#25：换 PCM 格式时，另一实例正在抓的盘整块跳过，曲名缓存也留着。"""
        import fcntl

        from config import PCM_FORMAT_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            cache = Cache(tmp)
            active = build_fake_disc(durations=(1.0,))
            stale = build_fake_disc(durations=(2.0,))
            cache.ensure_dir(active)
            cache.ensure_dir(stale)
            cache.write_meta(active, {"album": "正在抓的盘"})
            (cache.disc_dir(active) / "01.pcm").write_bytes(b"old")
            (cache.disc_dir(active) / "01.pcm.part").write_bytes(b"active")
            (cache.disc_dir(stale) / "01.pcm").write_bytes(b"stale")
            (root / "format.json").write_text('{"pcm_version": 1}')
            fd = os.open(cache.disc_dir(active) / ".lock",
                         os.O_RDWR | os.O_CREAT)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                cache.ensure_format()
            finally:
                os.close(fd)

            self.assertTrue((cache.disc_dir(active) / "01.pcm").exists())
            self.assertTrue((cache.disc_dir(active) / "01.pcm.part").exists(),
                            "别的实例正在写的半成品不能删")
            self.assertTrue(cache.meta_path(active).exists(), "曲名缓存留下")
            self.assertFalse((cache.disc_dir(stale) / "01.pcm").exists(),
                             "没人在用的旧格式 PCM 还是要清")
            self.assertEqual(json.loads((root / "format.json").read_text())
                             ["pcm_version"], PCM_FORMAT_VERSION)

    def test_purge_keeps_foreign_files_in_disc_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            keep = build_fake_disc(durations=(1.0,))
            other = build_fake_disc(durations=(2.0,))
            cache.ensure_dir(other)
            (cache.disc_dir(other) / "01.pcm").write_bytes(b"o")
            (cache.disc_dir(other) / "notes.txt").write_text("mine")

            cache.purge_except(keep)

            self.assertFalse((cache.disc_dir(other) / "01.pcm").exists())
            self.assertTrue((cache.disc_dir(other) / "notes.txt").exists(),
                            "目录已认领，但没有文件名的陌生文件也不该删")

    def test_acquire_is_exclusive_per_disc(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0,))
            first, second = Cache(tmp), Cache(tmp)
            first.ensure_format()
            self.assertTrue(first.acquire(disc))
            try:
                self.assertFalse(second.acquire(disc), "同一张盘不能有两个写者")
            finally:
                first.close()
            self.assertTrue(second.acquire(disc))
            second.close()

    def test_fake_mode_uses_separate_cache_root(self):
        from types import SimpleNamespace
        from cdda.cache import cache_root_for
        base = "/tmp/asciicd-cache-base"
        real = cache_root_for(SimpleNamespace(cache_dir=base, fake=False))
        fake = cache_root_for(SimpleNamespace(cache_dir=base, fake=True))
        self.assertNotEqual(real, fake)
        self.assertEqual(fake.parent, real)
        self.assertTrue(str(fake).endswith("fake"))

    def test_clear_audio_removes_own_shell_even_while_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            cache.ensure_format()
            disc = build_fake_disc(durations=(1.0,))
            self.assertTrue(cache.acquire(disc))
            (cache.disc_dir(disc) / "01.pcm").write_bytes(b"pcm")
            cache.clear_audio(disc)
            self.assertFalse(cache.disc_dir(disc).exists(),
                             "退出时不能留空壳目录")
            cache.close()

    def test_isolated_cache_is_removed_on_close(self):
        cache = Cache.isolated()
        root = cache.root
        self.assertTrue(root.exists())
        cache.close()
        self.assertFalse(root.exists())


class TestCacheIntegrity(unittest.TestCase):
    """P1③：完成标记必须和 PCM 大小一致；截断/陈旧缓存不能喂给播放器。"""

    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        disc = build_fake_disc(durations=(0.5,))
        cache = Cache(tmp.name)
        cache.ensure_dir(disc)
        return disc, cache, disc.track(1).pcm_bytes

    def test_dangling_done_is_not_done(self):
        disc, cache, _expected = self._setup()
        cache.mark_done(disc, 1)                      # 只有标记没有 PCM
        self.assertFalse(cache.is_done(disc, 1))
        self.assertIsNone(cache.readable_path(disc, 1))
        self.assertEqual(cache.available_bytes(disc, 1), 0)

    def test_truncated_pcm_is_not_done(self):
        disc, cache, expected = self._setup()
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * (expected - 100))
        cache.mark_done(disc, 1)
        self.assertFalse(cache.is_done(disc, 1))
        self.assertIsNone(cache.readable_path(disc, 1))
        self.assertEqual(cache.available_bytes(disc, 1), 0)

    def test_exact_pcm_and_marker_is_done(self):
        disc, cache, expected = self._setup()
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * expected)
        cache.mark_done(disc, 1)
        self.assertTrue(cache.is_done(disc, 1))
        self.assertEqual(cache.readable_path(disc, 1), cache.pcm_path(disc, 1))
        self.assertEqual(cache.available_bytes(disc, 1), expected)

    def test_stale_final_does_not_shadow_part(self):
        """重抓时旧的无效 .pcm 不能遮蔽正在写的 .part。"""
        disc, cache, expected = self._setup()
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * (expected - 100))
        cache.mark_done(disc, 1)
        cache.part_path(disc, 1).write_bytes(b"\x01" * 777)
        self.assertEqual(cache.available_bytes(disc, 1), 777)
        self.assertEqual(cache.readable_path(disc, 1), cache.part_path(disc, 1))

    def test_reset_track_removes_stale_files(self):
        disc, cache, expected = self._setup()
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * (expected - 100))
        cache.mark_done(disc, 1)
        cache.part_path(disc, 1).write_bytes(b"\x01" * 777)
        cache.reset_track(disc, 1)
        for path in (cache.pcm_path(disc, 1), cache.part_path(disc, 1),
                     cache.done_path(disc, 1)):
            self.assertFalse(path.exists())
        self.assertEqual(cache.available_bytes(disc, 1), 0)

    def test_finalize_pads_short_pcm(self):
        disc, cache, expected = self._setup()
        cache.part_path(disc, 1).write_bytes(b"\x00" * (expected - 100))
        self.assertTrue(cache.finalize_track(disc, 1, tolerate=4096))
        self.assertEqual(cache.pcm_path(disc, 1).stat().st_size, expected)
        self.assertTrue(cache.is_done(disc, 1))
        self.assertEqual(cache.available_bytes(disc, 1), expected)
        self.assertEqual(cache.pcm_path(disc, 1).read_bytes()[-100:], b"\x00" * 100)

    def test_finalize_truncates_overshoot(self):
        disc, cache, expected = self._setup()
        cache.part_path(disc, 1).write_bytes(b"\x00" * (expected + 100))
        self.assertTrue(cache.finalize_track(disc, 1))
        self.assertEqual(cache.pcm_path(disc, 1).stat().st_size, expected)

    def test_finalize_rejects_incomplete(self):
        disc, cache, expected = self._setup()
        cache.part_path(disc, 1).write_bytes(b"\x00" * (expected - 5000))
        self.assertFalse(cache.finalize_track(disc, 1, tolerate=4096))
        self.assertFalse(cache.pcm_path(disc, 1).exists())
        self.assertFalse(cache.done_path(disc, 1).exists())

    def test_oversized_pcm_is_not_trusted(self):
        disc, cache, expected = self._setup()
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * (expected + 100))
        cache.mark_done(disc, 1)
        self.assertFalse(cache.is_done(disc, 1))
        self.assertIsNone(cache.readable_path(disc, 1))
        self.assertEqual(cache.available_bytes(disc, 1), 0)

    def test_repair_finalizes_complete_part(self):
        """finalize 后、rename 前崩溃：留了整轨 .part，启动时直接收尾不重抓。"""
        disc, cache, expected = self._setup()
        cache.part_path(disc, 1).write_bytes(b"\x00" * expected)
        cache.repair_incomplete(disc)
        self.assertTrue(cache.is_done(disc, 1))
        self.assertEqual(cache.pcm_path(disc, 1).stat().st_size, expected)
        self.assertFalse(cache.part_path(disc, 1).exists())

    def test_repair_finalizes_oversized_part(self):
        disc, cache, expected = self._setup()
        cache.part_path(disc, 1).write_bytes(b"\x00" * (expected + 100))
        cache.repair_incomplete(disc)
        self.assertTrue(cache.is_done(disc, 1))
        self.assertEqual(cache.pcm_path(disc, 1).stat().st_size, expected)

    def test_repair_drops_incomplete_part(self):
        disc, cache, expected = self._setup()
        cache.part_path(disc, 1).write_bytes(b"\x00" * (expected // 2))
        cache.repair_incomplete(disc)
        self.assertFalse(cache.part_path(disc, 1).exists())
        self.assertFalse(cache.is_done(disc, 1))

    def test_ripper_accepts_exact_pcm_without_marker(self):
        """rename 后、写 .done 前崩溃：大小对了就当完成，不重抓整轨。"""
        disc, cache, expected = self._setup()
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * expected)
        ripper = FakeRipper(disc, cache, speed=1e9)
        self.assertTrue(ripper.is_done(1))
        self.assertFalse(cache.is_done(disc, 1))

    def test_meta_roundtrip(self):
        disc = build_fake_disc()
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            cache.write_meta(disc, {"album": "测试专辑", "artist": "无名",
                                    "tracks": {"1": "第一首"}})
            title, artist = cache.title_of(disc, 1)
            self.assertEqual((title, artist), ("第一首", "无名"))


class TestMetadata(unittest.TestCase):
    def test_disc_id_matches_musicbrainz_example(self):
        """MB 文档里的样例盘：算出来必须正好是 49HHV7Eb8UKF3aQiNmu1GR8vKTY-。"""
        from services.metadata import disc_id
        starts = [0, 15213, 32164, 46442, 63264, 80339]
        ends = starts[1:] + [95312]
        tracks = [Track(i + 1, s, e - s) for i, (s, e) in enumerate(zip(starts, ends))]
        disc = Disc(device="/dev/sr0", tracks=tracks, leadout_lba=95312)
        self.assertEqual(disc_id(disc), "49HHV7Eb8UKF3aQiNmu1GR8vKTY-")

    def test_fetch_exact_hit_does_not_fall_back(self):
        from services.metadata import disc_id, fetch
        disc = build_fake_disc(durations=(2.0, 3.0))
        did = disc_id(disc)
        data = {"releases": [{
            "id": "r", "title": "精确命中", "status": "Official",
            "artist-credit": [{"name": "歌手"}],
            "media": [{"discs": [{"id": did}], "tracks": [
                {"number": "1", "title": "一", "length": 2000},
                {"number": "2", "title": "二", "length": 3000}]}]}]}
        with mock.patch("services.metadata._fetch_json", return_value=data) as fetch_json:
            got = fetch(disc)
        self.assertEqual(fetch_json.call_count, 1)
        assert got is not None
        self.assertEqual(got["album"], "精确命中")
        self.assertEqual(got["match"], "discid")

    def test_fetch_falls_back_to_toc_on_404(self):
        """刻录盘精确 ID 404：回退 TOC 查询，逐轨时长对得上才采用。"""
        from services.metadata import fetch
        disc = build_fake_disc(durations=(2.0, 3.0))
        toc_data = {"releases": [{
            "id": "r", "title": "刻录盘原版", "status": "Official",
            "artist-credit": [{"name": "歌手"}],
            "media": [{"discs": [], "tracks": [
                {"number": "1", "title": "一", "length": 2000},
                {"number": "2", "title": "二", "length": 3000}]}]}]}
        err = urllib.error.HTTPError("http://mb", 404, "Not Found", {}, None)
        with mock.patch("services.metadata._fetch_json",
                        side_effect=[err, toc_data]) as fetch_json:
            got = fetch(disc)
        self.assertEqual(fetch_json.call_count, 2)
        # 限速挪进了 _fetch_json 的请求入口（见 TestMetadataThrottle），
        # 回退路径不再自己 sleep，避免两处叠加成 2 秒。
        assert got is not None
        self.assertEqual(got["album"], "刻录盘原版")
        self.assertEqual(got["tracks"], {"1": "一", "2": "二"})
        self.assertEqual(got["match"], "toc")

    def _toc_data(self, lengths, title="某专辑", status="Official"):
        return {"releases": [{
            "id": "r", "title": title, "status": status,
            "artist-credit": [{"name": "歌手"}],
            "media": [{"discs": [], "tracks": [
                {"number": str(i + 1), "title": f"曲{i + 1}", "length": length}
                for i, length in enumerate(lengths)]}]}]}

    def test_toc_fallback_rejects_same_count_wrong_durations(self):
        """轨数相同但时长对不上：宁可不显示曲名，也不能配到别的专辑。"""
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        self.assertIsNone(_pick_toc(self._toc_data([8000, 9000]), disc))

    def test_toc_fallback_requires_lengths(self):
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        data = self._toc_data([2000, 3000])
        del data["releases"][0]["media"][0]["tracks"][0]["length"]
        self.assertIsNone(_pick_toc(data, disc))

    def test_toc_fallback_accepts_rounded_lengths(self):
        """MB 时长按秒取整：±1s 内的误差要能接受。"""
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        got = _pick_toc(self._toc_data([2900, 2100]), disc)   # 与 3s/2s 的误差在 1s 内
        assert got is not None
        self.assertEqual(got["tracks"], {"1": "曲1", "2": "曲2"})

    @staticmethod
    def _release(title, lengths, status="Official", prefix="曲", rid=None):
        return {
            "id": rid or title, "title": title, "status": status,
            "artist-credit": [{"name": "歌手"}],
            "media": [{"discs": [], "tracks": [
                {"number": str(i + 1), "title": f"{prefix}{i + 1}",
                 "length": length}
                for i, length in enumerate(lengths)]}],
        }

    def test_toc_fallback_prefers_closer_durations(self):
        """两个候选都过容差时，按误差选更接近的那张，而不是 API 顺序。"""
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        data = {"releases": [
            self._release("误差大的版本", [2800, 3800]),
            self._release("更接近的版本", [2050, 3050]),
        ]}
        got = _pick_toc(data, disc)
        assert got is not None
        self.assertEqual(got["album"], "更接近的版本")

    def test_toc_fallback_returns_none_for_ambiguous_titles(self):
        """不同曲目的候选误差难分伯仲：宁可 Track NN，也不赌一个。"""
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        data = {"releases": [
            self._release("版本 A", [2100, 3100], prefix="A"),
            self._release("版本 B", [2200, 3200], prefix="B"),
        ]}
        self.assertIsNone(_pick_toc(data, disc))

    def test_toc_fallback_same_titles_are_not_ambiguous(self):
        """同曲目的不同压制版不算歧义，正常取误差更小的。"""
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        data = {"releases": [
            self._release("版本 A", [2100, 3100]),
            self._release("版本 B", [2200, 3200]),
        ]}
        got = _pick_toc(data, disc)
        assert got is not None
        self.assertEqual(got["album"], "版本 A")
        self.assertEqual(got["tracks"], {"1": "曲1", "2": "曲2"})

    def test_toc_fallback_punctuation_variants_are_not_ambiguous(self):
        """曲名只差弯引号/全半角时是同一首，不能当歧义丢掉。"""
        from services.metadata import _pick_toc
        disc = build_fake_disc(durations=(2.0, 3.0))
        data = {"releases": [
            self._release("版本 A", [2100, 3100], prefix="I Can’t Tell You Why "),
            self._release("版本 B", [2100, 3100], prefix="I Can't Tell You Why "),
        ]}
        self.assertIsNotNone(_pick_toc(data, disc))

    def test_toc_result_rechecked_only_after_age(self):
        from services.metadata import toc_result_needs_recheck
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        meta = {"match": "toc", "updated": stamp}
        self.assertFalse(toc_result_needs_recheck(meta, now=now))
        self.assertTrue(toc_result_needs_recheck(meta, now=now + 31 * 86400))
        self.assertFalse(toc_result_needs_recheck({"updated": stamp},
                                                  now=now + 31 * 86400))
        self.assertTrue(toc_result_needs_recheck({"match": "toc"}, now=now))

    def test_pick_uses_disc_id_medium_and_prefers_official(self):
        """同一 disc ID 多版本时，只认 disc ID 对得上的那张媒介，官方版优先。"""
        from services.metadata import _pick
        did = "abc-"
        data = {"releases": [
            {"id": "r1", "title": "盗版拼盘", "status": "Bootleg",
             "artist-credit": [{"name": "无名"}],
             "media": [{"discs": [{"id": did}], "tracks": [
                 {"number": "1", "title": "A"}, {"number": "2", "title": "B"}]}]},
            {"id": "r2", "title": "正式版", "status": "Official",
             "artist-credit": [{"name": "歌手"}, {"name": "feat."}],
             "media": [
                 {"discs": [{"id": "别张盘"}], "tracks": [{"number": "1", "title": "错"}]},
                 {"discs": [{"id": did}], "tracks": [
                     {"number": "1", "title": "第一"}, {"number": "2", "title": "第二"}]},
             ]},
        ]}
        got = _pick(data, did, 2)
        assert got is not None
        self.assertEqual(got["album"], "正式版")
        self.assertEqual(got["release_id"], "r2")
        self.assertEqual(got["tracks"], {"1": "第一", "2": "第二"})

    def test_medium_without_disc_id_does_not_beat_real_match(self):
        """多碟 release：前一张没有 disc 信息，也不能抢在真正命中的那张前面。"""
        from services.metadata import _pick
        did = "abc-"
        data = {"releases": [{
            "id": "r", "title": "双碟版", "status": "Official",
            "artist-credit": [{"name": "艺人"}],
            "media": [
                {"discs": [], "tracks": [{"number": "1", "title": "错1"},
                                         {"number": "2", "title": "错2"}]},
                {"discs": [{"id": did}], "tracks": [{"number": "1", "title": "对1"},
                                                    {"number": "2", "title": "对2"}]},
            ]}]}
        got = _pick(data, did, 2)
        assert got is not None
        self.assertEqual(got["tracks"], {"1": "对1", "2": "对2"})

    def test_release_with_disc_info_elsewhere_does_not_fall_back(self):
        """目标 disc ID 在另一张轨数不符的 medium 上：不能猜没 disc ID 的那张。"""
        from services.metadata import _pick
        rel = {"media": [
            {"discs": [], "tracks": [{"number": "1", "title": "错1"},
                                     {"number": "2", "title": "错2"}]},
            {"discs": [{"id": "target"}],
             "tracks": [{"number": "1", "title": "对1"},
                        {"number": "2", "title": "对2"},
                        {"number": "3", "title": "对3"}]},
        ]}
        self.assertIsNone(_pick({"releases": [rel]}, "target", 2))

    def test_release_with_other_disc_ids_is_not_used(self):
        from services.metadata import _pick
        data = {"releases": [{
            "id": "r", "title": "别的碟", "status": "Official",
            "artist-credit": [{"name": "艺人"}],
            "media": [{"discs": [{"id": "other"}],
                       "tracks": [{"number": "1", "title": "错"}]}]}]}
        self.assertIsNone(_pick(data, "abc-", 1))


class TestMetaVersion(unittest.TestCase):
    def test_stale_meta_is_ignored_and_write_stamps_version(self):
        """旧版算法写下的 meta.json 不再可信，要当没有、让后台重查。"""
        disc = build_fake_disc()
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            path = cache.meta_path(disc)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"album": "别人的专辑", "tracks": {"1": "错曲名"}}',
                            encoding="utf-8")
            self.assertEqual(cache.read_meta(disc), {})
            self.assertEqual(cache.title_of(disc, 1)[0], "Track 01")

            cache.write_meta(disc, {"album": "新专辑", "tracks": {"1": "新曲名"}})
            self.assertEqual(cache.read_meta(disc)["meta_version"], META_VERSION)
            self.assertEqual(cache.title_of(disc, 1), ("新曲名", "新专辑"))


class TestCdText(unittest.TestCase):
    """CD-TEXT pack 解析：不碰光驱，用手搓的 18 字节 pack。"""

    @staticmethod
    def _pack(ptype: int, track: int, seq: int, text: bytes = b"",
              wide: bool = False) -> bytes:
        body = text[:12].ljust(12, b"\x00")
        head = bytes([ptype, track, seq, 0x80 if wide else 0x00])   # block 0
        return head + body + b"\x00\x00"

    def _basic(self) -> bytes:
        return b"".join([
            self._pack(0x8F, 0, 0, bytes([0x00, 1, 2])),   # charset 0 / 轨 1-2
            self._pack(0x80, 0, 1, b"Album"),
            self._pack(0x81, 0, 2, b"Artist"),
            self._pack(0x80, 1, 3, b"One"),
            self._pack(0x80, 2, 4, b"Two"),
        ])

    def test_parse_basic(self):
        from cdda.cdtext import parse
        got = parse(self._basic())
        self.assertEqual(got["album"], "Album")
        self.assertEqual(got["artist"], "Artist")
        self.assertEqual(got["tracks"], {"1": "One", "2": "Two"})
        self.assertEqual(got["source"], "cdtext")

    def test_empty_payload(self):
        from cdda.cdtext import parse
        self.assertIsNone(parse(b""))
        self.assertIsNone(parse(b"\x00" * 18))

    def test_shift_jis_titles(self):
        from cdda.cdtext import parse
        packs = b"".join([
            self._pack(0x8F, 0, 0, bytes([0x80, 1, 1])),
            self._pack(0x80, 0, 1, "ワガママ".encode("shift_jis")),
            self._pack(0x80, 1, 2, "始まり".encode("shift_jis")),
        ])
        got = parse(packs)
        self.assertEqual(got["album"], "ワガママ")
        self.assertEqual(got["tracks"]["1"], "始まり")

    def test_text_split_across_packs(self):
        from cdda.cdtext import parse
        packs = b"".join([
            self._pack(0x8F, 0, 0, bytes([0x00, 1, 1])),
            self._pack(0x80, 0, 1, b"A" * 12),             # 没有 NUL，跨到下一包
            self._pack(0x80, 0, 2, b"A" * 6 + b"\x00"),
        ])
        self.assertEqual(parse(packs)["album"], "A" * 18)


class TestTocSignature(unittest.TestCase):
    def test_disc_signature(self):
        from cdda.device import disc_signature
        disc = build_fake_disc(durations=(1.0, 2.0))
        self.assertEqual(disc_signature(disc), (((1, 0), (2, 75)), 225))

    def test_bad_device_is_not_a_match(self):
        from cdda.device import read_toc_signature, toc_matches
        self.assertIsNone(read_toc_signature("/dev/does-not-exist"))
        self.assertFalse(toc_matches("/dev/does-not-exist", build_fake_disc()))


class TestMetaMerge(unittest.TestCase):
    def test_musicbrainz_overrides_cdtext(self):
        from cdda.session import merge_meta
        base = {"album": "盘上专辑", "artist": "盘上艺人",
                "tracks": {"1": "盘上1", "2": "盘上2"}, "sources": ["cdtext"]}
        got = merge_meta(base, {"album": "MB 专辑", "artist": "",
                                "tracks": {"1": "MB1"}, "release_id": "r",
                                "source": "musicbrainz"})
        self.assertEqual(got["album"], "MB 专辑")
        self.assertEqual(got["artist"], "盘上艺人")
        self.assertEqual(got["tracks"], {"1": "MB1", "2": "盘上2"})
        self.assertEqual(got["sources"], ["cdtext", "musicbrainz"])

    def test_toc_match_and_disc_id_survive_merge(self):
        """match/disc_id 也要落盘，否则下次无法区分精确命中与 TOC 推断。"""
        from cdda.session import merge_meta
        got = merge_meta({}, {"album": "专辑", "tracks": {"1": "曲"},
                              "source": "musicbrainz", "match": "toc",
                              "disc_id": "did-"})
        self.assertEqual(got["match"], "toc")
        self.assertEqual(got["disc_id"], "did-")
        self.assertEqual(got["sources"], ["musicbrainz"])
        # 重新核验成精确命中时要能覆盖旧的 toc 标记
        self.assertEqual(merge_meta({"match": "toc"}, {"match": "discid"})["match"],
                         "discid")


class TestMetadataThrottle(unittest.TestCase):
    """P1②：限流放在请求入口，快速换盘时重叠的后台查询也要排队。"""

    def test_requests_are_spaced(self):
        import services.metadata as md
        now = [100.0]
        slept: list[float] = []

        def fake_sleep(sec: float) -> None:
            slept.append(sec)
            now[0] += sec

        with mock.patch.object(md.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(md.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(md, "_last_request_at", 0.0):
            md._wait_for_slot()
            md._wait_for_slot()

        self.assertEqual(len(slept), 1, "第二次请求必须等一个间隔")
        self.assertGreaterEqual(slept[0], 0.9)

    def test_every_request_goes_through_throttle(self):
        import services.metadata as md

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, *args):
                return b"{}"

        with mock.patch.object(md, "_wait_for_slot") as slot, \
             mock.patch.object(md.urllib.request, "urlopen", return_value=_Resp()):
            md._fetch_json("http://example.invalid", 1.0)
            md._fetch_json("http://example.invalid", 1.0)
        self.assertEqual(slot.call_count, 2)


class TestMetadataTocPersist(unittest.TestCase):
    def test_toc_match_persists_to_meta_json(self):
        """从 fetch 的回退结果一路写到 cache，再读回来仍带 match=toc。"""
        from types import SimpleNamespace

        from app import App

        disc = build_fake_disc(durations=(2.0, 3.0))
        toc = {"releases": [{
            "id": "r", "title": "刻录盘原版", "status": "Official",
            "artist-credit": [{"name": "歌手"}],
            "media": [{"discs": [], "tracks": [
                {"number": "1", "title": "一", "length": 2000},
                {"number": "2", "title": "二", "length": 3000}]}]}]}
        err = urllib.error.HTTPError("http://mb", 404, "Not Found", {}, None)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            app = App.__new__(App)
            app.disc, app.cache = disc, cache
            app.args = SimpleNamespace(no_metadata=False)
            app._disc_seq = 0
            with mock.patch("services.metadata._fetch_json",
                            side_effect=[err, toc]), \
                 mock.patch("services.metadata.time.sleep"):
                app._start_metadata_lookup()
                deadline = time.monotonic() + 3
                meta: dict = {}
                while time.monotonic() < deadline:
                    meta = cache.read_meta(disc)
                    if meta.get("tracks"):
                        break
                    time.sleep(0.01)
            self.assertEqual(meta.get("match"), "toc")
            self.assertEqual(meta.get("disc_id") is not None, True)
            self.assertEqual(meta.get("sources"), ["musicbrainz"])
            self.assertEqual(cache.title_of(disc, 1), ("一", "歌手"))

    def test_stale_toc_meta_triggers_requery(self):
        """TOC 推断的缓存超过 30 天，启动时要重新联网核验一次。"""
        from types import SimpleNamespace

        from app import App

        old = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() - 31 * 86400))

        class _Cache:
            def read_meta(self, disc):
                return {"match": "toc", "updated": old,
                        "tracks": {"1": "旧"}, "sources": ["musicbrainz"]}

            def write_meta(self, disc, data):
                raise AssertionError("没有新结果时不该写 meta")

        called = threading.Event()
        app = App.__new__(App)
        app.disc = build_fake_disc(durations=(1.0,))
        app.cache = _Cache()
        app.args = SimpleNamespace(no_metadata=False)
        app._disc_seq = 0
        with mock.patch("app.metadata.fetch",
                        side_effect=lambda disc: called.set() or None):
            app._start_metadata_lookup()
            self.assertTrue(called.wait(2.0))


class TestCdTextLookup(unittest.TestCase):
    def test_no_metadata_still_reads_cdtext(self):
        """--no-metadata 只是不联网；盘上有 CD-TEXT 还是要读。"""
        from types import SimpleNamespace

        from app import App

        class _Cache:
            def __init__(self):
                self.written: list[dict] = []

            def read_meta(self, disc):
                return {}

            def write_meta(self, disc, data):
                self.written.append(data)

        app = App.__new__(App)
        app.args = SimpleNamespace(no_metadata=True)
        app.disc, app.cache = build_fake_disc(durations=(1.0,)), _Cache()

        text = {"album": "盘上专辑", "artist": "盘上艺人",
                "tracks": {"1": "盘上1"}, "source": "cdtext"}
        with mock.patch("app.cdtext.read", return_value=text), \
             mock.patch("app.toc_matches", return_value=True):
            app._start_metadata_lookup()
            deadline = time.monotonic() + 3
            while not app.cache.written and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertTrue(app.cache.written)
        self.assertEqual(app.cache.written[0]["album"], "盘上专辑")
        self.assertEqual(app.cache.written[0]["sources"], ["cdtext"])


class TestCdTextRace(unittest.TestCase):
    """换碟竞态：CD-TEXT 读的是「设备当前那张盘」，不能写到上一张盘名下。"""

    def _app(self):
        from types import SimpleNamespace

        from app import App

        class _Cache:
            def __init__(self):
                self.written: list[dict] = []

            def read_meta(self, disc):
                return {}

            def write_meta(self, disc, data):
                self.written.append(data)

        app = App.__new__(App)
        app.args = SimpleNamespace(no_metadata=False)
        app.disc = build_fake_disc(durations=(1.0,), device="/dev/sr-fake-a")
        app.cache = _Cache()
        app._disc_seq = 1
        return app

    def test_swap_during_fetch_skips_cdtext(self):
        app = self._app()
        started, release = threading.Event(), threading.Event()

        def slow_fetch(disc):
            started.set()
            release.wait(2)
            return None

        with mock.patch("app.metadata.fetch", side_effect=slow_fetch), \
             mock.patch("app.cdtext.read") as read:
            app._start_metadata_lookup()
            self.assertTrue(started.wait(2))
            app.disc = build_fake_disc(durations=(2.0,), device="/dev/sr-fake-b")
            app._disc_seq += 1
            release.set()
            time.sleep(0.4)

        read.assert_not_called()
        self.assertEqual(app.cache.written, [])

    def test_toc_mismatch_discards_cdtext(self):
        app = self._app()
        text = {"album": "B 的专辑", "tracks": {"1": "B 曲"}, "source": "cdtext"}
        with mock.patch("app.metadata.fetch", return_value=None), \
             mock.patch("app.cdtext.read", return_value=text), \
             mock.patch("app.toc_matches", return_value=False):
            app._start_metadata_lookup()
            time.sleep(0.4)

        self.assertEqual(app.cache.written, [], "TOC 对不上时不能把 B 的标签写进 A")

    def test_toc_match_writes_cdtext(self):
        app = self._app()
        text = {"album": "A 的专辑", "tracks": {"1": "A 曲"}, "source": "cdtext"}
        with mock.patch("app.metadata.fetch", return_value=None), \
             mock.patch("app.cdtext.read", return_value=text), \
             mock.patch("app.toc_matches", return_value=True):
            app._start_metadata_lookup()
            deadline = time.monotonic() + 3
            while not app.cache.written and time.monotonic() < deadline:
                time.sleep(0.01)

        self.assertTrue(app.cache.written)
        self.assertEqual(app.cache.written[0]["album"], "A 的专辑")


class TestRipCommand(unittest.TestCase):
    def test_uses_little_endian_raw(self):
        from cdda.ripper import Ripper
        disc = build_fake_disc()
        r = Ripper(disc, Cache("/tmp/asciicd-test-cache"), "/dev/sr0")
        cmd = r._cmd(3)
        self.assertIn("-r", cmd)          # raw little-endian
        self.assertNotIn("-p", cmd)       # -p 在本机会给大端 → 沙沙声
        self.assertIn("-z5", cmd)         # 不能是 "-z" "5"（会被当成 span）
        self.assertEqual(cmd[-2:], ["3", "-"])


class TestRipPhase(unittest.TestCase):
    """cdparanoia 进度行 → status['phase']；热阶段保留相位并计数。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.disc = build_fake_disc(durations=(1.0,))
        self.ripper = Ripper(self.disc, Cache(self._tmp.name), "/dev/sr0")

    def tearDown(self):
        self._tmp.cleanup()

    def _phase(self) -> str:
        return self.ripper.snapshot()["phase"]

    def test_normal_phases(self):
        for line, want in ((b"##: 0 [read] @ 0", "read"),
                           (b"##: 588 [verify] @ 2352", "verify"),
                           (b"##: 1176 [overlap] @ 4704", "overlap")):
            self.ripper._score(line)
            self.assertEqual(self._phase(), want)
        self.ripper._score(b"##: 1764 [wrote] @ 7056")
        self.assertEqual(self._phase(), "")
        self.ripper._score(b"##: 0 [read] @ 0")
        self.ripper._score(b"##: 0 [finished] @ 0")
        self.assertEqual(self._phase(), "")

    def test_hot_stage_keeps_phase_and_counts(self):
        self.ripper._begin_attempt(1)
        self.ripper._score(b"##: 0 [read] @ 0")
        self.ripper._score(b"##: 0 [scratch] @ 17640")
        st = self.ripper.snapshot()
        self.assertEqual(st["phase"], "read")
        self.assertEqual(st["errors"]["!"], 1)
        self.assertEqual(st["smile"], "scratch")
        self.assertEqual(len(st["map"]), 20)
        self.assertEqual(st["map"][2], 1, "坏道事件应落到 5% 区间的 map 格")

    def test_begin_attempt_clears_phase_and_errors(self):
        self.ripper._score(b"##: 0 [verify] @ 0")
        self.ripper._begin_attempt(1)
        st = self.ripper.snapshot()
        self.assertEqual(st["phase"], "")
        self.assertEqual(st["stage"], "ripping")
        self.assertEqual(st["errors"], {"V": 0, "!": 0, "e": 0, "+": 0})
        self.assertEqual(st["map"], [0] * 20)


class TestRetryAccounting(unittest.TestCase):
    """P1①：一次失败只能记一次；连续 RIP_ATTEMPTS 次后放弃；retry 数是真实失败次数。"""

    class FlakyRipper(RipperBase):
        """前 fail_times 次 _rip_one 抛异常，之后把整轨写成有效缓存。"""

        def __init__(self, disc, cache, fail_times=10**9):
            super().__init__(disc, cache, name="test-ripper")
            self.fail_times = fail_times
            self.calls = 0
            self.retry_delay = 0.0

        def _rip_one(self, number: int) -> None:
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError(f"boom {self.calls}")
            expected = self.disc.track(number).pcm_bytes
            self.cache.pcm_path(self.disc, number).write_bytes(b"\x00" * expected)
            self.cache.mark_done(self.disc, number)

    @staticmethod
    def wait_for(cond, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.01)
        return False

    def make_ripper(self, fail_times):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        disc = build_fake_disc(durations=(1.0,))
        cache = Cache(tmp.name)
        ripper = self.FlakyRipper(disc, cache, fail_times)
        self.addCleanup(lambda: (ripper.stop(), ripper.join(timeout=5)))
        return disc, cache, ripper

    def test_counts_once_per_failure_and_gives_up(self):
        disc, cache, ripper = self.make_ripper(fail_times=10**9)
        ripper.start()
        self.assertTrue(self.wait_for(lambda: ripper.is_failed(1)), "连续失败后应标记 failed")
        # 旧代码 _rip_one 和 run() 各加一次，3 次重试实际只跑 2 次
        self.assertEqual(ripper.calls, RIP_ATTEMPTS)
        self.assertEqual(ripper.attempts_of(1), RIP_ATTEMPTS)
        self.assertEqual(int(ripper.snapshot()["errors"]["e"]), RIP_ATTEMPTS)
        self.assertIn("boom", ripper.failure_reason(1))
        self.assertFalse(cache.is_done(disc, 1))

    def test_retry_succeeds_before_limit(self):
        disc, cache, ripper = self.make_ripper(fail_times=2)
        ripper.start()
        self.assertTrue(self.wait_for(lambda: cache.is_done(disc, 1)))
        self.assertEqual(ripper.calls, 3)
        self.assertFalse(ripper.is_failed(1))
        self.assertEqual(cache.pcm_path(disc, 1).stat().st_size,
                         disc.track(1).pcm_bytes)

    def test_manual_retry_clears_failure_state(self):
        _disc, _cache, ripper = self.make_ripper(fail_times=10**9)
        ripper.start()
        self.assertTrue(self.wait_for(lambda: ripper.is_failed(1)))
        ripper.stop()
        ripper.join(timeout=5)
        # 放弃后 run() 会把 track 置 None 但 e/msg 还留着：retry 也要清掉
        ripper._update(track=None, stage="idle")
        with ripper._lock:
            ripper.status["errors"]["e"] = RIP_ATTEMPTS
            ripper.status["msg"] = "失败 3/3"
        ripper.retry(1)
        self.assertFalse(ripper.is_failed(1))
        self.assertEqual(ripper.attempts_of(1), 0)
        self.assertEqual(ripper.failure_reason(1), "")
        snap = ripper.snapshot()
        self.assertEqual(int(snap["errors"]["e"]), 0)
        self.assertEqual(snap["msg"], "")


class TestRealRipperRetryCounting(unittest.TestCase):
    """P1① 真路径：Ripper._rip_one 自己不再计数，一次失败只记一次。

    旧代码 _rip_one 和 run() 各 +1，3 次重试实际只会跑 2 次 —— 这里用假的
    cdparanoia（写一点 PCM 然后退出 1）驱动真实 Ripper._rip_one 验证。
    """

    class ExitingRipper(Ripper):
        def __init__(self, disc, cache, script):
            super().__init__(disc, cache, "/dev/null")
            self.script = script
            self.cmd_calls = 0
            self.retry_delay = 0.0

        def _cmd(self, number: int) -> list[str]:
            self.cmd_calls += 1
            return [sys.executable, self.script]

    def test_rip_one_failure_counted_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        script = os.path.join(tmp.name, "fake_cdparanoia.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("import sys\n"
                     "sys.stdout.buffer.write(b'\\x00' * 4000)\n"
                     "sys.stdout.buffer.flush()\n"
                     "sys.stderr.write('##: 0 [read] @ 0\\n')\n"
                     "sys.exit(1)\n")
        disc = build_fake_disc(durations=(1.0,))
        cache = Cache(os.path.join(tmp.name, "cache"))
        ripper = self.ExitingRipper(disc, cache, script)
        ripper.start()
        try:
            self.assertTrue(TestRetryAccounting.wait_for(lambda: ripper.is_failed(1)))
        finally:
            ripper.stop()
            ripper.join(timeout=5)
        self.assertEqual(ripper.cmd_calls, RIP_ATTEMPTS)   # 旧代码只会跑 2 次
        self.assertEqual(ripper.attempts_of(1), RIP_ATTEMPTS)
        self.assertIn("cdparanoia", ripper.failure_reason(1))


class TestRipOneExceptionCleanup(unittest.TestCase):
    """_rip_one 在 os.write 等异常路径上也必须杀掉子进程，否则 cdparanoia 变孤儿。"""

    class WriteFailRipper(Ripper):
        def __init__(self, disc, cache, script):
            super().__init__(disc, cache, "/dev/null")
            self.script = script
            self.retry_delay = 0.0

        def _cmd(self, number: int) -> list[str]:
            return [sys.executable, self.script]

    @staticmethod
    def _kill_quiet(pid: int) -> None:
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    def test_child_is_killed_when_write_raises(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pid_file = os.path.join(tmp.name, "child.pid")
        script = os.path.join(tmp.name, "slow_cdparanoia.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("import os, sys, time\n"
                     f"with open({pid_file!r}, 'w') as fh:\n"
                     "    fh.write(str(os.getpid()))\n"
                     "sys.stdout.buffer.write(b'\\x00' * 4000)\n"
                     "sys.stdout.buffer.flush()\n"
                     "time.sleep(30)\n")
        disc = build_fake_disc(durations=(1.0,))
        cache = Cache(os.path.join(tmp.name, "cache"))
        ripper = self.WriteFailRipper(disc, cache, script)

        def boom(fd: int, data: bytes) -> int:
            raise OSError(28, "No space left on device")

        with mock.patch("cdda.ripper.os.write", side_effect=boom):
            ripper.start()
            try:
                self.assertTrue(TestRetryAccounting.wait_for(
                    lambda: ripper.is_failed(1), timeout=15))
            finally:
                ripper.stop()
                ripper.join(timeout=10)

        with open(pid_file, encoding="utf-8") as fh:
            pid = int(fh.read())
        self.addCleanup(self._kill_quiet, pid)
        # 进程已被 wait 回收时 kill(pid, 0) 抛 ProcessLookupError；泄漏则不会抛
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class TestRipperThreadLifecycle(unittest.TestCase):
    """P1① 顺带：停止标志不能叫 _stop，否则 Thread.join() 调 self._stop() 会 TypeError。"""

    class SleepyRipper(RipperBase):
        def __init__(self, disc, cache):
            super().__init__(disc, cache, name="sleepy")

        def _rip_one(self, number: int) -> None:
            time.sleep(0.02)          # 永远抓不完，也不会失败

    def test_stop_then_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0,))
            ripper = self.SleepyRipper(disc, Cache(tmp))
            ripper.start()
            time.sleep(0.05)
            ripper.stop()
            ripper.join(timeout=5)    # 旧代码在这里抛 TypeError: 'Event' object is not callable
            self.assertFalse(ripper.is_alive())


class TestFakeRipper(unittest.TestCase):
    def test_rips_exact_pcm(self):
        disc = build_fake_disc(durations=(1.0,))
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(tmp)
            ripper = FakeRipper(disc, cache, speed=1e9)
            ripper.start()
            try:
                self.assertTrue(TestRetryAccounting.wait_for(lambda: cache.is_done(disc, 1)))
            finally:
                ripper.stop()
                ripper.join(timeout=5)
            self.assertEqual(cache.pcm_path(disc, 1).stat().st_size,
                             disc.track(1).pcm_bytes)
            self.assertTrue(cache.done_path(disc, 1).exists())
            self.assertFalse(ripper.is_failed(1))


class TestRipModeWindow(unittest.TestCase):
    """RIP_MODE 决定写盘范围：disc=整盘 / track=只本轨 / next=本轨+下一轨。"""

    class Picker(RipperBase):
        def __init__(self, disc, cache, mode):
            super().__init__(disc, cache, mode=mode)

        def _rip_one(self, number: int) -> None:      # pragma: no cover - 不跑线程
            pass

    def _picker(self, mode):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        disc = build_fake_disc(durations=(1.0, 1.0, 1.0, 1.0))
        ripper = self.Picker(disc, Cache(tmp.name), mode)
        done: set[int] = set()
        ripper.is_done = lambda number: number in done       # type: ignore[method-assign]
        return ripper, done

    def test_track_mode_only_current(self):
        ripper, done = self._picker("track")
        ripper.want(2)
        self.assertEqual(ripper._pick(), 2)
        done.add(2)
        self.assertIsNone(ripper._pick(), "只抓本轨，播完之前不许碰别的轨")

    def test_next_mode_current_then_one_ahead(self):
        ripper, done = self._picker("next")
        ripper.want(1)
        self.assertEqual(ripper._pick(), 1)
        done.add(1)
        self.assertEqual(ripper._pick(), 2)
        done.add(2)
        self.assertIsNone(ripper._pick(), "只预取一轨，不该把整盘抓完")

    def test_next_mode_window_follows_playback(self):
        ripper, done = self._picker("next")
        done.update({1, 2})
        ripper.want(2)
        self.assertEqual(ripper._pick(), 3)

    def test_next_mode_last_track_has_no_prefetch(self):
        ripper, done = self._picker("next")
        ripper.want(4)
        self.assertEqual(ripper._pick(), 4)
        done.add(4)
        self.assertIsNone(ripper._pick())

    def test_disc_mode_rips_everything(self):
        ripper, done = self._picker("disc")
        done.update({1, 2})
        ripper.want(1)
        self.assertEqual(ripper._pick(), 3)
        done.update({3, 4})
        self.assertIsNone(ripper._pick())

    def test_disc_mode_wraps_to_earlier_tracks(self):
        ripper, done = self._picker("disc")
        done.update({2, 3, 4})
        ripper.want(3)
        self.assertEqual(ripper._pick(), 1, "整盘模式下回头补前面没抓的轨")

    def test_no_priority_starts_from_first(self):
        ripper, _done = self._picker("track")
        self.assertEqual(ripper._pick(), 1, "还没 want() 时从第一轨开始")

    def test_invalid_mode_falls_back_to_config(self):
        ripper, _done = self._picker("nonsense")
        self.assertEqual(ripper.mode, RIP_MODE)


class TestFailedTrackDoesNotThrash(unittest.TestCase):
    """当前轨认输后别再把它「抢回来」——否则下一轨会起来就被打断，空转光驱。"""

    def test_next_mode_finishes_prefetch_after_giving_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(0.5, 0.5, 0.5))
            cache = Cache(tmp)
            ripper = FakeRipper(disc, cache, speed=1e9, mode="next")
            ripper._failed.add(1)        # 第 1 轨已放弃，播放器还停在它上面
            ripper.want(1)
            ripper.start()
            try:
                self.assertTrue(
                    TestRetryAccounting.wait_for(lambda: cache.is_done(disc, 2)),
                    "第 1 轨放弃后，第 2 轨的预抓不该被立刻抢占打断")
            finally:
                ripper.stop()
                ripper.join(timeout=5)
            self.assertFalse(cache.is_done(disc, 1))


class _StubRipper:
    """只给 TrackMedia 用的抓轨器替身（不跑线程）。"""

    def __init__(self, done: bool = False, failed: bool = False, reason: str = ""):
        self.done = done
        self.failed = failed
        self.reason = reason

    def is_done(self, number: int) -> bool:
        return self.done

    def is_failed(self, number: int) -> bool:
        return self.failed

    def failure_reason(self, number: int) -> str:
        return self.reason


class TestMediaFailure(unittest.TestCase):
    """P1②：抓轨永久失败要抛 MediaFailed，不能被 engine 当正常 EOF 静默跳轨。"""

    def _disc_cache(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        disc = build_fake_disc(durations=(1.0,))
        cache = Cache(tmp.name)
        cache.ensure_dir(disc)
        return disc, cache

    def test_failed_raises_media_failed(self):
        disc, cache = self._disc_cache()
        media = TrackMedia(cache, disc, disc.track(1),
                           _StubRipper(failed=True, reason="cdparanoia rc=1"))
        with self.assertRaises(MediaFailed) as ctx:
            media.read(0, 4096, threading.Event())
        self.assertIn("抓轨失败", str(ctx.exception))
        self.assertIn("cdparanoia", str(ctx.exception))

    def test_done_returns_empty_for_eof(self):
        disc, cache = self._disc_cache()
        expected = disc.track(1).pcm_bytes
        cache.pcm_path(disc, 1).write_bytes(b"\x00" * expected)
        media = TrackMedia(cache, disc, disc.track(1), _StubRipper(done=True))
        self.assertEqual(media.read(expected, 4096, threading.Event()), b"")

    def test_partial_data_served_before_failure(self):
        disc, cache = self._disc_cache()
        cache.part_path(disc, 1).write_bytes(b"\x01" * 100)
        media = TrackMedia(cache, disc, disc.track(1),
                           _StubRipper(failed=True, reason="boom"))
        self.assertEqual(media.read(0, 4096, threading.Event()), b"\x01" * 100)
        with self.assertRaises(MediaFailed):
            media.read(100, 4096, threading.Event())

    def test_stalled_raises(self):
        disc, cache = self._disc_cache()
        media = TrackMedia(cache, disc, disc.track(1), _StubRipper(),
                           stall_timeout=0.05)
        with self.assertRaises(MediaStalled):
            media.read(0, 4096, threading.Event())


class TestEngineFailureState(unittest.TestCase):
    def test_fail_marks_current_stream_error(self):
        states: list[str] = []
        engine = Engine(on_state=states.append)
        engine._abort = threading.Event()
        engine._generation += 1
        engine.fail("第 1 轨抓轨失败：boom")
        self.assertEqual(engine.state, "error")
        self.assertIn("boom", engine.error)
        self.assertEqual(states, ["error"])
        self.assertIsNone(engine._abort)             # 输出流已释放

    def test_stale_stream_failure_is_ignored(self):
        """旧喂数据线程迟到的失败不能把 seek/换轨后的新流标成 error。"""
        engine = Engine()
        engine._abort = threading.Event()
        engine._generation = 3
        engine._fail("stale", threading.Event(), 3)  # abort 不是当前流
        self.assertEqual(engine.state, "idle")
        engine._fail("stale", engine._abort, 2)      # 旧 generation
        self.assertEqual(engine.state, "idle")

    def test_fail_without_stream_is_noop(self):
        engine = Engine()
        engine.fail("boom")
        self.assertEqual(engine.state, "idle")
        self.assertEqual(engine.error, "")


class TestEngineSeekGeneration(unittest.TestCase):
    """Seek 换流后，旧喂数据线程不能改写新流的状态/播放头。"""

    class _Media:
        track = type("Track", (), {"seconds": 30.0})()

        def __init__(self, payload: bytes = b"", block: bool = False):
            self.payload = payload
            self.block = block
            self.entered = threading.Event()
            self.release = threading.Event()

        def read(self, offset, nbytes, abort):
            self.entered.set()
            if self.block:
                self.release.wait(2.0)
            return self.payload

    class _LiveProc:
        pass

    def _start_stale_feeder(self, engine, media, old_abort):
        thread = threading.Thread(
            target=engine._feed_loop,
            args=(self._LiveProc(), old_abort, 0, 1),
            daemon=True,
        )
        thread.start()
        self.assertTrue(media.entered.wait(2.0))
        return thread

    @staticmethod
    def _switch_to_new_stream(engine, new_abort):
        with engine._lock:
            engine._abort = new_abort
            engine._generation = 2
            engine.state = "playing"

    def test_stale_feeder_cannot_mark_new_stream_eof(self):
        engine = Engine()
        media = self._Media(b"", block=True)
        engine._media = media
        old_abort = threading.Event()
        new_abort = threading.Event()
        engine._abort = old_abort
        engine._generation = 1
        engine.state = "playing"

        thread = self._start_stale_feeder(engine, media, old_abort)
        self._switch_to_new_stream(engine, new_abort)
        media.release.set()
        thread.join(timeout=2)
        self.assertEqual(engine.state, "playing",
                         "旧 feeder 的 EOF 不能覆盖新流")

    def test_stale_feeder_does_not_emit_audio_to_new_stream(self):
        engine = Engine()
        media = self._Media(b"\x00" * 16, block=True)
        engine._media = media
        engine._write_all = lambda proc, data: None
        events: list[bytes] = []
        engine.on_audio = events.append
        old_abort = threading.Event()
        new_abort = threading.Event()
        engine._abort = old_abort
        engine._generation = 1
        engine.state = "playing"

        thread = self._start_stale_feeder(engine, media, old_abort)
        self._switch_to_new_stream(engine, new_abort)
        media.release.set()
        thread.join(timeout=2)
        self.assertEqual(events, [], "旧代音频不能喂给新流的分析器")

    def test_stale_feeder_cannot_overwrite_new_head(self):
        engine = Engine()
        engine._media = self._Media()
        engine._generation = 2
        engine._consumed = 15 * 176400
        engine._write_all = lambda proc, data: None
        engine._feed_loop(mock.Mock(), threading.Event(), 0, 1)
        self.assertEqual(engine._consumed, 15 * 176400)

    def test_aborted_feeder_cannot_overwrite_new_head(self):
        engine = Engine()
        engine._media = self._Media()
        engine._generation = 2
        engine._consumed = 20 * 176400
        engine._write_all = lambda proc, data: None
        abort = threading.Event()
        abort.set()
        engine._feed_loop(mock.Mock(), abort, 0, 2)
        self.assertEqual(engine._consumed, 20 * 176400)


class TestEngineStopPlayback(unittest.TestCase):
    """停止（s）：不是暂停 —— 关输出、播放头归零，音源留着重播。"""

    class _Media:
        track = type("Track", (), {"seconds": 10.0})()

    def _engine(self, state="playing"):
        engine = Engine()
        engine._media = self._Media()
        engine.state = state
        engine._stop_output = lambda *a, **k: True
        return engine

    def test_stop_resets_head_and_keeps_media(self):
        engine = self._engine()
        engine._consumed = 5 * 176400
        engine.stop_playback()
        self.assertEqual(engine.state, "stopped")
        self.assertEqual(engine._consumed, 0)
        self.assertEqual(engine.position(), 0.0)
        self.assertIsNotNone(engine.media)
        self.assertTrue(engine._paused.is_set())

    def test_stop_without_media_is_noop(self):
        engine = Engine()
        engine._stop_output = lambda *a, **k: self.fail("没有音源不该碰输出")
        engine.stop_playback()
        self.assertEqual(engine.state, "idle")

    def test_seek_while_stopped_only_moves_head(self):
        engine = self._engine(state="stopped")
        engine._consumed = 0
        engine._stop_output = lambda *a, **k: self.fail("停止态 seek 不该重开输出")
        engine.seek_to(5.0)
        self.assertEqual(engine.state, "stopped")
        self.assertAlmostEqual(engine.position(), 5.0, places=2)

    def test_seek_uses_quick_output_stop(self):
        """Seek 不能等旧 pw-cat 优雅放完 1s；用 quick 切断旧流。"""
        engine = self._engine(state="playing")
        calls: list[dict] = []
        engine._stop_output = lambda *a, **k: calls.append(k) or True
        engine._start_output = lambda off: None
        engine.seek_to(5.0)
        self.assertEqual(calls, [{"quick": True}])

    def test_seek_while_paused_never_leaves_paused(self):
        """暂停态 Seek 必须保持 _paused，否则喂数据线程会闪出一声。"""
        engine = self._engine(state="paused")
        engine._paused.set()
        starts: list[tuple[int, bool]] = []
        engine._start_output = lambda off: starts.append((off, engine._paused.is_set()))
        engine.seek_to(5.0)
        self.assertTrue(engine._paused.is_set())
        self.assertEqual(starts, [(5 * 176400, True)])

    def test_seek_to_same_head_does_not_restart_output(self):
        engine = self._engine(state="playing")
        engine._proc = mock.Mock()
        engine._consumed = 5 * 176400
        engine._stop_output = lambda *a, **k: self.fail("原地 Seek 不该重启输出")
        engine.seek_to(5.0)
        self.assertEqual(engine._consumed, 5 * 176400)

    def test_play_from_stopped_restarts_at_head(self):
        engine = self._engine(state="stopped")
        engine._consumed = 0
        offsets: list[int] = []
        engine._start_output = lambda off: offsets.append(off)
        engine.play()
        self.assertEqual(offsets, [0])
        self.assertFalse(engine._paused.is_set())

    def test_cue_holds_media_without_starting_output(self):
        """启动停止态只装音源：不建 pw-cat，不启动喂数据线程。"""
        engine = Engine()
        starts: list[int] = []
        engine._start_output = lambda off: starts.append(off)
        engine.cue(self._Media(), start_s=5.0)

        self.assertEqual(engine.state, "stopped")
        self.assertIsNotNone(engine.media)
        self.assertAlmostEqual(engine.position(), 5.0, places=2)
        self.assertEqual(starts, [])
        self.assertIsNone(engine._proc)
        self.assertIsNone(engine._thread)

    def test_cue_then_play_opens_output_at_cued_position(self):
        engine = Engine()
        starts: list[int] = []
        engine._start_output = lambda off: starts.append(off)
        engine.cue(self._Media(), start_s=3.0)
        engine.play()

        self.assertEqual(starts, [3 * 176400])
        self.assertFalse(engine._paused.is_set())


class TestAppTickOnRipError(unittest.TestCase):
    """P1②：engine error 时 app 不能自动下一轨、不能一直转缓冲动画。"""

    class _Engine:
        def __init__(self, state="error"):
            self.state = state
            self.error = "第 1 轨抓轨失败"
            self.paused = self.played = self.stopped = False
            self.failed_with: str | None = None

        def position(self) -> float:
            return 0.0

        def pause(self) -> None:
            self.paused = True

        def play(self) -> None:
            self.played = True

        def stop(self) -> None:
            self.stopped = True

        def fail(self, message: str) -> None:
            self.failed_with = message
            self.state = "error"

    class _Ripper:
        def __init__(self, failed: bool = False, reason: str = "cdparanoia rc=1"):
            self.failed = failed
            self.reason = reason
            self.requested: list[int] = []

        def available(self, number: int) -> int:
            return 0

        def want(self, number: int) -> None:
            pass

        def request_priority(self, number: int, immediate: bool = False) -> None:
            self.requested.append(number)

        def is_failed(self, number: int) -> bool:
            return self.failed

        def failure_reason(self, number: int) -> str:
            return self.reason

        def is_done(self, number: int) -> bool:
            return True          # 该测试里的轨视为已缓存，EOF 应正常切下一轨

    def _app(self, state="error", failed: bool = False):
        from app import App

        app = App.__new__(App)
        app.engine = self._Engine(state)
        app.ripper = self._Ripper(failed)
        app.disc = build_fake_disc()
        app.cache = None
        app.monitor = None
        app.disc_present = True
        app.disc_state = "ready"
        app.eject_action = "stop"
        app.insert_action = "play"
        app._ripper_pending = False
        app._ripper_stuck = False
        app.track_no = 1
        app.message = ""
        app._message_until = 0.0
        app.hint = "缓冲中 0.0s / 6s"
        app._autopaused = True
        app._spin = "⠋⠙"
        app._spin_i = 0
        app.layout = "dash"
        app.theme_name = "phosphor"
        app.started = []
        app._start_track = lambda number, start_s=0.0, transition=None: (
            app.started.append(number))
        return app

    def test_error_clears_autopause_and_does_not_advance(self):
        app = self._app()
        app._tick()
        self.assertFalse(app._autopaused)
        self.assertEqual(app.started, [], "抓轨失败不能自动下一轨")
        self.assertFalse(app.engine.paused)
        self.assertFalse(app.engine.played)
        self.assertEqual(app.hint, "")

    def test_buffering_does_not_write_verbose_hint(self):
        """首段抓轨/缓冲不再占用底框提示；状态只留在 CACHE 行。"""
        app = self._app(state="paused")
        app.hint = "旧提示"
        app._tick()
        self.assertEqual(app.hint, "")

    def test_tick_uses_debounced_priority_request(self):
        app = self._app(state="paused")
        app._tick()
        self.assertEqual(app.ripper.requested, [1],
                         "逐帧 tick 只能发防抖请求，不能绕过稳定窗口")

    def test_failed_rip_wakes_autopaused_engine(self):
        """抓轨永久失败时即使 engine 卡在 autopause，也要收尾并报错。"""
        app = self._app(state="paused", failed=True)
        app._tick()
        self.assertEqual(app.engine.state, "error")
        self.assertEqual(app.engine.failed_with,
                         "第 1 轨抓轨失败：cdparanoia rc=1")
        self.assertFalse(app._autopaused)
        self.assertEqual(app.started, [])
        self.assertEqual(app.hint, "")

    def test_eof_still_advances_normally(self):
        app = self._app(state="eof")
        app._tick()
        self.assertEqual(app.started, [2])

    def test_eof_advances_with_auto_transition(self):
        app = self._app(state="eof")
        calls = []
        app._start_track = lambda number, start_s=0.0, paused=None, transition=None: (
            calls.append((number, transition)))
        app._tick()
        self.assertEqual(calls, [(2, "auto")])


class TestDepsCheck(unittest.TestCase):
    """P2：分发包依赖说明 + 运行前自检（--check-deps）。"""

    @staticmethod
    def _which(present: set[str]):
        return lambda name: (f"/usr/bin/{name}" if name in present else None)

    def _checks(self, present: set[str], fake: bool = False, numpy: bool = True):
        with mock.patch("services.deps.shutil.which", side_effect=self._which(present)), \
             mock.patch("services.deps._module_available", return_value=numpy):
            return check_dependencies(fake=fake)

    def test_all_present(self):
        checks = self._checks({"cdparanoia", "pw-cat", "kitty"})
        self.assertTrue(required_ok(checks))
        report = format_report(checks)
        self.assertIn("✓", report)
        self.assertIn("可以运行", report)

    def test_missing_numpy_is_required_failure(self):
        checks = self._checks({"cdparanoia", "pw-cat", "kitty"}, numpy=False)
        self.assertFalse(required_ok(checks))
        self.assertIn("python-numpy", format_report(checks))

    def test_missing_cdparanoia_only_ok_in_fake_mode(self):
        present = {"pw-cat", "kitty"}
        self.assertFalse(required_ok(self._checks(present)))
        self.assertTrue(required_ok(self._checks(present, fake=True)))

    def test_missing_pw_cat_required_even_in_fake(self):
        self.assertFalse(required_ok(self._checks({"cdparanoia", "kitty"}, fake=True)))

    def test_kitty_is_optional(self):
        checks = self._checks({"cdparanoia", "pw-cat"})
        self.assertTrue(required_ok(checks))
        kitty = [c for c in checks if c.name == "kitty"][0]
        self.assertFalse(kitty.ok)
        self.assertTrue(kitty.optional)

    def test_missing_pipewire_session_is_only_a_warning(self):
        with mock.patch("services.deps.shutil.which",
                        side_effect=self._which({"pw-cat"})), \
             mock.patch("services.deps._module_available", return_value=True), \
             mock.patch("services.deps.pipewire_socket", return_value=None):
            checks = check_dependencies(fake=True)
        self.assertTrue(required_ok(checks), "会话没起不该拦启动（用户可先自查）")
        session = [c for c in checks if c.name == "PipeWire 会话"][0]
        self.assertTrue(session.warn)
        self.assertIn("systemctl --user start pipewire", format_report(checks))

    def test_cli_check_deps_exit_codes(self):
        import asciicd
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("services.deps.shutil.which", return_value="/usr/bin/x"), \
                 mock.patch("services.deps._module_available", return_value=True):
                self.assertEqual(asciicd.main(["--check-deps"]), 0)
            with mock.patch("services.deps.shutil.which", return_value=None), \
                 mock.patch("services.deps._module_available", return_value=False):
                self.assertEqual(asciicd.main(["--check-deps"]), 1)
            with mock.patch("services.deps.shutil.which",
                            side_effect=lambda name: None if name == "cdparanoia"
                            else "/usr/bin/x"), \
                 mock.patch("services.deps._module_available", return_value=True):
                self.assertEqual(asciicd.main(["--check-deps", "--fake"]), 0)


class TestDistDocs(unittest.TestCase):
    def test_readme_lists_all_dependencies(self):
        from tools.build_dist import README
        for word in ("python3", "numpy", "cdparanoia", "pw-cat", "kitty",
                     "--check-deps", "不捆绑"):
            self.assertIn(word, README, f"分发包 README 缺 {word}")

    def test_install_script_runs_check(self):
        from tools.build_dist import INSTALL
        self.assertIn("--check-deps", INSTALL)


class TestDriveStatus(unittest.TestCase):
    """盘片探测：ioctl 返回值到托盘三态（开/关空/关有盘/未知）的映射。"""

    def _call(self, status):
        with mock.patch("cdda.discmon.os.open", return_value=7), \
             mock.patch("cdda.discmon.os.close"), \
             mock.patch("cdda.discmon.fcntl.ioctl", return_value=status):
            return drive_has_disc("/dev/sr0")

    def _tray(self, status):
        with mock.patch("cdda.discmon.os.open", return_value=7), \
             mock.patch("cdda.discmon.os.close"), \
             mock.patch("cdda.discmon.fcntl.ioctl", return_value=status):
            return drive_tray_state("/dev/sr0")

    def test_disc_ok(self):
        self.assertTrue(self._call(CDS_DISC_OK))

    def test_no_disc_and_tray_open(self):
        self.assertFalse(self._call(CDS_NO_DISC))
        self.assertFalse(self._call(CDS_TRAY_OPEN))

    def test_tray_state_mapping(self):
        self.assertEqual(self._tray(CDS_DISC_OK), TRAY_CLOSED_DISC)
        self.assertEqual(self._tray(CDS_NO_DISC), TRAY_CLOSED_EMPTY)
        self.assertEqual(self._tray(CDS_TRAY_OPEN), TRAY_OPEN)

    def test_no_info_is_unknown(self):
        self.assertIsNone(self._call(CDS_NO_INFO))
        self.assertIsNone(self._tray(CDS_NO_INFO))

    def test_not_ready_is_transitional(self):
        """托盘动作中的 DRIVE_NOT_READY 不能当成弹盘。"""
        self.assertIsNone(self._call(CDS_DRIVE_NOT_READY))
        self.assertIsNone(self._tray(CDS_DRIVE_NOT_READY))

    def test_errors_are_unknown(self):
        with mock.patch("cdda.discmon.os.open", side_effect=FileNotFoundError):
            self.assertIsNone(drive_has_disc("/dev/sr99"))
            self.assertIsNone(drive_tray_state("/dev/sr99"))
        with mock.patch("cdda.discmon.os.open", return_value=7), \
             mock.patch("cdda.discmon.os.close"), \
             mock.patch("cdda.discmon.fcntl.ioctl", side_effect=OSError("EINVAL")):
            self.assertIsNone(drive_has_disc("/dev/sr0"))
            self.assertIsNone(drive_tray_state("/dev/sr0"))


class TestDiscMonitor(unittest.TestCase):
    """换盘监视器：只轮询，不碰真实设备；TOC 读失败下一轮再试。"""

    def _monitor(self, present_probe, disc):
        mon = DiscMonitor(present_probe, lambda: disc, interval=0.01)
        mon.prime(True, disc)
        return mon

    def test_removal_and_insertion(self):
        disc = build_fake_disc(durations=(1.0,))
        state = {"present": True}
        mon = DiscMonitor(lambda: state["present"], lambda: disc, interval=0.01)
        mon.prime(True, disc)
        self.assertEqual(mon.snapshot(), (True, disc))
        state["present"] = False
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (False, None))
        state["present"] = True
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, disc))

    def test_unknown_probe_keeps_state(self):
        disc = build_fake_disc(durations=(1.0,))
        mon = self._monitor(lambda: None, disc)
        before = mon.tray_snapshot()
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, disc))
        self.assertEqual(mon.tray_snapshot(), before)

    def test_tray_edges_and_timestamp(self):
        """open / closed-empty / closed-disc 三态要分得开，边沿带时间戳。"""
        disc = build_fake_disc(durations=(1.0,))
        state = {"tray": TRAY_CLOSED_DISC}
        mon = DiscMonitor(lambda: state["tray"], lambda: disc, interval=0.01)
        with mock.patch("cdda.discmon.time.monotonic",
                        side_effect=[10.0, 11.0, 12.0]):
            mon.prime(TRAY_CLOSED_DISC, disc)
            state["tray"] = TRAY_OPEN
            mon.poll_once()
            self.assertEqual(mon.snapshot(), (False, None))
            state["tray"] = TRAY_CLOSED_EMPTY
            mon.poll_once()
        self.assertEqual(mon.tray_snapshot(), (TRAY_CLOSED_EMPTY, 12.0, None))

    def test_toc_pending_keeps_closed_disc_edge(self):
        """插盘边沿先到、TOC 后到：托盘状态和起始时间不能被 TOC 重试刷新。"""
        disc = build_fake_disc(durations=(1.0,))
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            if calls["n"] == 1:
                raise CddaError("盘还没转起来")
            return disc

        mon = DiscMonitor(lambda: TRAY_CLOSED_DISC, read, interval=0.01)
        with mock.patch("cdda.discmon.time.monotonic",
                        side_effect=[10.0, 11.0]):
            mon.prime(TRAY_CLOSED_EMPTY, None)
            mon.poll_once()
            mon.poll_once()
        self.assertEqual(mon.tray_snapshot(), (TRAY_CLOSED_DISC, 11.0, disc))

    def test_toc_failure_then_retry(self):
        disc = build_fake_disc(durations=(1.0,))
        calls = {"n": 0}

        def read():
            calls["n"] += 1
            if calls["n"] == 1:
                raise CddaError("盘还没转起来")
            return disc

        mon = DiscMonitor(lambda: True, read, interval=0.01)
        mon.prime(False, None)
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, None))
        mon.poll_once()
        self.assertEqual(mon.snapshot(), (True, disc))

    def test_thread_stops(self):
        mon = DiscMonitor(lambda: False, lambda: None, interval=0.01)
        mon.start()
        mon.stop()
        mon.join(timeout=2)
        self.assertFalse(mon.is_alive())


class TestDiscUI(unittest.TestCase):
    def test_dash_and_mini_show_no_disc(self):
        for w, h, layout in ((117, 25, "dash"), (100, 12, "mini")):
            rows = render(make_ui_state(layout=layout, disc_present=False), w, h)
            visible = [strip_ansi(r) for r in rows]
            self.assertTrue(any("无光盘" in r for r in visible), f"{layout} 未显示无光盘")
        rows = render(make_ui_state(), 117, 25)
        self.assertFalse(any("无光盘" in strip_ansi(r) for r in rows))


class TestDiscCliFlags(unittest.TestCase):
    def test_on_eject_and_insert(self):
        from asciicd import build_parser

        parser = build_parser()
        self.assertIsNone(parser.parse_args([]).on_eject)
        self.assertIsNone(parser.parse_args([]).on_insert)
        args = parser.parse_args(["--on-eject", "stop", "--on-insert", "pause"])
        self.assertEqual((args.on_eject, args.on_insert), ("stop", "pause"))

    def test_rip_mode(self):
        from asciicd import build_parser

        parser = build_parser()
        self.assertIsNone(parser.parse_args([]).rip_mode)
        self.assertEqual(parser.parse_args(["--rip-mode", "disc"]).rip_mode, "disc")
        self.assertEqual(parser.parse_args(["--rip-mode", "track"]).rip_mode, "track")


class TestRipperStopAndJoin(unittest.TestCase):
    """换盘/退出必须等旧抓轨线程真退出，不能只 set Event。"""

    class SlowRipper(RipperBase):
        def __init__(self, disc, cache):
            super().__init__(disc, cache, name="slow-ripper")

        def _rip_one(self, number: int) -> None:
            time.sleep(0.01)

    def test_join_waits_for_thread_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0,))
            ripper = self.SlowRipper(disc, Cache(tmp))
            ripper.start()
            time.sleep(0.05)
            self.assertTrue(ripper.is_alive())
            ripper.stop_and_join(timeout=5)
            self.assertFalse(ripper.is_alive())


class _MetaCache:
    def __init__(self, tag: str):
        self.tag = tag
        self.written: list[tuple[str, str, dict]] = []

    def read_meta(self, disc) -> dict:
        return {}

    def write_meta(self, disc, data: dict) -> None:
        self.written.append((self.tag, disc.discid, data))


class TestMetadataDiscCapture(unittest.TestCase):
    """换盘时，上一个盘未返回的曲名查询不能写进新盘缓存。"""

    def test_disc_swap_during_fetch_keeps_old_cache(self):
        from types import SimpleNamespace

        from app import App

        app = App.__new__(App)
        app.args = SimpleNamespace(no_metadata=False)
        old_disc = build_fake_disc(durations=(1.0,))
        new_disc = build_fake_disc(durations=(2.0,))
        self.assertNotEqual(old_disc.discid, new_disc.discid)
        old_cache, new_cache = _MetaCache("old"), _MetaCache("new")
        app.disc, app.cache = old_disc, old_cache

        started = threading.Event()
        release = threading.Event()

        def fake_fetch(disc):
            started.set()
            release.wait(2)
            return {"tracks": {"1": "旧盘曲名"}}

        with mock.patch("app.metadata.fetch", side_effect=fake_fetch):
            app._start_metadata_lookup()
            self.assertTrue(started.wait(2))
            # 请求还没返回就换盘
            app.disc, app.cache = new_disc, new_cache
            release.set()
            deadline = time.monotonic() + 3
            while not old_cache.written and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertTrue(old_cache.written, "结果应写回发起查询时的旧盘缓存")
        for tag, discid, data in old_cache.written:
            self.assertEqual(tag, "old")
            self.assertEqual(discid, old_disc.discid)
            self.assertEqual(data["tracks"]["1"], "旧盘曲名")
        self.assertEqual(new_cache.written, [], "不能写进新盘缓存")


class TestRipPreservePartial(unittest.TestCase):
    """抓轨器停止时 .part 的保留/丢弃行为。"""

    class PartialRipper(Ripper):
        def __init__(self, disc, cache, script):
            super().__init__(disc, cache, "/dev/null")
            self.script = script

        def _cmd(self, number: int) -> list[str]:
            return [sys.executable, self.script]

    def _make(self, tmp_name: str):
        script = os.path.join(tmp_name, "slow_cdparanoia.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("import sys, time\n"
                     "sys.stdout.buffer.write(b'\\x00' * 65536)\n"
                     "sys.stdout.buffer.flush()\n"
                     "time.sleep(30)\n")
        disc = build_fake_disc(durations=(2.0,))
        cache = Cache(os.path.join(tmp_name, "cache"))
        ripper = self.PartialRipper(disc, cache, script)
        ripper.start()
        part = cache.part_path(disc, 1)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if part.exists() and part.stat().st_size > 0:
                break
            time.sleep(0.01)
        return disc, cache, ripper, part

    def test_preserve_keeps_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            _disc, _cache, ripper, part = self._make(tmp)
            self.assertTrue(ripper.stop_and_join(preserve_part=True))
            self.assertTrue(part.exists(), "continue 模式不能删掉已缓冲的 .part")
            self.assertGreater(part.stat().st_size, 0)

    def test_discard_removes_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            _disc, _cache, ripper, part = self._make(tmp)
            self.assertTrue(ripper.stop_and_join(preserve_part=False))
            self.assertFalse(part.exists(), "stop 模式仍应丢掉半成品")

class TestRipperStopTimeout(unittest.TestCase):
    """stop_and_join 超时要返回 False，调用方不能静默继续换盘。"""

    class StubbornRipper(RipperBase):
        def __init__(self, disc, cache):
            super().__init__(disc, cache, name="stubborn")

        def _rip_one(self, number: int) -> None:
            time.sleep(0.3)          # 停止信号来了也要跑完这一下

    def test_timeout_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0,))
            ripper = self.StubbornRipper(disc, Cache(tmp))
            ripper.start()
            time.sleep(0.05)
            self.assertIs(ripper.stop_and_join(timeout=0.05), False)
            self.assertTrue(ripper.is_alive())
            ripper.join(timeout=3)
            self.assertFalse(ripper.is_alive())


class TestSynth(unittest.TestCase):
    def test_chunk_independent(self):
        s = MusicSynth()
        a = s.render(1, 0, 512)
        b = s.render(1, 0, 2048)[:512]
        np.testing.assert_allclose(a, b, rtol=0, atol=1e-6)

    def test_signal_healthy(self):
        s = MusicSynth()
        audio = s.render(2, 44100, 44100)
        self.assertEqual(audio.shape, (44100, 2))
        rms = float(np.sqrt(np.mean(audio ** 2)))
        self.assertGreater(rms, 0.05)
        self.assertLess(rms, 0.95)
        self.assertTrue(np.all(np.isfinite(audio)))

    def test_fake_disc(self):
        d = build_fake_disc()
        self.assertEqual(len(d.tracks), 3)
        self.assertEqual(d.tracks[0].start_lba, 0)
        self.assertEqual(d.leadout_lba, sum(t.sectors for t in d.tracks))
        self.assertEqual(len(d.discid), 16)


class TestAppExitClearsCache(unittest.TestCase):
    """退出只清 PCM；meta.json 留着，下次没网也能显示曲名。"""

    def _app(self, tmp, disc):
        from app import App

        app = App.__new__(App)
        app.cache = Cache(tmp)
        app.disc = disc
        app.monitor = app.engine = app.ripper = None
        app._save_theme = lambda: None

        class _Screen:
            def leave(self) -> None:
                pass

        app.screen = _Screen()
        return app

    def test_shutdown_removes_audio_but_keeps_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0,))
            app = self._app(tmp, disc)
            app.cache.ensure_dir(disc)
            app.cache.mark_done(disc, 1)
            app.cache.write_meta(disc, {"album": "留着", "tracks": {"1": "曲名"}})
            app.shutdown()
            self.assertFalse(app.cache.done_path(disc, 1).exists())
            self.assertTrue(app.cache.meta_path(disc).exists())
            self.assertEqual(app.cache.read_meta(disc)["album"], "留着")

    def test_shutdown_removes_empty_disc_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            disc = build_fake_disc(durations=(1.0,))
            app = self._app(tmp, disc)
            app.cache.ensure_dir(disc)
            app.cache.mark_done(disc, 1)
            app.shutdown()
            self.assertFalse(app.cache.disc_dir(disc).exists())

    def test_shutdown_without_disc_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp, None)
            app.shutdown()                     # 没盘/没缓存时也不能炸


class TestVersion(unittest.TestCase):
    """P0⑤：源码 CLI、zipapp、pyproject、包名/归档名用同一个版本号。"""

    def test_cli_version(self):
        import asciicd
        from config import VERSION

        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
             self.assertRaises(SystemExit) as cm:
            asciicd.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"asciicd {VERSION}")

    def test_pyproject_version_matches_config(self):
        from config import VERSION

        path = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        self.assertIsNotNone(match, "pyproject.toml 里找不到 version")
        self.assertEqual(match.group(1), VERSION)
        self.assertEqual(VERSION, "0.1.1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
