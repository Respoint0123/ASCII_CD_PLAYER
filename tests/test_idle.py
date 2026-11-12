"""待机动画：正弦/扫描相位对齐、点阵余辉、布局接入。"""
from __future__ import annotations

import unittest

import numpy as np

from app import App
from config import THEMES
from dsp.analyzer import VisFrame
from ui import idle
from ui.glyphs import braille_scope, strip_ansi, text_width
from ui.layouts import UIState, render


class TestIdleMath(unittest.TestCase):
    @staticmethod
    def _crest_xs(t: float, cells: int = 99, n: int = 16384) -> list[float]:
        wave = idle.sine_wave(t, n=n, cells=cells)
        x = np.linspace(0.0, 1.0, len(wave), endpoint=False)
        crests: list[float] = []
        for i in range(1, len(wave) - 1):
            if wave[i] >= wave[i - 1] and wave[i] >= wave[i + 1] \
                    and wave[i] > 0.15:
                if not crests or x[i] - crests[-1] > 0.01:
                    crests.append(float(x[i]))
        return crests

    def test_sine_has_two_crests_and_sane_amplitude(self):
        wave = idle.sine_wave(1.0, n=4096, cells=99)
        self.assertLessEqual(float(np.max(np.abs(wave))), 0.35)
        crests = self._crest_xs(1.0, n=4096)
        self.assertEqual(len(crests), 2)

    def test_sine_crests_move_at_constant_speed(self):
        """示波器正弦走线性相位：每 2 秒的位移必须一样，不能跟着频谱缓动。"""
        x0 = (idle.SPEC_COL_OFFSET - idle.SCOPE_ROW_OFFSET) / 99
        xs = []
        for k in range(5):
            t = k * 2.0
            want = (x0 + idle.sweep_linear(t)) % 1.0
            xs.append(min(self._crest_xs(t), key=lambda c: abs(c - want)))
        diffs = np.diff(xs)
        np.testing.assert_allclose(diffs, 2.0 / idle.T_SWEEP, atol=1e-3)

    def test_scope_and_spectrum_align_at_sweep_anchors(self):
        """首/中两个锚点对齐；速度变化的中段不要求精确同列。"""
        cells, spec_w = 99, 95
        for t in (0.0, idle.T_SWEEP / 2.0):
            spec_col = idle.SPEC_COL_OFFSET + idle.sweep_u(t, cells, spec_w) * spec_w
            cols = [idle.SCOPE_ROW_OFFSET + c * cells
                    for c in self._crest_xs(t)]
            self.assertLess(min(abs(c - spec_col) for c in cols), 1.2,
                            f"t={t} spec_col={spec_col} crests={cols}")

    def test_speed_easing_only_on_spectrum(self):
        early, late = idle.T_SWEEP * 0.25, idle.T_SWEEP * 0.75
        self.assertLess(idle.sweep_ease(early), idle.sweep_linear(early))
        self.assertGreater(idle.sweep_ease(late), idle.sweep_linear(late))

    def test_sweep_spectrum_peak_moves_with_u(self):
        for t in (4.0, 8.0, 12.0):
            spec, peaks = idle.sweep_spectrum(t, 99, 95)
            u = idle.sweep_u(t, 99, 95)
            self.assertEqual(spec.shape, (64,))
            self.assertEqual(peaks.shape, (64,))
            self.assertLess(abs(int(np.argmax(spec)) / 63.0 - u), 0.08,
                            f"t={t} u={u} argmax={np.argmax(spec)}")

    def test_edge_fade(self):
        spec0, _ = idle.sweep_spectrum(0.0, 99, 95)
        spec_mid, _ = idle.sweep_spectrum(idle.T_SWEEP / 2.0, 99, 95)
        self.assertLess(float(spec0.max()), 0.10)
        self.assertGreater(float(spec_mid.max()), 0.70)

    def test_morph_endpoints_match_renderers(self):
        live = np.sin(np.linspace(0.0, 20.0, 768)).astype(np.float32)
        sine = idle.sine_wave(4.0, cells=99)
        self.assertEqual(idle.morph_scope(live, sine, 0.0, 99, 6),
                         braille_scope(live, 99, rows=6))
        self.assertEqual(idle.morph_scope(live, sine, 1.0, 99, 6),
                         braille_scope(sine, 99, rows=6, auto_gain=False))

    def test_dotfield_rows_are_valid_braille(self):
        for name, theme in THEMES.items():
            for rows in (2, 3):
                lines = idle.dotfield_waterfall(4.0, theme, 99, 95, rows)
                self.assertEqual(len(lines), rows)
                for line in lines:
                    self.assertEqual(text_width(line), 95, name)
                plain = "".join(strip_ansi(line) for line in lines)
                self.assertTrue(any("\u2800" <= ch <= "\u28ff" for ch in plain),
                                name)


