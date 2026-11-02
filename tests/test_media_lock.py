"""The display has one owner at a time — and never while the speaker plays.

A scroll sends only the pixels it changes, so a frame from another writer that
lands in the middle of one is not painted over: the digits sit on top of the
animation until it ends. And a scroll running alongside speech starved the
audio outright (6.84 s of speech took 6.89 s alone against 30.99 s with a
scroll, 154 gaps over the firmware's 63 ms buffer). Both are the same fix: one
owner at a time, in the daemon, where every writer meets.

These drive a real `ControlServer` over a real socket, so the op codes and the
`busy` refusals are exercised end to end.
"""

from __future__ import annotations

import threading
import time

import pytest

from pylumicube.control import (AUDIO_WAIT_FOR_DISPLAY, ControlClient,
                                ControlError, ControlServer)

from .test_control_protocol import FakeCube


class SlowDisplay:
    """A matrix that animates for a while and honours `should_stop`."""

    def __init__(self, seconds: float = 2.0) -> None:
        self.seconds = seconds
        self.scrolled: list[tuple] = []
        self.stopped = False
        self.frames = 0
        self.ended_at: float | None = None

    def scroll_pixels(self, rows, *, speed: float = 1.0, with_gap: bool = True,
                      background_colour: int = 0, times: int = 1,
                      timeout: float = 2.0, should_stop=None) -> bool:
        self.scrolled.append((rows, speed, times))
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                self.stopped = True
                self.ended_at = time.monotonic()
                return False
            self.frames += 1
            time.sleep(0.01)
        self.ended_at = time.monotonic()
        return True

    def scroll_text(self, text, colour=0xFFFFFF, background_colour=0x000000,
                    speed: float = 1.0, with_gap: bool = True, *,
                    times: int = 1, timeout: float = 2.0,
                    should_stop=None) -> bool:
        return self.scroll_pixels([[colour]], speed=speed, times=times,
                                  timeout=timeout, should_stop=should_stop)

    def flush(self, timeout: float | None = None) -> None:
        pass


class DisplayCube(FakeCube):
    def __init__(self, display: SlowDisplay) -> None:
        super().__init__()
        self.display = display


@pytest.fixture
def server(tmp_path):
    display = SlowDisplay()
    control = ControlServer(DisplayCube(display), str(tmp_path / "control.sock"))
    control.start()
    client = ControlClient(control.path).connect()
    try:
        yield control, client, display
    finally:
        client.close()
        control.stop()


def test_the_animation_is_one_request_that_reports_what_it_did(server) -> None:
    control, client, display = server
    reply = client.request("animate", rows=[[0xFF0000] * 4], speed=2.0, times=1,
                           timeout=30.0)
    assert reply["ok"] is True
    assert reply["aborted"] is False
    assert reply["seconds"] > 0
    assert display.scrolled and display.scrolled[0][1] == 2.0


def test_a_frame_write_is_refused_while_an_animation_owns_the_display(server) -> None:
    """The caller at fault waits — the animation cannot paint over its frame."""
    control, client, display = server
    display.seconds = 5.0
    started_animating = threading.Event()

    def animate() -> None:
        client.request("animate", rows=[[0xFF0000] * 4], max_seconds=10.0,
                       timeout=30.0)

    thread = threading.Thread(target=animate)
    thread.start()
    try:
        while not control._animating.is_set():        # noqa: SLF001
            time.sleep(0.01)
        started_animating.set()
        with ControlClient(control.path).connect() as other:
            started = time.monotonic()
            with pytest.raises(ControlError) as error:
                other.request("write", module="display",
                              values={"led_colour_0": 0xFF})
            assert error.value.code == "busy"
            assert time.monotonic() - started < 0.9, "the frame waited too long"
        control._animation_stop.set()                 # noqa: SLF001
    finally:
        control._animation_stop.set()                 # noqa: SLF001
        thread.join(timeout=10.0)


