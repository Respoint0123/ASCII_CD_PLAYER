"""虚拟 CD：合成一段「像音乐」的音频，用于无光驱自测与演示（--fake）。

要求：逐块生成时结果必须只取决于「绝对采样序号」，这样渐进写入任意块大小都连续。
"""
from __future__ import annotations

import numpy as np

from cdda.device import Disc, Track
from config import SAMPLE_RATE as SR

PROGRESSIONS: list[list[tuple[int, ...]]] = [
    [(60, 64, 67), (57, 60, 64), (53, 57, 60), (55, 59, 62)],   # C  Am  F   G
    [(58, 62, 65), (58, 62, 65), (55, 58, 62), (60, 64, 67)],   # Bb Bb Gm  C
    [(62, 65, 69), (57, 60, 64), (60, 64, 67), (53, 57, 60)],   # Dm  Am  C   F
]

BEAT = 0.625          # 96 BPM
CHORD_SEC = 2.0


def midi_hz(m: int) -> float:
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


class MusicSynth:
    """一段带鼓、贝斯、琶音和 pads 的和声循环。"""

    def __init__(self, seed: int = 0):
        self.seed = seed

    def render(self, track_number: int, start_frame: int, nframes: int) -> np.ndarray:
        """返回 (nframes, 2) float32，取值范围 -1..1。"""
        prog = PROGRESSIONS[(track_number - 1) % len(PROGRESSIONS)]
        t = (start_frame + np.arange(nframes, dtype=np.float64)) / SR
        pad = np.zeros(nframes, dtype=np.float64)

        chord_idx = np.floor(t / CHORD_SEC).astype(np.int64)
        frac = t / CHORD_SEC - chord_idx
        env_pad = np.minimum(1.0, frac / 0.05) * np.minimum(1.0, (1.0 - frac) / 0.10)
        for c, chord in enumerate(prog):
            mask = (chord_idx % len(prog)) == c
            if not mask.any():
                continue
            tt = t[mask]
            for k, midi in enumerate(chord):
                f = midi_hz(midi)
                pad[mask] += (np.sin(2 * np.pi * f * tt + k * 0.4)
                              + 0.35 * np.sin(4 * np.pi * f * tt)
                              + 0.15 * np.sin(6 * np.pi * f * tt)) * 0.055
        pad *= env_pad

        # 贝斯：每个和弦的根音，低八度
        bass = np.zeros(nframes, dtype=np.float64)
        bi = np.floor(t / BEAT).astype(np.int64)
        bf = t / BEAT - bi
        env_b = np.exp(-bf * 4.0)
        for c, chord in enumerate(prog):
            mask = (bi % len(prog)) == c
            if not mask.any():
                continue
            tt = t[mask]
            f = midi_hz(chord[0] - 12)
            bass[mask] = (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt)) * 0.22
        bass *= env_b

        # 琶音：八分音符，和弦音上下行
        arp = np.zeros(nframes, dtype=np.float64)
        step = BEAT / 2
        ai = np.floor(t / step).astype(np.int64)
        af = t / step - ai
        env_a = np.exp(-af * 9.0)
        pattern = (0, 1, 2, 1)
        for c, chord in enumerate(prog):
            mask = (ai % len(prog)) == c
            if not mask.any():
                continue
            tt = t[mask]
            note = midi_hz(chord[pattern[int(ai[mask][0]) % len(pattern)]] + 12)
            arp[mask] = np.sin(2 * np.pi * note * tt) * 0.10
        arp *= env_a

        # 底鼓 + 军鼓 + 踩镲
        ki = np.floor(t / BEAT).astype(np.int64)
        kf = t / BEAT - ki
        kick = np.sin(2 * np.pi * (52 + 45 * np.exp(-kf * 12)) * (kf * BEAT)) * np.exp(-kf * 11) * 0.5
        snare_gate = kf > 0.5
        idx = np.arange(nframes, dtype=np.int64) + start_frame
        noise = np.mod(np.sin(idx * 12.9898 + self.seed) * 43758.5453, 1.0) * 2.0 - 1.0
        snare = np.where(snare_gate, noise * np.exp(-(kf - 0.5) * 26) * 0.22, 0.0)
        hi = np.floor(t / (BEAT / 2)).astype(np.int64)
        hf = t / (BEAT / 2) - hi
        hat = noise * np.exp(-hf * 45) * 0.05

        mono = pad + bass + arp + kick + snare + hat
        shimmer = 0.035 * np.sin(2 * np.pi * 1.7 * t)
        left = np.clip(mono + shimmer, -0.98, 0.98)
        right = np.clip(mono - shimmer, -0.98, 0.98)
        return np.stack([left, right], axis=1).astype(np.float32)


def build_fake_disc(durations: tuple[float, ...] = (36.0, 30.0, 24.0),
                    device: str = "fake://synth",
                    model: str = "asciicd virtual drive") -> Disc:
    tracks: list[Track] = []
    start = 0
    for i, dur in enumerate(durations):
        sectors = int(round(dur * 75))
        tracks.append(Track(number=i + 1, start_lba=start, sectors=sectors))
        start += sectors
    return Disc(device=device, tracks=tracks, leadout_lba=start, model=model)
