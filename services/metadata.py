"""元数据：MusicBrainz disc ID 查询（后台线程跑，不挡播放）。

disc ID 按 libdiscid 的算法在本地算：首轨号 + 末轨号，接 100 个 4 字节槽位
（第 0 个是 lead-out，随后是各轨 LBA+150，不够的补 0），取 SHA1 再转成
MusicBrainz 的 base64 变体（`+/=` → `._-`）。拿到 ID 再查
`/ws/2/discid/<id>`，是精确命中。

刻录盘/翻录盘的起始 LBA 常和原版压制盘不同，精确 ID 查不到；这时回退到
`/ws/2/discid/-?toc=...`，但会逐轨比对时长（±1 s），只接受整张都对得上的
release。以前直接用它、再按「日文曲名多者优先」挑，曾把整张盘配到别人的
CD 上 —— 所以回退必须带这条时长校验。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from cdda.device import Disc
from config import META_VERSION, PROJECT_URL, VERSION
from textfmt import one_line

# MusicBrainz 要求带版本和联系方式的 User-Agent，否则会被限流甚至封号。
USER_AGENT = f"asciicd/{VERSION} ( {PROJECT_URL} )"
API = "https://musicbrainz.org/ws/2/discid/"

# 官方限流是 1 req/s。所有请求都从这里过一道闸：后台线程可能因为快速换盘
# 同时发起查询，只在某一处 sleep 挡不住重叠的请求。
_MIN_REQUEST_INTERVAL_S = 1.0
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _wait_for_slot() -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _MIN_REQUEST_INTERVAL_S - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()

_SLOTS = 100          # libdiscid 的槽位数：1 个 lead-out + 最多 99 轨

# 刻录盘/翻录盘的 TOC 起始 LBA 常和原版压制盘差几十个扇区，精确 disc ID
# 因此查不到；但每轨时长几乎不变。回退到 TOC 模糊查询时，只有整张专辑逐轨
# 时长都落在容差内才采用 —— 这是不退回「配到别人专辑」老 bug 的保险。
TOC_TOLERANCE_MS = 1000
# 多个候选都过容差时按 (最大单轨误差, 平均误差) 排序；不同曲目的候选差距
# 小于这个值就算「难分伯仲」，宁可回到 Track NN 也不赌。
TOC_AMBIGUITY_MS = 500
# TOC 是推断结果（match=toc），隔一段时间重查一次；精确命中不需要。
TOC_RECHECK_DAYS = 30


def disc_id(disc: Disc) -> str:
    """按 libdiscid 算法算 MusicBrainz disc ID（28 字符）。"""
    if not disc.tracks:
        raise ValueError("空 TOC 算不出 disc id")
    slots = [disc.leadout_lba + 150]
    slots += [t.start_lba + 150 for t in disc.tracks]
    slots += [0] * max(0, _SLOTS - len(slots))
    raw = "%02X%02X" % (disc.tracks[0].number, disc.tracks[-1].number)
    raw += "".join("%08X" % s for s in slots)
    digest = hashlib.sha1(raw.encode("ascii")).digest()
    return (base64.b64encode(digest).decode("ascii")
            .replace("+", ".").replace("/", "_").replace("=", "-"))


def fetch(disc: Disc, timeout: float = 15.0) -> dict | None:
    """先按精确 disc ID 查；刻录盘查不到再回退到 TOC 模糊查询。

    精确查询能命中时结果最可信；404 表示 MB 没有这个 ID（刻录盘/翻录盘的
    起始 LBA 和原版不同），此时用 TOC 找近似盘，但候选必须逐轨时长都对上。
    """
    if not disc.tracks:
        return None
    did = disc_id(disc)
    url = f"{API}{urllib.parse.quote(did)}?inc=artists+recordings&fmt=json"
    try:
        data = _fetch_json(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:                 # 没有这张盘，也没有 CD stub
            raise
        exc.close()
        data = None
    if data:
        got = _pick(data, did, len(disc.tracks))
        if got:
            return got
    # _fetch_json 在请求入口统一节流，这里不再单独 sleep（否则两处叠加）
    return fetch_toc(disc, timeout)


def _fetch_json(url: str, timeout: float) -> dict:
    _wait_for_slot()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _toc_param(disc: Disc) -> str:
    """MusicBrainz 的 toc 参数：首轨号 + 轨数 + lead-out + 各轨偏移（LBA+150）。"""
    parts = [str(disc.tracks[0].number), str(len(disc.tracks)),
             str(disc.leadout_lba + 150)]
    parts += [str(t.start_lba + 150) for t in disc.tracks]
    return "+".join(parts)


def fetch_toc(disc: Disc, timeout: float = 15.0) -> dict | None:
    """按 TOC 模糊查 MB，只接受逐轨时长对得上的 release。"""
    if not disc.tracks:
        return None
    query = urllib.parse.urlencode({
        "toc": _toc_param(disc), "fmt": "json", "inc": "artists+recordings",
    })
    try:
        data = _fetch_json(f"{API}-?{query}", timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return None
        raise
    return _pick_toc(data, disc)


def _durations_error(disc: Disc, medium: dict) -> tuple[float, float] | None:
    """返回 (最大单轨误差, 平均误差) 毫秒；有任一轨超容差/缺时长就 None。

    MB 的 track length 是毫秒，通常按秒取整；1000ms 容差足够吸收取整误差，
    又能把「同轨数但时长不同」的其他专辑挡掉。
    """
    tracks = medium.get("tracks") or []
    if len(tracks) != len(disc.tracks):
        return None
    errors: list[float] = []
    for actual, candidate in zip(disc.tracks, tracks):
        length = candidate.get("length")
        if not isinstance(length, int) or length <= 0:
            return None                     # 没时长就没法验证，宁可不猜
        delta = abs(length - actual.seconds * 1000.0)
        if delta > TOC_TOLERANCE_MS:
            return None
        errors.append(delta)
    if not errors:
        return None
    return max(errors), sum(errors) / len(errors)


def toc_result_needs_recheck(meta: dict, now: float | None = None) -> bool:
    """TOC 模糊匹配的结果隔一段时间重查；精确命中的 meta 永远不重查。"""
    if (meta.get("match") or "") != "toc":
        return False
    try:
        when = time.mktime(time.strptime(meta.get("updated") or "",
                                         "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return True                         # 老 meta 没时间戳：当过期处理
    return (time.time() if now is None else now) - when > TOC_RECHECK_DAYS * 86400


def _titles(tracks: list[dict]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for t in tracks:
        number = str(t.get("number") or "")
        title = one_line(t.get("title") or "")
        if number.isdigit() and title:
            titles[number] = title
    return titles


def _normalize_title(title: str) -> str:
    """只用于比较：全半角、大小写、弯引号、空白差异不算不同曲名。"""
    text = unicodedata.normalize("NFKC", title).casefold()
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"'))
    return " ".join(text.split())


def _title_signature(titles: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(n), _normalize_title(t)) for n, t in titles.items()))


def _release_meta(rel: dict, titles: dict[str, str]) -> dict:
    artist = " / ".join(one_line(a.get("name", ""))
                        for a in rel.get("artist-credit", [])
                        if isinstance(a, dict))
    return {
        "album": one_line(rel.get("title") or ""),
        "artist": one_line(artist),
        "tracks": titles,
        "source": "musicbrainz",
        "release_id": rel.get("id") or "",
        "meta_version": META_VERSION,
    }


def _pick_toc(data: dict, disc: Disc) -> dict | None:
    """在 TOC 模糊结果里挑逐轨时长最接近的 release。

    先按 (最大单轨误差, 平均误差) 排序，误差一样再用官方状态和返回顺序；
    不同曲目的候选若在误差上难分伯仲，直接放弃 —— 宁可只显示 Track NN。
    """
    count = len(disc.tracks)
    candidates: list[tuple[tuple[float, float], int, int, dict, dict[str, str]]] = []
    for order, rel in enumerate(data.get("releases") or []):
        for medium in rel.get("media") or []:
            error = _durations_error(disc, medium)
            if error is None:
                continue
            titles = _titles(medium.get("tracks") or [])
            if len(titles) < count:
                continue
            official = 0 if rel.get("status") == "Official" else 1
            candidates.append((error, official, order, rel, titles))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0][0], c[0][1], c[1], c[2]))
    best_error, _, _, rel, titles = candidates[0]
    best_signature = _title_signature(titles)
    for error, _, _, _, other_titles in candidates[1:]:
        if _title_signature(other_titles) == best_signature:
            continue
        if _near_tie(best_error, error):
            return None
    meta = _release_meta(rel, titles)
    meta["disc_id"] = disc_id(disc)
    meta["match"] = "toc"
    return meta


def _near_tie(best: tuple[float, float], other: tuple[float, float]) -> bool:
    """两个 (最大误差, 平均误差) 是否落在歧义区间内。"""
    return (abs(other[0] - best[0]) <= TOC_AMBIGUITY_MS
            and abs(other[1] - best[1]) <= TOC_AMBIGUITY_MS)


def _medium_for(rel: dict, did: str, count: int) -> dict | None:
    """在这张 release 里找到属于这个 disc ID、且轨数对得上的那一张媒介。

    只认 discs 里显式写着目标 ID 的 medium；多碟 release 里「没有 disc 信息」
    的 medium 不能抢在真正匹配的那张前面。只有整张 release 都没有 disc 信息
    （CD stub / 精简响应）时才退而求其次，按轨数选第一张。
    """
    media = rel.get("media") or []
    candidates = [m for m in media if len(m.get("tracks") or []) == count]
    for medium in candidates:
        if any(d.get("id") == did for d in (medium.get("discs") or [])):
            return medium
    # 注意要扫「全部 medium」而不是只扫轨数相同的：目标 disc ID 可能挂在
    # 另一张轨数不符的 medium 上，那说明这张盘不是这个 release，不能拿
    # 没 disc 信息的那张来顶。
    if any((m.get("discs") or []) for m in media):
        return None
    return candidates[0] if candidates else None


def _pick(data: dict, did: str, count: int) -> dict | None:
    """同一 disc ID 可能挂着首版/再版/限定版，优先官方版，再按返回顺序。"""
    best: tuple[tuple[int, int], dict, dict[str, str]] | None = None
    for order, rel in enumerate(data.get("releases") or []):
        medium = _medium_for(rel, did, count)
        if medium is None:
            continue
        titles = _titles(medium.get("tracks") or [])
        if len(titles) < count:
            continue
        rank = (0 if rel.get("status") == "Official" else 1, order)
        if best is None or rank < best[0]:
            best = (rank, rel, titles)
    if best is None:
        return None

    _, rel, titles = best
    meta = _release_meta(rel, titles)
    meta["disc_id"] = did
    meta["match"] = "discid"
    return meta
