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
        self.scrolled_pixels: list[tuple[list[list[int]], float]] = []
        self.fills: list[int] = []
        self.flushes = 0

    def set_panel(self, panel, rows, show=True):
        self.panels.append((panel, [list(row) for row in rows]))

    def scroll_text(self, text, colour=0xFFFFFF, speed=1.0, **kwargs):
        self.scrolls.append((text, colour, speed))

    def scroll_pixels(self, rows, *, speed=1.0, **kwargs):
        self.scrolled_pixels.append(([list(row) for row in rows], speed))

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


class FakeScreen:
    """Records what the CPU-temperature chart draws on the LCD."""

    def __init__(self, width: int = 320, height: int = 240) -> None:
        self.max_width = width
        self.max_height = height
        self.rectangles: list[tuple[int, int, int, int, int]] = []
        self.texts: list[tuple[int, int, str]] = []
        self.clears: list[int] = []

    def clear(self, colour: int = 0) -> None:
        self.clears.append(colour)
        self.rectangles.append((0, 0, self.max_width, self.max_height, colour))

    def draw_rectangle(self, x, y, width, height, colour=0xFFFFFF) -> None:
        self.rectangles.append((x, y, width, height, colour))

    def write_text(self, x, y, text, size=1, colour=0xFFFFFF,
                   background_colour=0) -> None:
        self.texts.append((x, y, text))

    @property
    def written(self) -> list[str]:
        return [text for _x, _y, text in self.texts]


class FakeSpeaker:
    """Stands in for the native `Speaker` driver."""

    def __init__(self) -> None:
        self.volume: int | None = None
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)


class FakeCube:
    def __init__(self, modules=None, *, screen=None, speaker=None) -> None:
        self.display = FakeDisplay()
        self.screen = screen
        self.speaker = speaker
        self._modules = dict(modules or {})

    @property
    def modules(self):
        return self._modules


def _clock(modules=None, **kwargs):
    cube = FakeCube(modules, **kwargs)
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
    # The clock scrolls its own 3x5 digits, not the taller built-in font.
    assert cube.display.scrolled_pixels
    rows, speed = cube.display.scrolled_pixels[0]
    assert speed == clock.SCROLL_SPEED
    assert len(rows) == 8
    assert len(rows[0]) == 5 * 4                  # "14:05": 4 columns each
    lit_rows = [index for index, row in enumerate(rows) if any(row)]
    assert lit_rows == [1, 2, 3, 4, 5]            # same rows as the static digits


def test_scroll_font_firmware_uses_the_builtin_font(monkeypatch) -> None:
    monkeypatch.setattr(clock, "SCROLL_FONT", "firmware")
    instance, cube, _curve = _clock()
    instance.scroll_time(datetime.datetime(2026, 10, 23, 14, 5))
    assert cube.display.scrolls
    assert cube.display.scrolled_pixels == []


def test_render_scroll_rows_matches_the_static_digits() -> None:
    colour = 0x00FF00
    rows = clock.render_scroll_rows("12", colour)
    assert len(rows) == 8
    assert len(rows[0]) == 8                       # 2 glyphs of 3 + separator
    # The "1" glyph lights only its third column, on rows 1..5.
    assert rows[1][:3] == [0, 0, colour]
    # The separator column between the two digits is blank.
    assert all(rows[row][3] == 0 for row in range(8))
    # Row 0 and row 7 stay empty, exactly like `render_text`.
    assert not any(rows[0]) and not any(rows[7])


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


def test_bottom_button_draws_the_temperature_chart(monkeypatch) -> None:
    monkeypatch.setattr(clock, "_cpu_temperature_c", lambda: 45.0)
    screen = FakeScreen()
    buttons = FakeModule({"top_pressed_count": 0, "middle_pressed_count": 0,
                          "bottom_pressed_count": 0})
    instance, _cube, _curve = _clock({"buttons": buttons}, screen=screen)
    instance.poll_buttons(datetime.datetime.now())              # baseline
    buttons.values["bottom_pressed_count"] = 1
    instance.poll_buttons(datetime.datetime.now())
    assert screen.clears                              # LCD blanked first
    assert any("CPU TEMPERATURE" in text for text in screen.written)
    assert instance._chart_mode is True


