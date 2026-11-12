"""CD-TEXT：直接发 SCSI READ TOC/PMA/ATIP（format 0101b）读盘上自带的文字。

不依赖 libcdio-utils，也不需要 pip 包 —— 一次 SG_IO ioctl 就够。盘上没写、
光驱不吐、没权限、非 Linux，一律返回 None，绝不挡播放。

响应格式（MMC / NCITS 1364-D）：
    4 字节头（2 字节长度 + 2 字节保留）+ 若干 18 字节 pack
    pack：类型、轨号、序号、字符位置/块号/双字节位、12 字节文本、2 字节 CRC

TITLE / PERFORMER 是文本流：每个字符串以 NUL 结尾，轨号从 pack 里的轨号开始
逐串递增；跨 pack 的字符串拼起来。0x09 是「同上」指示符。
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import sys

from textfmt import one_line

_PACK = 18
_TYPE_TITLE = 0x80
_TYPE_PERFORMER = 0x81
_TYPE_BLOCKSIZE = 0x8F

_SG_IO = 0x2285
_SG_DXFER_FROM_DEV = -3

# 块大小信息包里的字符集编码
_CHARSETS = {
    0x00: "latin-1",      # ISO-8859-1
    0x01: "latin-1",      # ASCII（有的盘会混 8 位字符，按 latin-1 读更容易看懂）
    0x80: "shift_jis",    # JIS X 0208
}


class _SgIoHdr(ctypes.Structure):
    _fields_ = [
        ("interface_id", ctypes.c_int),
        ("dxfer_direction", ctypes.c_int),
        ("cmd_len", ctypes.c_ubyte),
        ("mx_sb_len", ctypes.c_ubyte),
        ("iovec_count", ctypes.c_ushort),
        ("dxfer_len", ctypes.c_uint),
        ("dxferp", ctypes.c_void_p),
        ("cmdp", ctypes.c_void_p),
        ("sbp", ctypes.c_void_p),
        ("timeout", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("pack_id", ctypes.c_int),
        ("usr_ptr", ctypes.c_void_p),
        ("status", ctypes.c_ubyte),
        ("masked_status", ctypes.c_ubyte),
        ("msg_status", ctypes.c_ubyte),
        ("sb_len_wr", ctypes.c_ubyte),
        ("host_status", ctypes.c_ushort),
        ("driver_status", ctypes.c_ushort),
        ("resid", ctypes.c_int),
        ("duration", ctypes.c_uint),
        ("info", ctypes.c_uint),
    ]


def read(device: str, timeout_ms: int = 2000) -> dict | None:
    """读这张盘的 CD-TEXT；没有/读不到返回 None。{album, artist, tracks, source}"""
    if not device or not os.path.exists(device):
        return None
    try:
        data = _read_packs(device, timeout_ms)
    except OSError:
        return None
    if not data:
        return None
    try:
        return parse(data)
    except Exception:                    # 盘上数据再怪也不该影响播放
        return None


def _read_packs(device: str, timeout_ms: int) -> bytes:
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    try:
        head = _sg_read_toc(fd, 4096, timeout_ms)
        if not head:
            return b""
        total = int.from_bytes(head[:2], "big") - 2   # 长度含自己的 2 字节
        if total <= 0:
            return b""
        if total + 4 > len(head):
            head = _sg_read_toc(fd, min(total + 4, 65536), timeout_ms)
            if not head:
                return b""
        return head[4:4 + total]
    finally:
        os.close(fd)


def _sg_read_toc(fd: int, alloc: int, timeout_ms: int) -> bytes:
    if not sys.platform.startswith("linux"):
        return b""
    buf = ctypes.create_string_buffer(alloc)
    sense = ctypes.create_string_buffer(64)
    cdb = bytearray(10)
    cdb[0] = 0x43                 # READ TOC/PMA/ATIP
    cdb[1] = 0x02                 # MSF（和 libcdio 一样带上，有的光驱认）
    cdb[2] = 0x05                 # format 0101b = CD-TEXT
    cdb[7] = (alloc >> 8) & 0xFF
    cdb[8] = alloc & 0xFF
    cdb_p = ctypes.create_string_buffer(bytes(cdb), 10)
    hdr = _SgIoHdr()
    hdr.interface_id = ord("S")
    hdr.dxfer_direction = _SG_DXFER_FROM_DEV
    hdr.cmd_len = 10
    hdr.mx_sb_len = len(sense)
    hdr.dxfer_len = alloc
    hdr.dxferp = ctypes.cast(buf, ctypes.c_void_p)
    hdr.cmdp = ctypes.cast(cdb_p, ctypes.c_void_p)
    hdr.sbp = ctypes.cast(sense, ctypes.c_void_p)
    hdr.timeout = max(1, timeout_ms)
    fcntl.ioctl(fd, _SG_IO, hdr)
    if hdr.status or hdr.host_status or hdr.driver_status:
        return b""
    got = alloc - max(0, hdr.resid)
    return buf.raw[:got]


# ── 解析（纯函数，测试不碰光驱） ──────────────────────────────────────────
def parse(data: bytes) -> dict | None:
    packs = [data[i:i + _PACK] for i in range(0, len(data) - _PACK + 1, _PACK)]
    if not packs:
        return None

    blocks: dict[int, list[bytes]] = {}
    for p in packs:
        blocks.setdefault((p[3] >> 4) & 0x07, []).append(p)

    # 多语言时优先 block 0（多数盘第一块就是主语言），空的再往下找
    for block in sorted(blocks, key=lambda b: (b != 0, b)):
        text = _decode_block(blocks[block])
        if text:
            return text
    return None


def _decode_block(packs: list[bytes]) -> dict | None:
    charset = "latin-1"
    for p in packs:
        if p[0] == _TYPE_BLOCKSIZE:
            charset = _CHARSETS.get(p[4], "latin-1")
            break

    fields: dict[tuple[int, int], str] = {}
    last: dict[int, str] = {}
    buf = bytearray()
    for p in packs:
        ptype = p[0]
        if ptype not in (_TYPE_TITLE, _TYPE_PERFORMER):
            continue
        wide = bool(p[3] & 0x80)          # 双字节字符
        step = 2 if wide else 1
        track = p[1]
        pending_tab = False
        j = 0
        while j < 12:
            unit = p[4 + j:4 + j + step]
            if len(unit) < step:
                break
            if all(b == 0 for b in unit):            # 字符串结束
                if buf:
                    text = _decode(buf, charset)
                elif pending_tab:
                    text = last.get(ptype, "")
                else:
                    text = ""
                if text:
                    fields[(ptype, track)] = text
                    last[ptype] = text
                buf.clear()
                pending_tab = False
                track += 1
            elif unit[0] == 0x09 and not buf:        # 「同上」
                pending_tab = True
            else:
                buf += unit
            j += step

    album = fields.get((_TYPE_TITLE, 0), "")
    artist = (fields.get((_TYPE_PERFORMER, 0))
              or fields.get((_TYPE_PERFORMER, 1)) or "")
    tracks = {str(n): t for (ptype, n), t in fields.items()
              if ptype == _TYPE_TITLE and n >= 1}
    if not album and not tracks:
        return None
    # 盘上的文字同样不可信（可能是刻录时随手塞的控制字符），压成单行再交给 UI。
    return {
        "album": one_line(album),
        "artist": one_line(artist),
        "tracks": {number: one_line(title) for number, title in tracks.items()
                   if one_line(title)},
        "source": "cdtext",
    }


def _decode(raw: bytes, charset: str) -> str:
    try:
        text = raw.decode(charset)
    except UnicodeDecodeError:
        text = raw.decode(charset, "replace")
    return text.replace("\x00", "").strip()