def test_an_announcement_waits_for_the_animation_to_finish(server) -> None:
    """Strict both ways: the voice waits for the scroll, never overlaps it."""
    control, client, display = server
    display.seconds = 0.6
    said_at: list[float] = []
    original = control.cube.say

    def timed_say(text, **kwargs):
        said_at.append(time.monotonic())
        return original(text, **kwargs)

    control.cube.say = timed_say

    def animate() -> None:
        client.request("animate", rows=[[0x00FF00] * 4], timeout=30.0)

    thread = threading.Thread(target=animate)
    thread.start()
    try:
        while not control._animating.is_set():        # noqa: SLF001
            time.sleep(0.01)
        with ControlClient(control.path).connect() as other:   # its own connection
            other.request("say", text="hello")
    finally:
        thread.join(timeout=5.0)
    assert said_at, "the announcement never ran"
    assert display.ended_at is not None, "the animation never finished"
    assert said_at[0] >= display.ended_at, "the announcement talked over a scroll"
    assert display.stopped is False, "the announcement cut a short scroll short"


def test_a_wedged_animation_is_cut_short_for_an_announcement(server,
                                                            monkeypatch) -> None:
    """Past the cap the *scroll* yields: an announcement is never dropped."""
    monkeypatch.setattr("pylumicube.control.AUDIO_WAIT_FOR_DISPLAY", 0.15)
    control, client, display = server
    display.seconds = 10.0
    aborted: list[dict] = []

    def animate() -> None:
        aborted.append(client.request("animate", rows=[[0x0000FF] * 4],
                                      max_seconds=8.0, timeout=30.0))

    thread = threading.Thread(target=animate)
    thread.start()
    try:
        while not control._animating.is_set():        # noqa: SLF001
            time.sleep(0.01)
        started = time.monotonic()
        with ControlClient(control.path) as other:
            other.request("say", text="now")
        waited = time.monotonic() - started
    finally:
        thread.join(timeout=10.0)
    assert waited < 3.0, f"the announcement waited {waited:.1f} s"
    assert display.stopped is True, "the animation was not stopped"
    assert aborted and aborted[0]["aborted"] is True
    assert aborted[0]["aborted_by"] == "audio"


def test_the_speaker_holds_the_stage_so_a_scroll_waits(server) -> None:
    control, client, display = server
    holding = threading.Event()

    def speak() -> None:
        control.media_lock.acquire()
        holding.set()
        try:
            time.sleep(0.3)
        finally:
            control.media_lock.release()

    thread = threading.Thread(target=speak)
    thread.start()
    try:
        assert holding.wait(timeout=2.0)
        started = time.monotonic()
        reply = client.request("animate", rows=[[0xFFFFFF] * 4],
                               max_seconds=5.0, timeout=30.0)
        assert reply["ok"] is True
        assert time.monotonic() - started >= 0.25, "the scroll did not wait"
        assert display.stopped is False, "the scroll gave up although it waited"
    finally:
        thread.join(timeout=3.0)


def test_an_animation_gives_up_when_the_speaker_never_finishes(server) -> None:
    control, client, _display = server
    control.media_lock.acquire()
    try:
        with pytest.raises(ControlError) as error:
            client.request("animate", rows=[[0xFFFFFF] * 4], max_seconds=0.2,
                           timeout=10.0)
        assert error.value.code == "busy"
    finally:
        control.media_lock.release()


def test_stats_says_who_holds_the_display(server) -> None:
    """`media` is how a caller looks before it blocks (`RLock.locked()` is
    Python 3.14 only, so the state is tracked with an Event)."""
    control, client, _display = server
    assert client.request("stats")["media"] == {
        "animating": False, "animating_for": 0.0, "audio_busy": False}

    control.cube.say_delay = 0.6
    thread = threading.Thread(
        target=lambda: ControlClient(control.path).connect().request(
            "say", text="busy"))
    thread.start()
    try:
        while not control._audio_busy.is_set():        # noqa: SLF001
            time.sleep(0.02)
        busy = client.request("stats")["media"]
        assert busy["audio_busy"] is True and busy["animating"] is False
    finally:
        thread.join(timeout=5.0)
    assert client.request("stats")["media"]["audio_busy"] is False


def test_the_wait_cap_leaves_room_for_a_scroll() -> None:
    """A scroll is ~13 s at the defaults; the cap is what "never drop an
    announcement" costs, so it has to be comfortably longer than one scroll."""
    assert 13.0 < AUDIO_WAIT_FOR_DISPLAY <= 60.0