def test_bottom_button_toggles_the_chart_off_again(monkeypatch) -> None:
    monkeypatch.setattr(clock, "_cpu_temperature_c", lambda: 45.0)
    screen = FakeScreen()
    buttons = FakeModule({"bottom_pressed_count": 0})
    instance, _cube, _curve = _clock({"buttons": buttons}, screen=screen)
    now = datetime.datetime.now()
    instance.poll_buttons(now)
    buttons.values["bottom_pressed_count"] = 1
    instance.poll_buttons(now)
    drawn = len(screen.rectangles)
    buttons.values["bottom_pressed_count"] = 2
    instance.poll_buttons(now)
    assert instance._chart_mode is False
    assert len(screen.clears) == 2                    # blanked again
    assert len(screen.rectangles) == drawn + 1        # only the blanking fill


def test_chart_plots_the_sample_history(monkeypatch) -> None:
    temperatures = iter([40.0, 50.0, 45.0])
    monkeypatch.setattr(clock, "_cpu_temperature_c", lambda: next(temperatures))
    screen = FakeScreen()
    instance, _cube, _curve = _clock(screen=screen)
    now = datetime.datetime(2026, 10, 23, 14, 5)
    for _ in range(3):
        instance.sample_temperature(now, force=True)
    assert instance.draw_temperature_chart() is True
    assert any("now 45.0C" in text for text in screen.written)
    assert any("min 40.0C" in text for text in screen.written)
    # One column per sample, plus the frame and the two text rows.
    assert len(screen.rectangles) == 1 + 1 + 4 + 3


def test_chart_without_a_screen_only_logs(caplog) -> None:
    instance, _cube, _curve = _clock()
    with caplog.at_level("INFO"):
        assert instance.draw_temperature_chart() is False
    assert "no screen" in caplog.text


def test_top_button_leaves_chart_mode(monkeypatch) -> None:
    monkeypatch.setattr(clock, "_cpu_temperature_c", lambda: 45.0)
    screen = FakeScreen()
    buttons = FakeModule({"top_pressed_count": 0, "bottom_pressed_count": 0})
    instance, cube, _curve = _clock({"buttons": buttons}, screen=screen)
    now = datetime.datetime(2026, 10, 23, 14, 5)
    instance.poll_buttons(now)
    buttons.values["bottom_pressed_count"] = 1
    instance.poll_buttons(now)
    assert instance._chart_mode is True
    buttons.values["top_pressed_count"] = 1
    instance.poll_buttons(now)
    assert instance._chart_mode is False
    assert cube.display.scrolled_pixels               # the top button still scrolls


def test_say_without_a_speaker_only_logs(caplog) -> None:
    instance, _cube, _curve = _clock()
    with caplog.at_level("INFO"):
        instance.say("hello")
    assert "speaker unavailable" in caplog.text


# ----- speaker driver ----------------------------------------------------


def test_speaker_volume_is_pinned_at_startup(monkeypatch) -> None:
    monkeypatch.setattr(clock, "SPEAKER_VOLUME", 80)
    speaker = FakeSpeaker()
    _clock(speaker=speaker)
    assert speaker.volume == 80


def test_speaker_driver_wins_over_the_telemetry_namespace(monkeypatch) -> None:
    """The clock only subscribes to the sensor board: no `speaker` module."""
    monkeypatch.setattr(clock, "_cpu_temperature_c", lambda: 45.0)
    driver, module = FakeSpeaker(), FakeModule()
    instance, _cube, _curve = _clock({"speaker": module}, speaker=driver)
    instance.say("hello")
    assert driver.said == ["hello"]
    assert module.said == []


# ----- configuration -----------------------------------------------------


