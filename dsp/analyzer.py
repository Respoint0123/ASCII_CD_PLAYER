"""可视化分析：PCM → 频谱 / 波形 / 电平。

线程模型：播放线程 push()，UI 线程 frame() 取快照；内部一把小锁。
所有量都归一化到 0..1，渲染层不需要懂 dB。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np

from config import (CHANNELS, F_MAX, F_MIN, N_FFT, PEAK_FALL, SAMPLE_RATE,
                    SPEC_ATTACK, SPEC_FALL, SPECTRUM_BINS, WATERFALL_ROWS)

SCOPE_FRAMES = 768           # 示波器窗口（约 17 ms）
LEVEL_FRAMES = 2048
CAPACITY = 32768             # 环形缓冲（0.74 s）
HISTORY_DT = 0.12            # 瀑布谱采样间隔


@dataclass
class VisFrame:
    spectrum: np.ndarray = field(default_factory=lambda: np.zeros(SPECTRUM_BINS))
    peaks: np.ndarray = field(default_factory=lambda: np.zeros(SPECTRUM_BINS))
    scope_l: np.ndarray = field(default_factory=lambda: np.zeros(SCOPE_FRAMES, np.float32))
    scope_r: np.ndarray = field(default_factory=lambda: np.zeros(SCOPE_FRAMES, np.float32))
    waterfall: np.ndarray = field(default_factory=lambda: np.zeros((WATERFALL_ROWS, SPECTRUM_BINS)))
    rms_db: tuple[float, float] = (-90.0, -90.0)
    peak_db: tuple[float, float] = (-90.0, -90.0)
    silent: bool = True

    @property
    def level_l(self) -> float:
        return max(0.0, min(1.0, (self.rms_db[0] + 60.0) / 60.0))

    @property
    def level_r(self) -> float:
        return max(0.0, min(1.0, (self.rms_db[1] + 60.0) / 60.0))


class Analyzer:
    def __init__(self, sr: int = SAMPLE_RATE, n_fft: int = N_FFT, bins: int = SPECTRUM_BINS):
        self.sr = sr
        self.n_fft = n_fft
        self.bins = bins
        self._lock = threading.Lock()
        self._buf = np.zeros((CAPACITY, CHANNELS), dtype=np.float32)
        self._w = 0
        self._count = 0

        self._window = np.hanning(n_fft).astype(np.float32)
        edges = np.geomspace(F_MIN, F_MAX, bins + 1)
        freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
        idx = np.searchsorted(freqs, edges)
        idx = np.clip(idx, 0, len(freqs) - 1)
        # reduceat 需要严格递增的起点
        idx = np.maximum.accumulate(idx)
        if idx[0] != 0:
            idx[0] = 0
        self._band_idx = idx

        self._spec = np.zeros(bins, dtype=np.float32)
        self._peaks = np.zeros(bins, dtype=np.float32)
        self._history = np.zeros((WATERFALL_ROWS, bins), dtype=np.float32)
        self._last_history = 0.0
        self._frame = VisFrame()

    # ── 写入（播放线程） ──────────────────────────────────────────────────
    def reset(self) -> None:
        with self._lock:
            self._buf.fill(0.0)
            self._w = 0
            self._count = 0
            self._spec.fill(0.0)
            self._peaks.fill(0.0)
            self._history.fill(0.0)

    def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        frames = arr.reshape(-1, CHANNELS)
        n = len(frames)
        if n == 0:
            return
        with self._lock:
            if n >= CAPACITY:
                self._buf[:] = frames[-CAPACITY:]
                self._w = 0
                self._count = CAPACITY
                return
            end = self._w + n
            if end <= CAPACITY:
                self._buf[self._w:end] = frames
            else:
                first = CAPACITY - self._w
                self._buf[self._w:] = frames[:first]
                self._buf[: end - CAPACITY] = frames[first:]
            self._w = end % CAPACITY
            self._count = min(CAPACITY, self._count + n)

    def _tail(self, frames: int) -> np.ndarray:
        frames = min(frames, self._count)
        if frames <= 0:
            return np.zeros((0, CHANNELS), np.float32)
        start = (self._w - frames) % CAPACITY
        if start + frames <= CAPACITY:
            return self._buf[start:start + frames].copy()
        head = CAPACITY - start
        return np.concatenate([self._buf[start:], self._buf[: frames - head]], axis=0)

    # ── 取快照（UI 线程） ─────────────────────────────────────────────────
    def frame(self, now: float | None = None) -> VisFrame:
        now = time.monotonic() if now is None else now
        with self._lock:
            data = self._tail(max(self.n_fft, SCOPE_FRAMES))
            count = self._count
        if count < 256:
            return VisFrame(waterfall=self._history.copy())

        if data.shape[0] < self.n_fft:          # 开头数据不够时补零，保证 bin 映射固定
            pad = np.zeros((self.n_fft - data.shape[0], CHANNELS), np.float32)
            data = np.concatenate([pad, data], axis=0)
        mono = data[-self.n_fft:, :].mean(axis=1)
        spec = np.fft.rfft(mono * self._window)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        band = np.add.reduceat(power, self._band_idx[:-1]) if len(power) > 1 else power
        band = np.maximum(band, 1e-12)[: self.bins]
        db = 10.0 * np.log10(band)

        scope = data[-SCOPE_FRAMES:]
        level = data[-min(LEVEL_FRAMES, len(data)):]
        rms = np.sqrt(np.mean(level ** 2, axis=0) + 1e-12)
        peak = np.max(np.abs(level), axis=0) + 1e-9
        rms_db = tuple(20.0 * np.log10(max(float(v), 1e-6)) for v in rms)
        peak_db = tuple(20.0 * np.log10(max(float(v), 1e-6)) for v in peak)

        # 相对归一化（dB 动态范围 46 dB），静音时压制
        top = float(db.max())
        value = np.clip((db - (top - 46.0)) / 46.0, 0.0, 1.0) ** 0.85
        silence = float(np.clip((-48.0 - rms_db[0]) / 22.0, 0.0, 1.0))
        value = (value * (1.0 - silence)).astype(np.float32)

        spec_s = np.where(value > self._spec,
                          self._spec * (1.0 - SPEC_ATTACK) + value * SPEC_ATTACK,
                          self._spec * SPEC_FALL + value * (1.0 - SPEC_FALL)).astype(np.float32)
        peaks = np.maximum(self._peaks - PEAK_FALL, spec_s).astype(np.float32)

        with self._lock:
            self._spec = spec_s
            self._peaks = peaks
            if now - self._last_history >= HISTORY_DT:
                self._history = np.roll(self._history, -1, axis=0)
                self._history[-1] = spec_s
                self._last_history = now
            history = self._history.copy()

        fr = VisFrame(
            spectrum=spec_s,
            peaks=peaks,
            scope_l=(scope[:, 0] - scope[:, 0].mean()).copy(),
            scope_r=(scope[:, 1] - scope[:, 1].mean()).copy(),
            waterfall=history,
            rms_db=(float(rms_db[0]), float(rms_db[1])),
            peak_db=(float(peak_db[0]), float(peak_db[1])),
            silent=silence > 0.9,
        )
        return fr
