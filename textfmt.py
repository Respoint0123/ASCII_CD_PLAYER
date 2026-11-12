"""外部文本的单行化处理。

曲名、艺人、专辑可能来自光盘（CD-TEXT）、网络（MusicBrainz）或以前写下的
meta.json。它们都不是我们能控制的内容：可能带换行、制表符、ANSI 转义、
零宽字符或方向控制符。界面是「一行一个控件」的布局，所以进入系统时统一
压成单行纯文本 —— 否则一条带 `\\n` 的曲名就会撑破整块画面。
"""
from __future__ import annotations

import re
import unicodedata

# 终端转义序列：CSI（颜色/光标）、OSC（窗口标题等）、以及落单的 ESC。
# 先整段剥掉，免得变成 "[31m" 这样的可见垃圾。
_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"       # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"   # OSC
    r"|\x1b[@-_]"                     # 其余两字节转义
    r"|\x1b"                          # 落单的 ESC
)

MAX_LEN = 200        # 外部标题再长也不值得占满一行；截断比撑破画面好


def one_line(text: object, limit: int = MAX_LEN) -> str:
    """把任意外部文本压成单行：控制字符→空格、空白合并、超长截断。"""
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    raw = _ESCAPE_RE.sub("", raw)      # 转义序列整段去掉，不留空档
    out: list[str] = []
    for ch in raw:
        if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co"):
            out.append(" ")
        else:
            out.append(ch)
    text_out = " ".join("".join(out).split())
    if limit > 0 and len(text_out) > limit:
        text_out = text_out[:limit].rstrip()
    return text_out
