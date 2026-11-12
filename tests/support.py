"""Reusable App / engine / ripper / tray stubs for tests."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock

from app import App
from cdda.discmon import TRAY_CLOSED_DISC, TRAY_CLOSED_EMPTY
from cdda.fake import build_fake_disc
from cdda.session import DiscSession
from app_state import TrayState
from dsp.analyzer import VisFrame
from ui.feedback import VisualFeedback


class AppEngine:
    def __init__(self, state="playing"):
        self.state = state
        self.error = ""
        self.stopped = self.paused = self.played = False
        self.stop_playbacks = 0
        self.cued = None
        self.loaded = None
        self.pos = 0.0
        self.volume = 1.0
        self.seeks: list[float] = []

    def position(self) -> float:
        return self.pos

    def stop(self) -> None:
        self.stopped = True
        self.state = "idle"

    def stop_playback(self) -> None:
        self.stop_playbacks += 1
        self.state = "stopped"

    def pause(self) -> None:
        self.paused = True

    def play(self) -> None:
        self.played = True

    def toggle(self) -> None:
        self.played = True
        if self.state == "playing":
            self.state = "paused"
        elif self.state in ("paused", "buffering", "stopped"):
            self.state = "playing"

    def cue(self, media, start_s: float = 0.0) -> None:
        self.cued = (media, start_s)
        self.state = "stopped"

    def load(self, media, start_s: float = 0.0, paused: bool = False) -> None:
        self.loaded = (media, start_s, paused)
        self.state = "paused" if paused else "playing"

    def set_volume(self, value: float) -> None:
        self.volume = value

    def seek_to(self, seconds: float) -> None:
        self.seeks.append(seconds)
        self.pos = seconds

    def seek_rel(self, delta: float) -> None:
        self.seek_to(max(0.0, self.pos + delta))

    def nudge_volume(self, delta: float) -> None:
        pass


class AppRipper:
    def __init__(self, done: set[int] | None = None, events: list[str] | None = None,
                 name: str = "ripper"):
        self.done = done or set()
        self.stopped = self.started = 0
        self.paused = False
        self.pause_calls = self.resumed = 0
        self.wanted: list[int] = []
        self.requests: list[tuple[int, bool]] = []
        self.cancelled = 0
        self.events = events if events is not None else []
        self.name = name
        self.preserve_part = False
        self.avail = 0

    def stop(self) -> None:
        self.stopped += 1
        self.events.append(self.name + ":stop")

    def stop_and_join(self, timeout: float = 10.0,
                      preserve_part: bool = False) -> bool:
        self.stopped += 1
        self.preserve_part = preserve_part
        self.events.append(self.name + ":stop_join")
        return True

    def start(self) -> None:
        self.started += 1
        self.events.append(self.name + ":start")

    def want(self, number: int) -> None:
        self.wanted.append(number)

    def request_priority(self, number: int, immediate: bool = False) -> None:
        self.requests.append((number, immediate))

    def cancel_priority_request(self) -> None:
        self.cancelled += 1

    def pause(self) -> None:
        self.pause_calls += 1
        self.paused = True
        self.cancelled += 1

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            self.resumed += 1

    def is_paused(self) -> bool:
        return self.paused

    def retry(self, number: int) -> None:
        pass

    def is_done(self, number: int) -> bool:
        return number in self.done

    def is_failed(self, number: int) -> bool:
        return False

    def available(self, number: int) -> int:
        return self.avail

    def snapshot(self) -> dict:
        return {}


class AppMonitor:
    def __init__(self, present, disc, tray=None, since=None):
        self.snap = (present, disc)
        if tray is None and present is not None:
            tray = TRAY_CLOSED_DISC if present else TRAY_CLOSED_EMPTY
        self.tray = tray
        self.since = time.monotonic() if since is None else since

    def snapshot(self):
        return self.snap

    def tray_snapshot(self):
        return self.tray, self.since, self.snap[1]


class Keys:
    def __init__(self, keys: list[str]):
        self._keys = list(keys)

    def read(self) -> list[str]:
        return list(self._keys)




class CacheStub:
    """最小缓存接口：App 生命周期测试不需要真的写 PCM。"""

    def __init__(self) -> None:
        self.done: set[int] = set()
        self.purged: list[str] = []
        self.meta: dict = {}

    def ensure_format(self) -> None:
        pass

    def acquire(self, disc) -> bool:
        return True

    def close(self) -> None:
        pass

    def purge_except(self, disc) -> None:
        self.purged.append(disc.discid)

    def repair_incomplete(self, disc) -> None:
        pass

    def is_done(self, disc, number: int) -> bool:
        return number in self.done

    def read_meta(self, disc) -> dict:
        return dict(self.meta)

    def title_of(self, disc, number: int):
        return f"Track {number:02d}", ""

    def write_meta(self, disc, data: dict) -> None:
        self.meta = dict(data)

    def clear_audio(self, disc) -> None:
        pass


def fake_args(**kw) -> SimpleNamespace:
    args = SimpleNamespace(
        cache_dir=None, fake=True, device=None, track=None, speed=8.0,
        no_metadata=True, fast_rip=False, theme=None, layout="dash",
        minimal=False, paused=False, wait_disc=False, volume=0.85,
        rip_mode=None, on_eject=None, on_insert=None,
    )
    for key, value in kw.items():
        setattr(args, key, value)
    return args


def make_app(*, disc=None, engine=None, ripper=None, cache=None,
             insert_action="pause", boot_paused=False, track_no=1,
             disc_present=True, **arg_overrides):
    """构造一个不碰光驱/终端/真实线程的 App fixture。

    测试只需要改自己关心的字段；session/tray/analyzer/engine 都有默认值。
    """
    app = App.__new__(App)
    app.args = fake_args(**arg_overrides)
    app.analyzer = mock.Mock()
    app.volume = app.args.volume
    app.insert_action = insert_action
    app.eject_action = "stop"
    app._boot_paused = boot_paused
    app._boot_pause_prompt = boot_paused or insert_action != "play"
    app.track_no = track_no
    app.disc_present = disc_present
    app.disc_state = "ready" if disc_present else "no_disc"
    app.tray = TrayState(
        tray=TRAY_CLOSED_DISC if disc_present else TRAY_CLOSED_EMPTY,
        since=time.monotonic(), disc_present=disc_present,
        disc_state=app.disc_state,
    )
    app.monitor = None
    app._last_dash_rows = None
    app._ripper_stuck = False
    app._autopaused = False
    app._autopause_since = 0.0
    app.message = ""
    app._message_until = 0.0
    app.hint = ""
    app._spin = "⠋"
    app._spin_i = 0
    app.layout = "dash"
    app.theme_name = "phosphor"
    app.help = False
    app.engine = engine or AppEngine("stopped")
    ripper_obj = ripper or AppRipper()
    cache_obj = cache or CacheStub()
    disc_obj = disc if disc is not None else build_fake_disc(durations=(1.0, 1.0, 1.0))
    app.session = DiscSession(app.args)
    app.session.disc = disc_obj
    app.session.cache = cache_obj
    app.session.ripper = ripper_obj
    app.session.seq = 1
    app._clock = None
    app._pending_seek_target = None
    app._pending_seek_deadline = 0.0
    app._pending_seek_origin = 0.0
    app._pending_seek_track = None
    app._pending_seek_discid = None
    app._frozen_vis = VisFrame()
    app._scope_mix = app._idle_target = app._idle_from = 0.0
    app._idle_t0 = app._idle_start = time.monotonic()
    app._idle_dur = 0.45
    app._idle_t = 0.0
    app._make_ripper = lambda d, c: app.ripper
    app._start_disc_monitor = lambda: None
    app._start_metadata_lookup = mock.Mock()
    app.keys = Keys([])
    app.feedback = VisualFeedback()
    return app
