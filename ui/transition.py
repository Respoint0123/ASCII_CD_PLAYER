"""开仓/回仪表盘的「从上到下压扁」转场。

只做行合成，不碰终端：
  - source/target 都是等高的 ANSI 行；
  - p=0 显示 source，p=1 显示 target；
  - 中间把 source 垂直重采样到底部窄带，target 从上方露出；
  - 接缝是一条静态亮线，不做噪点/闪烁。
"""
from __future__ import annotations

from ui.glyphs import RESET, fg, fit, text_width


def squash(source: list[str], target: list[str], p: float, theme) -> list[str]:
    h = len(target) or len(source)
    if h <= 0:
        return []
    w = max((text_width(r) for r in source + target), default=0)
    source = [fit(r, w) for r in source[:h]]
    target = [fit(r, w) for r in target[:h]]
    if len(source) < h:
        source += [""] * (h - len(source))
    if len(target) < h:
        target += [""] * (h - len(target))
    p = max(0.0, min(1.0, p))
    if p <= 0.02:
        return source
    if p >= 0.97:
        return target
    e = p * p * (3.0 - 2.0 * p)
    band = max(1, int(round((1.0 - e) * h)))
    top = h - band
    out: list[str] = list(target[:top])
    out.append(fg(theme["text"]) + "─" * w + RESET)
    inner = band - 1
    for i in range(inner):
        src = min(h - 1, int(round(i * (h - 1) / max(1, inner - 1))))
        out.append(source[src])
    if len(out) < h:
        out.extend(target[-1] for _ in range(h - len(out)))
    return [fit(r, w) for r in out[:h]]