class _IdleApp(App):
    """只测 _update_idle，不装配真实播放器。"""

    def __init__(self):
        self.state = "paused"
        self._scope_mix = 0.0
        self._idle_target = 0.0
        self._idle_from = 0.0
        self._idle_t0 = 0.0
        self._idle_dur = 0.45
        self._idle_start = 0.0
        self._idle_t = 0.0

    def _state_name(self) -> str:
        return self.state


class TestIdleTransition(unittest.TestCase):
    def test_stop_enters_idle_and_play_returns(self):
        app = _IdleApp()
        app.state = "stopped"
        app._update_idle(0.0)
        self.assertEqual(app._idle_target, 1.0)
        self.assertEqual(app._scope_mix, 0.0)
        app._update_idle(0.55)
        self.assertAlmostEqual(app._scope_mix, 1.0, places=3)
        self.assertGreater(app._idle_t, 0.5)

        app.state = "playing"
        app._update_idle(1.0)
        self.assertEqual(app._idle_target, 0.0)
        app._update_idle(1.45)
        self.assertAlmostEqual(app._scope_mix, 0.0, places=3)

    def test_pause_freezes_without_idle(self):
        """暂停不进待机：画面冻结，绝不切正弦。"""
        app = _IdleApp()
        app.state = "paused"
        app._update_idle(0.0)
        self.assertEqual(app._idle_target, 0.0)
        app._update_idle(3.0)
        self.assertEqual(app._scope_mix, 0.0)
        self.assertEqual(app._idle_t, 0.0)

    def test_new_stop_restarts_from_zero(self):
        app = _IdleApp()
        app.state = "stopped"
        app._update_idle(0.0)
        app._update_idle(3.0)
        old = app._idle_t
        self.assertGreater(old, 2.0)
        app.state = "playing"
        app._update_idle(4.0)
        app._update_idle(4.45)
        app.state = "stopped"
        app._update_idle(5.0)
        self.assertLess(app._idle_t, 0.01)


class TestIdleLayout(unittest.TestCase):
    @staticmethod
    def _state(**kw) -> UIState:
        vis = VisFrame()
        vis.scope_l = np.sin(np.linspace(0, 8, 1024)).astype(np.float32) * 0.5
        vis.scope_r = vis.scope_l.copy()
        vis.spectrum = np.abs(np.sin(np.linspace(0, 6, 64))).astype(np.float32)
        vis.peaks = vis.spectrum.copy()
        vis.waterfall = [vis.spectrum * 0.6] * 3
        st = UIState(
            device="/dev/sr0", model="TEST", tracks=[object()] * 12,
            track_no=1, title="test", duration=100.0, state="paused",
            theme=THEMES["amber"], vis=vis, scope_mix=1.0, idle_t=8.0,
            total_seconds=1000.0,
        )
        for k, v in kw.items():
            setattr(st, k, v)
        return st

    def test_idle_dash_25_rows_has_wave_and_dotfield(self):
        rows = render(self._state(), 105, 25)
        self.assertEqual(len(rows), 25)
        for row in rows:
            self.assertEqual(text_width(row), 105)
        plain = "\n".join(strip_ansi(row) for row in rows)
        self.assertIn("OSCILLOSCOPE", plain)
        self.assertIn("SPECTRUM", plain)
        self.assertTrue(any("\u2800" <= ch <= "\u28ff" for ch in plain))

    def test_idle_respects_smaller_widths(self):
        for w, h in ((117, 25), (105, 23), (105, 22), (105, 21),
                     (80, 20)):
            rows = render(self._state(), w, h)
            self.assertLessEqual(len(rows), h)
            for row in rows:
                self.assertLessEqual(text_width(row), w)

    def test_live_state_does_not_use_idle(self):
        st = self._state(scope_mix=0.0, idle_t=8.0, state="playing")
        rows = render(st, 105, 25)
        plain = "\n".join(strip_ansi(row) for row in rows)
        self.assertIn("OSCILLOSCOPE", plain)
        self.assertEqual(len(rows), 25)


if __name__ == "__main__":
    unittest.main()
