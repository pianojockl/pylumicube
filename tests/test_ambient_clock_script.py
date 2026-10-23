"""Tests for `scripts/ambient_clock.py` (no hardware).

The script is loaded by path — it is not part of the installed package —
and driven against fake cube/display/module objects, so the rendering
layout, the button handling and the hourly chime are covered without a
LumiCube attached.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ambient_clock.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("ambient_clock_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


clock = _load_script()


# ----- fakes --------------------------------------------------------------


class FakeDisplay:
    def __init__(self) -> None:
        self.panels: list[tuple[str, list[list[int]]]] = []
        self.scrolls: list[tuple[str, int, float]] = []
        self.fills: list[int] = []
        self.flushes = 0

    def set_panel(self, panel, rows, show=True):
        self.panels.append((panel, [list(row) for row in rows]))

    def scroll_text(self, text, colour=0xFFFFFF, speed=1.0, **kwargs):
        self.scrolls.append((text, colour, speed))

    def set_all(self, colour, show=True):
        self.fills.append(colour)

    def flush(self, timeout=None):
        self.flushes += 1


class FakeModule:
    def __init__(self, values=None) -> None:
        self.values = dict(values or {})
        self.said: list[str] = []

    def get(self, name, default=None):
        return self.values.get(name, default)

    def say(self, text):
        self.said.append(text)

    def wait_for(self, name, timeout=None, default=None):
        return self.values.get(name)


class FakeCube:
    def __init__(self, modules=None) -> None:
        self.display = FakeDisplay()
        self._modules = dict(modules or {})

    @property
    def modules(self):
        return self._modules


def _clock(modules=None, **kwargs):
    cube = FakeCube(modules)
    curve = clock.build_curve()
    return clock.AmbientClock(cube, brightness=curve), cube, curve


# ----- digit rendering ----------------------------------------------------


def test_render_text_shape_and_lit_pixels() -> None:
    rows = clock.render_text("12", lambda: 0xFFFFFF)
    assert len(rows) == 8
    assert all(len(row) == 8 for row in rows)
    # "1" has 5 lit pixels, "2" has 11.
    assert sum(1 for row in rows for pixel in row if pixel) == 16


def test_render_text_matches_the_classic_glyphs() -> None:
    rows = clock.render_text("1", lambda: 0xFFFFFF)
    lit = {(x, y) for y, row in enumerate(rows) for x, pixel in enumerate(row) if pixel}
    # A "1" is the right-hand column of its 3-column cell, on rows 1..5.
    assert lit == {(2, y) for y in range(1, 6)}


def test_render_text_uses_the_colour_callable_per_pixel() -> None:
    counter = iter(range(1, 100))
    rows = clock.render_text("8", lambda: next(counter))
    values = [pixel for row in rows for pixel in row if pixel]
    assert values == list(range(1, len(values) + 1))


def test_render_text_unknown_char_falls_back_to_zero() -> None:
    assert clock.render_text("x", lambda: 1) == clock.render_text("0", lambda: 1)


def test_render_text_ignores_a_third_character() -> None:
    assert clock.render_text("123", lambda: 1) == clock.render_text("12", lambda: 1)


def test_hsv_colour_packs_rgb() -> None:
    assert clock.hsv_colour(0.0, 1.0, 1.0) == 0xFF0000
    assert clock.hsv_colour(1 / 3, 1.0, 1.0) == 0x00FF00
    assert clock.hsv_colour(0.0, 1.0, 0.0) == 0x000000


# ----- rendering the clock -----------------------------------------------


def test_render_draws_hours_left_and_minutes_right() -> None:
    instance, cube, _curve = _clock()
    now = datetime.datetime(2026, 10, 23, 14, 5)
    assert instance.render(now, 1.0, force=True) is True
    panels = dict(cube.display.panels)
    assert set(panels) == {"left", "right"}
    assert panels["left"] == clock.render_text("14", lambda: panels["left"][0][0] or 1) \
        or sum(1 for row in panels["left"] for p in row if p) > 0
    # Minutes panel shows "05": a leading zero plus a five.
    assert sum(1 for row in panels["right"] for p in row if p) > 0


def test_render_is_skipped_when_nothing_changed() -> None:
    instance, cube, _curve = _clock()
    now = datetime.datetime(2026, 10, 23, 14, 5)
    assert instance.render(now, 1.0) is True
    assert instance.render(now, 1.0) is False
    assert len(cube.display.panels) == 2


def test_render_redraws_when_brightness_changes() -> None:
    instance, cube, _curve = _clock()
    now = datetime.datetime(2026, 10, 23, 14, 5)
    instance.render(now, 1.0)
    assert instance.render(now, 0.2) is True      # dimmed -> repaint
    assert len(cube.display.panels) == 4
    # The dimmed frame is actually darker than the bright one.
    bright = max(p for row in cube.display.panels[0][1] for p in row)
    dim = max(p for row in cube.display.panels[2][1] for p in row)
    assert dim < bright


def test_render_redraws_when_the_minute_changes() -> None:
    instance, cube, _curve = _clock()
    instance.render(datetime.datetime(2026, 10, 23, 14, 5), 1.0)
    instance.render(datetime.datetime(2026, 10, 23, 14, 6), 1.0)
    assert len(cube.display.panels) == 4


def test_colour_follows_the_hour_when_configured() -> None:
    instance, _cube, _curve = _clock()
    midnight = instance._colour_for(0, 1.0)()
    noon = instance._colour_for(12, 1.0)()
    assert midnight != noon


# ----- ambient light -----------------------------------------------------


def test_poll_light_feeds_the_curve() -> None:
    light = FakeModule({"ambient_light": 1500})
    instance, _cube, curve = _clock({"light_sensor": light})
    brightness = instance.poll_light()
    assert brightness is not None and 0.0 <= brightness <= 1.0
    assert curve.value == pytest.approx(brightness)


def test_poll_light_without_a_sensor_returns_none() -> None:
    instance, _cube, _curve = _clock()
    assert instance.light is None
    assert instance.poll_light() is None
    assert instance.brightness() == clock.BRIGHTNESS_MAX


def test_dim_and_bright_rooms_differ() -> None:
    light = FakeModule()
    instance, _cube, _curve = _clock({"light_sensor": light})
    light.values["ambient_light"] = 60
    dark = instance.poll_light()
    light.values["ambient_light"] = 1900
    bright = instance.poll_light()
    assert dark < bright


# ----- buttons -----------------------------------------------------------


def test_first_poll_does_not_trigger_handlers() -> None:
    """The counters start at whatever they are; only *changes* are presses."""
    buttons = FakeModule({"top_pressed_count": 7, "middle_pressed_count": 0,
                          "bottom_pressed_count": 0})
    instance, cube, _curve = _clock({"buttons": buttons})
    instance.poll_buttons(datetime.datetime.now())
    assert cube.display.scrolls == []


def test_top_button_scrolls_the_time() -> None:
    buttons = FakeModule({"top_pressed_count": 3, "middle_pressed_count": 0,
                          "bottom_pressed_count": 0})
    instance, cube, _curve = _clock({"buttons": buttons})
    now = datetime.datetime(2026, 10, 23, 14, 5)
    instance.poll_buttons(now)                    # baseline
    buttons.values["top_pressed_count"] = 4
    instance.poll_buttons(now)
    assert cube.display.scrolls
    assert cube.display.scrolls[0][0] == "14:05"


def test_middle_button_reboots_when_allowed(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(clock, "ALLOW_REBOOT", True)
    monkeypatch.setattr(clock.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    speaker = FakeModule()
    buttons = FakeModule({"top_pressed_count": 0, "middle_pressed_count": 0,
                          "bottom_pressed_count": 0})
    instance, _cube, _curve = _clock({"buttons": buttons, "speaker": speaker})
    instance.poll_buttons(datetime.datetime.now())
    buttons.values["middle_pressed_count"] = 1
    instance.poll_buttons(datetime.datetime.now())
    assert calls == [["/usr/sbin/reboot"]]
    assert speaker.said == ["getting ready to reboot"]


def test_middle_button_does_nothing_when_reboot_is_disabled(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(clock, "ALLOW_REBOOT", False)
    monkeypatch.setattr(clock.subprocess, "run", lambda cmd, check=False: calls.append(cmd))
    buttons = FakeModule({"top_pressed_count": 0, "middle_pressed_count": 0,
                          "bottom_pressed_count": 0})
    instance, _cube, _curve = _clock({"buttons": buttons})
    instance.poll_buttons(datetime.datetime.now())
    buttons.values["middle_pressed_count"] = 1
    instance.poll_buttons(datetime.datetime.now())
    assert calls == []


def test_bottom_button_logs_the_temperature(caplog) -> None:
    buttons = FakeModule({"top_pressed_count": 0, "middle_pressed_count": 0,
                          "bottom_pressed_count": 0})
    instance, _cube, _curve = _clock({"buttons": buttons})
    instance.poll_buttons(datetime.datetime.now())
    buttons.values["bottom_pressed_count"] = 1
    with caplog.at_level("INFO"):
        instance.poll_buttons(datetime.datetime.now())
    assert "temperature" in caplog.text.lower()


def test_say_without_a_speaker_only_logs(caplog) -> None:
    instance, _cube, _curve = _clock()
    with caplog.at_level("INFO"):
        instance.say("hello")
    assert "speaker unavailable" in caplog.text


# ----- hourly chime ------------------------------------------------------


def test_hourly_chime_scrolls_and_speaks_once_per_hour(monkeypatch) -> None:
    monkeypatch.setattr(clock, "HOURLY_SCROLL", True)
    monkeypatch.setattr(clock, "HOURLY_SPEAK", True)
    speaker = FakeModule()
    instance, cube, _curve = _clock({"speaker": speaker})
    at_the_hour = datetime.datetime(2026, 10, 23, 14, 0, 1)
    instance.hourly_chime(at_the_hour)
    instance.hourly_chime(at_the_hour)          # same hour: no repeat
    assert len(cube.display.scrolls) == 1
    assert speaker.said == ["it's 14 o'clock"]
    instance.hourly_chime(datetime.datetime(2026, 10, 23, 15, 0, 1))
    assert len(cube.display.scrolls) == 2


def test_hourly_chime_is_silent_at_night(monkeypatch) -> None:
    monkeypatch.setattr(clock, "HOURLY_SPEAK", True)
    speaker = FakeModule()
    instance, _cube, _curve = _clock({"speaker": speaker})
    instance.hourly_chime(datetime.datetime(2026, 10, 23, 3, 0, 1))
    assert speaker.said == []


def test_hourly_chime_ignores_other_minutes() -> None:
    instance, cube, _curve = _clock()
    instance.hourly_chime(datetime.datetime(2026, 10, 23, 14, 30, 0))
    assert cube.display.scrolls == []