def test_apply_config_ignores_unknown_names() -> None:
    class Config:
        SPEAKER_VOLUME = 55
        NOT_A_SETTING = 1

    applied = clock.apply_config(Config(), source="test")
    assert applied == ["SPEAKER_VOLUME"]


def test_config_file_overrides_the_defaults(tmp_path, monkeypatch) -> None:
    for name in clock.CONFIG_NAMES:                  # restored by monkeypatch
        monkeypatch.setattr(clock, name, getattr(clock, name))
    path = tmp_path / "clock_config.py"
    path.write_text("SPEAKER_VOLUME = 42\nADAPT = False\nLOW = 10\nHIGH = 900\n")
    assert clock.load_config(str(path)) == str(path)
    assert clock.SPEAKER_VOLUME == 42
    assert clock.ADAPT is False
    assert (clock.LOW, clock.HIGH) == (10, 900)
    assert clock.build_curve().adapt is False


def test_missing_config_file_is_an_error(tmp_path) -> None:
    with pytest.raises(SystemExit):
        clock.load_config(str(tmp_path / "nope.py"))


def test_sibling_config_is_optional() -> None:
    """`scripts/ambient_clock_config.py` need not exist (only the example)."""
    assert clock.load_config() in (None, clock.DEFAULT_CONFIG)


# ----- light log (calibration data) ---------------------------------------


def test_light_log_appends_a_row_per_status_interval(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(clock, "LIGHT_LOG", str(tmp_path / "light.csv"))
    handle = clock._open_light_log()  # noqa: SLF001
    assert handle is not None
    clock._write_light_row(handle, 118, 0.45, (72.0, 1869.0))  # noqa: SLF001
    clock._write_light_row(handle, None, 0.45, (72.0, 1869.0))  # noqa: SLF001
    handle.close()

    lines = (tmp_path / "light.csv").read_text().splitlines()
    assert lines[0] == "timestamp,ambient,brightness,low,high"
    assert lines[1].endswith(",118,0.450,72.0,1869.0")
    assert lines[2].endswith(",,0.450,72.0,1869.0")   # no reading yet
    # Re-opening appends rather than rewriting the header.
    handle = clock._open_light_log()  # noqa: SLF001
    clock._write_light_row(handle, 120, 0.45, (72.0, 1869.0))  # noqa: SLF001
    handle.close()
    assert len((tmp_path / "light.csv").read_text().splitlines()) == 4


def test_light_log_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setattr(clock, "LIGHT_LOG", "")
    assert clock._open_light_log() is None  # noqa: SLF001
    clock._write_light_row(None, 100, 0.5, (10.0, 20.0))  # noqa: SLF001


def test_light_log_failure_does_not_stop_the_clock(tmp_path, monkeypatch, caplog) -> None:
    target = tmp_path / "nope" / "light.csv"
    target.parent.write_text("not a directory")          # mkdir will fail
    monkeypatch.setattr(clock, "LIGHT_LOG", str(target))
    with caplog.at_level("WARNING"):
        assert clock._open_light_log() is None  # noqa: SLF001
    assert "light log" in caplog.text


# ----- hourly chime ------------------------------------------------------


def test_hourly_chime_scrolls_and_speaks_once_per_hour(monkeypatch) -> None:
    monkeypatch.setattr(clock, "HOURLY_SCROLL", True)
    monkeypatch.setattr(clock, "HOURLY_SPEAK", True)
    speaker = FakeModule()
    instance, cube, _curve = _clock({"speaker": speaker})
    at_the_hour = datetime.datetime(2026, 10, 23, 14, 0, 1)
    instance.hourly_chime(at_the_hour)
    instance.hourly_chime(at_the_hour)          # same hour: no repeat
    assert len(cube.display.scrolled_pixels) == 1
    assert speaker.said == ["it's 14 o'clock"]
    instance.hourly_chime(datetime.datetime(2026, 10, 23, 15, 0, 1))
    assert len(cube.display.scrolled_pixels) == 2


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


