#!/usr/bin/env python3
"""Ambient-lit digital clock for the LumiCube — native pylumicube API.

A fresh rewrite of the classic community clock (the `main.py` that lives in
the old Java daemon's script directory), driving `pylumicube` directly:

  * hours on the left panel, minutes on the right — 3x5 digits, hue follows
    the hour, per-pixel shimmer like the original;
  * **brightness follows `light_sensor.ambient_light`** through an
    auto-ranging curve (`pylumicube.dimming`), so the clock dims in a dark
    room instead of always burning at full brightness;
  * buttons: top = scroll the current time, middle = reboot (configurable),
    bottom = CPU-temperature chart on the secondary LCD (press again, or any
    other button, to go back to a blank LCD);
  * top of the hour: scroll the time, and speak it if a speaker is
    available.

Unlike the community scripts in `scripts/original/`, this one does not use
the compat shim: it opens the cube through `pylumicube.LumiCube` with
telemetry enabled and reads sensors through `cube.light_sensor`.

Run it directly:

    python scripts/ambient_clock.py
    # or, on the Pi, under the venv/uv env that has pylumicube installed:
    uv run python scripts/ambient_clock.py

Configuration lives in `scripts/ambient_clock_config.py` (copy the
`.example`), or in any file passed as `--config PATH` — which is how the
systemd unit keeps its settings outside the git checkout. Without a config
file the defaults below apply.
"""

from __future__ import annotations

import argparse
import colorsys
import datetime
import importlib.util
import logging
import os
import random
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pylumicube import LumiCube  # noqa: E402
from pylumicube.compat import open_or_use_hosted  # noqa: E402
from pylumicube.constants import SERIAL_DEVICE  # noqa: E402
from pylumicube.control import ControlError  # noqa: E402
from pylumicube.dimming import BrightnessCurve, scale_colour  # noqa: E402

#: Control errors that mean the link itself is gone, so the client should
#: reconnect rather than merely retry: the daemon restarted, or is not up yet.
_LINK_GONE_CODES = frozenset({"closed", "no_daemon"})

log = logging.getLogger("ambient_clock")

# The sensor board owns the light sensor and the buttons.
SENSOR_NODE_NAME = "button_and_light_sensor"


# --------------------------------------------------------------------------
# Configuration — defaults, overridden by ambient_clock_config.py if present
# --------------------------------------------------------------------------

BRIGHTNESS_MIN = 0.02      # darkest the display ever gets
BRIGHTNESS_MAX = 1.0       # brightest
GAMMA = 1.0                # <1 brightens mid-tones, >1 darkens them
SMOOTHING = 0.25           # 0 = jump to each new value, 0.9 = very slow
ADAPT = True               # let the curve learn the room's light range
LOW = None                 # explicit range (disables auto-ranging when ADAPT=False)
HIGH = None

HUE_FOLLOWS_HOUR = True    # hue cycles through the day; else HUE
HUE = 0.25                 # fixed hue when HUE_FOLLOWS_HOUR is False
SATURATION = 1.0
JITTER = 1.0               # per-pixel hue shimmer, 0 = steady colour

ALLOW_REBOOT = True        # middle button reboots the Pi
HOURLY_SCROLL = True       # scroll the time at the top of each hour
HOURLY_SPEAK = True        # ...and speak it, if a speaker is available
SCROLL_SPEED = 0.7
SCROLL_FONT = "digits"     # "digits" = the clock's own 3x5 glyphs;
                           # "firmware" = the built-in 5x7 font (taller)
SPEAKER_VOLUME = 100       # 0..200; the firmware powers up loud, so set it
DISPLAY_BRIGHTNESS = 255   # the matrix's own brightness (0..255): the firmware
                           # boots at 65, which multiplies with the dimming
                           # below — 0.5 brightness then means 13%, not 50%

# CPU-temperature chart on the secondary LCD (bottom button).
TEMP_SAMPLE_SECONDS = 10   # one sample this often
TEMP_HISTORY = 60          # samples kept (and columns plotted)
CHART_REFRESH = 60.0       # seconds between redraws while the chart is up

#: How often the log gets a line about the ambient reading and brightness —
#: the easiest way to see what the auto-ranging curve is doing.
STATUS_INTERVAL = 300.0

#: Where to append a CSV of (timestamp, ambient, brightness, range) every
#: `STATUS_INTERVAL` — the raw data for calibrating the dimming curve to a
#: room. `None` (or "") disables it.
LIGHT_LOG = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "pylumicube", "light.csv")

#: Settings a config file may override.
CONFIG_NAMES = ("BRIGHTNESS_MIN", "BRIGHTNESS_MAX", "GAMMA", "SMOOTHING", "ADAPT",
                "LOW", "HIGH", "HUE_FOLLOWS_HOUR", "HUE", "SATURATION", "JITTER",
                "ALLOW_REBOOT", "HOURLY_SCROLL", "HOURLY_SPEAK", "SCROLL_SPEED",
                "SCROLL_FONT", "SPEAKER_VOLUME", "DISPLAY_BRIGHTNESS",
                "TEMP_SAMPLE_SECONDS", "TEMP_HISTORY", "CHART_REFRESH",
                "STATUS_INTERVAL", "LIGHT_LOG")

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(_HERE, "ambient_clock_config.py")


def apply_config(config, *, source: str = "<config>") -> list[str]:
    """Copy recognised settings from `config` into this module's globals.

    Everything downstream reads these globals at call time, so applying a
    config works at any point before the clock starts running.
    """
    applied: list[str] = []
    for field in CONFIG_NAMES:
        if hasattr(config, field):
            globals()[field] = getattr(config, field)
            applied.append(field)
    log.info("config %s: applied %s", source, ", ".join(applied) or "nothing")
    return applied


def load_config(path: str | None = None) -> str | None:
    """Load a config file, or the sibling `ambient_clock_config.py` by default.

    Returns the path that was loaded, or None when the default is absent.
    """
    target = path or DEFAULT_CONFIG
    if not os.path.exists(target):
        if path is None:
            return None
        raise SystemExit(f"config file not found: {target}")
    spec = importlib.util.spec_from_file_location("ambient_clock_config", target)
    if spec is None or spec.loader is None:  # pragma: no cover - bad path
        raise SystemExit(f"cannot load config file: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    apply_config(module, source=target)
    return target


# A config file sitting next to this script is picked up automatically; an
# explicit --config is applied on top of it (so the flag wins).
load_config()


# --------------------------------------------------------------------------
# Digit rendering (same 3x5 glyphs as the original community clock)
# --------------------------------------------------------------------------

DIGITS: dict[str, tuple[str, str, str, str, str]] = {
    "0": ("###", "# #", "# #", "# #", "###"),
    "1": ("  #", "  #", "  #", "  #", "  #"),
    "2": ("###", "  #", "###", "#  ", "###"),
    "3": ("###", "  #", "###", "  #", "###"),
    "4": ("# #", "# #", "###", "  #", "  #"),
    "5": ("###", "#  ", "###", "  #", "###"),
    "6": ("###", "#  ", "###", "# #", "###"),
    "7": ("###", "  #", "  #", "  #", "  #"),
    "8": ("###", "# #", "###", "# #", "###"),
    "9": ("###", "# #", "###", "  #", "###"),
    ".": ("   ", "   ", "   ", "   ", "  #"),
    ":": ("   ", " # ", "   ", " # ", "   "),
}

PANEL_SIZE = 8

# LCD chart colours (the screen wants 0xRRGGBB and converts to RGB565 itself).
BLACK = 0x000000
WHITE = 0xFFFFFF
TITLE_BAND = 0x101820
AXIS_COLOUR = 0x405060
LINE_COLOUR = 0x00C0FF
HOT_COLOUR = 0xFF8000
TITLE_HEIGHT = 40


def hsv_colour(hue: float, saturation: float, value: float) -> int:
    """Pack an HSV colour into 0xRRGGBB (same rounding as upstream)."""
    hue = hue % 1.0
    saturation = max(0.0, min(1.0, saturation))
    value = max(0.0, min(1.0, value))
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def render_text(text: str, colour_for: Callable[[], int]) -> list[list[int]]:
    """Render up to two characters into an 8x8 grid of colours.

    Layout matches the original community clock exactly: each glyph is
    3 columns wide followed by a blank separator column, and the text sits
    on rows 1..5 with blank rows above and below. ``colour_for`` is called
    once per lit pixel so callers can vary the colour (the original uses
    that for its hue shimmer).
    """
    rows: list[list[int]] = [[0] * PANEL_SIZE for _ in range(PANEL_SIZE)]
    for row_index in range(5):
        column = 0
        for char in text[:2]:
            glyph = DIGITS.get(char, DIGITS["0"])
            for bit in glyph[row_index]:
                if column < PANEL_SIZE and bit == "#":
                    rows[row_index + 1][column] = colour_for()
                column += 1
            column += 1  # separator, also after the final digit
    return rows


def render_scroll_rows(text: str, colour: int) -> list[list[int]]:
    """Render `text` as an 8-row bitmap for `Display.scroll_pixels`.

    Uses the clock's own 3x5 digits, in the same rows (1..5) and with the
    same 1-column separators as the static display, so scrolling shows the
    digits at the size you are already looking at. Upstream scrolled with a
    taller font (`slkscr.ttf` at size 8) and so did this until it was
    pointed out.
    """
    columns: list[list[int]] = []
    for char in text:
        glyph = DIGITS.get(char, DIGITS["0"])
        for column_index in range(3):
            column = [0] * PANEL_SIZE
            for row_index in range(5):
                if glyph[row_index][column_index] == "#":
                    column[row_index + 1] = colour
            columns.append(column)
        columns.append([0] * PANEL_SIZE)          # separator
    rows = [[column[row_index] for column in columns]
            for row_index in range(PANEL_SIZE)]
    return rows


# --------------------------------------------------------------------------
# The clock
# --------------------------------------------------------------------------


class AmbientClock:
    """Renders the time and tracks ambient light on an open cube."""

    def __init__(self, cube: LumiCube, *, brightness: BrightnessCurve) -> None:
        self.cube = cube
        self.curve = brightness
        self._rendered: tuple[str, str, int] | None = None
        self._last_counts: dict[str, int] = {}
        self._last_hour_chime: int | None = None
        self._last_raw: int | None = None
        self.light = self._find_module("light_sensor")
        self.buttons = self._find_module("buttons")
        # The speaker is a driver on the cube board, *not* a telemetry module:
        # the clock only subscribes to the sensor board, so a namespace lookup
        # would silently degrade every announcement to a log line.
        self._speaker = getattr(cube, "speaker", None) or self._find_module("speaker")
        self._screen = getattr(cube, "screen", None)
        self._temperatures: deque[float] = deque(maxlen=TEMP_HISTORY)
        self._last_temp_sample: datetime.datetime | None = None
        self._chart_mode = False
        self._chart_refresh_at: datetime.datetime | None = None
        self._configure_speaker()
        self._configure_display()

    def _configure_display(self) -> None:
        """Set the matrix's own brightness, which the dimming multiplies with.

        The firmware boots it at 65/255, so a per-pixel brightness of 0.5
        lands at 13% — dim enough to look broken in a dark room. Setting it
        here (once, at startup) keeps the ambient response as the only
        thing varying.
        """
        if DISPLAY_BRIGHTNESS is None:
            return
        try:
            self.cube.display.set_brightness(int(DISPLAY_BRIGHTNESS))
            log.info("display brightness set to %s", DISPLAY_BRIGHTNESS)
        except Exception:  # noqa: BLE001 — the clock still tells the time
            log.warning("could not set the display brightness", exc_info=True)

    def _configure_speaker(self) -> None:
        """Pin the volume: the firmware powers up at full blast."""
        if self._speaker is None or SPEAKER_VOLUME is None:
            return
        try:
            self._speaker.volume = SPEAKER_VOLUME
            log.info("speaker volume set to %s", SPEAKER_VOLUME)
        except Exception:  # noqa: BLE001 — audio is never worth failing over
            log.warning("could not set the speaker volume", exc_info=True)

    def reapply_settings(self) -> None:
        """Re-assert the startup settings after the daemon came back.

        The matrix brightness lives in the cube's firmware, so it survives a
        daemon restart — but the speaker volume lives in the daemon, which
        comes back at its own default. Re-applying ours keeps a restart from
        being audible.
        """
        self._configure_display()
        self._configure_speaker()

    # ---------------- module access ----------------

    def _find_module(self, name: str):
        try:
            return self.cube.modules.get(name)
        except Exception:  # noqa: BLE001 — telemetry may be unavailable
            log.debug("no module namespace; %r unavailable", name, exc_info=True)
            return None

    # ---------------- ambient light ----------------

    def poll_light(self) -> float | None:
        """Feed the newest ambient reading to the curve; returns brightness."""
        if self.light is None:
            return None
        raw = self.light.get("ambient_light")
        if raw is None:
            return None
        self._last_raw = raw
        return self.curve.update(raw)

    @property
    def last_raw(self) -> int | None:
        """The most recent raw `ambient_light` reading, if there was one."""
        return self._last_raw

    def brightness(self) -> float:
        value = self.curve.value
        return BRIGHTNESS_MAX if value is None else value

    # ---------------- rendering ----------------

    def _colour_for(self, hour: int, brightness: float) -> Callable[[], int]:
        base_hue = (hour / 24.0) if HUE_FOLLOWS_HOUR else HUE

        def colour() -> int:
            hue = base_hue
            if JITTER > 0:
                # The original's shimmer: hue scaled by a random factor in
                # [1/3, 1], recomputed per pixel.
                delta = (1.5 - random.randint(0, 20) / 20.0) / 1.5
                hue = base_hue * (1.0 - JITTER + JITTER * delta)
            return scale_colour(hsv_colour(hue, SATURATION, 1.0), brightness)

        return colour

    def render(self, now: datetime.datetime, brightness: float,
               *, force: bool = False) -> bool:
        """Draw the current time. Returns True if anything was written."""
        hours = now.strftime("%H")
        minutes = now.strftime("%M")
        quantised = int(brightness * 100)
        state = (hours, minutes, quantised)
        if not force and state == self._rendered:
            return False
        colour_for = self._colour_for(now.hour, brightness)
        try:
            self.cube.display.set_panel("left", render_text(hours, colour_for))
            self.cube.display.set_panel("right", render_text(minutes, colour_for))
        except Exception:  # noqa: BLE001 — a dropped frame is cosmetic
            # Deliberately not recorded as rendered: the next poll retries.
            log.warning("could not draw the time; will retry", exc_info=True)
            return False
        self._rendered = state
        return True

    def scroll_time(self, now: datetime.datetime) -> None:
        colour = scale_colour(hsv_colour(0.25, 1.0, 1.0), self.brightness())
        if SCROLL_FONT == "firmware":
            self.cube.display.scroll_text(now.strftime("%H:%M"), colour,
                                          speed=SCROLL_SPEED)
        else:
            self.cube.display.scroll_pixels(
                render_scroll_rows(now.strftime("%H:%M"), colour),
                speed=SCROLL_SPEED)
        self._rendered = None      # the scroll overwrote the digits

    def say(self, text: str) -> None:
        """Speak `text` if a speaker is available (else log and move on).

        `cube.say` is preferred where it exists: through the daemon it holds
        the speaker for the whole utterance, so an announcement from Home
        Assistant cannot talk over the clock.
        """
        atomic = getattr(self.cube, "say", None)
        if atomic is not None:
            try:
                atomic(text)
                return
            except Exception:  # noqa: BLE001 — never let audio break the clock
                log.warning("cube.say failed", exc_info=True)
        if self._speaker is None:
            log.info("speaker unavailable; would have said: %s", text)
            return
        try:
            self._speaker.say(text)          # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — never let audio break the clock
            log.warning("speaker.say failed", exc_info=True)

    # ---------------- buttons ----------------

    def poll_buttons(self, now: datetime.datetime) -> None:
        """Act on button presses, using the firmware's press counters.

        The counters are monotonic, so a press cannot be missed between
        polls (unlike sampling the `*_pressed` level flags).
        """
        if self.buttons is None:
            return
        for name, handler in (("top", self._on_top),
                              ("middle", self._on_middle),
                              ("bottom", self._on_bottom)):
            field = f"{name}_pressed_count"
            count = self.buttons.get(field)
            if count is None:
                continue
            previous = self._last_counts.get(field)
            self._last_counts[field] = count
            if previous is not None and count > previous:
                log.info("%s button pressed (%d total)", name, count)
                handler(now)

    def _on_top(self, now: datetime.datetime) -> None:
        self.hide_chart()
        self.scroll_time(now)

    def _on_middle(self, now: datetime.datetime) -> None:
        self.hide_chart()
        if not ALLOW_REBOOT:
            log.info("middle button pressed, but ALLOW_REBOOT is off")
            return
        self.say("getting ready to reboot")
        log.warning("rebooting on middle button")
        subprocess.run(["/usr/sbin/reboot"], check=False)

    def _on_bottom(self, now: datetime.datetime) -> None:
        """Toggle the CPU-temperature chart on the secondary LCD."""
        if self._chart_mode:
            self.hide_chart()
            return
        self.sample_temperature(now, force=True)
        if self.draw_temperature_chart():
            self._chart_mode = True
            self._chart_refresh_at = now + datetime.timedelta(seconds=CHART_REFRESH)

    # ---------------- CPU temperature (LCD chart) ----------------

    def sample_temperature(self, now: datetime.datetime, *, force: bool = False) -> None:
        """Take a CPU-temperature sample at most once every TEMP_SAMPLE_SECONDS."""
        if not force and self._last_temp_sample is not None:
            elapsed = (now - self._last_temp_sample).total_seconds()
            if elapsed < TEMP_SAMPLE_SECONDS:
                return
        self._last_temp_sample = now
        value = _cpu_temperature_c()
        if value is not None:
            self._temperatures.append(value)

    def refresh_chart(self, now: datetime.datetime) -> None:
        """Redraw the chart while it is on screen (called from the main loop)."""
        if not self._chart_mode or self._chart_refresh_at is None:
            return
        if now < self._chart_refresh_at:
            return
        self.sample_temperature(now, force=True)
        self.draw_temperature_chart()
        self._chart_refresh_at = now + datetime.timedelta(seconds=CHART_REFRESH)

    def hide_chart(self) -> None:
        """Drop the chart and blank the LCD."""
        if not self._chart_mode:
            return
        self._chart_mode = False
        self._chart_refresh_at = None
        if self._screen is not None:
            try:
                self._screen.clear(BLACK)
            except Exception:  # noqa: BLE001 — never let the LCD break the clock
                log.warning("could not clear the screen", exc_info=True)

    def draw_temperature_chart(self) -> bool:
        """Plot the temperature history on the LCD. Returns True if drawn.

        The LCD is a separate display from the 8x8 panels, so the clock keeps
        showing the time while the chart is up — unlike the community script
        this replaces, whose chart loop never returned.
        """
        screen = self._screen
        samples = list(self._temperatures)
        if screen is None:
            log.info("bottom button pressed, but no screen is available "
                     "(CPU temperature: %s)", _cpu_temperature())
            return False
        width, height = screen.max_width, screen.max_height
        screen.clear(BLACK)
        screen.draw_rectangle(0, 0, width, TITLE_HEIGHT, TITLE_BAND)
        screen.write_text(8, 8, "CPU TEMPERATURE", 2, WHITE, TITLE_BAND)

        left, top = 12, TITLE_HEIGHT + 14
        right, bottom = width - 12, height - 46
        plot_width, plot_height = right - left, bottom - top
        for x, y, w, h in ((left, top, plot_width, 1), (left, bottom, plot_width, 1),
                           (left, top, 1, plot_height), (right, top, 1, plot_height)):
            screen.draw_rectangle(x, y, w, h, AXIS_COLOUR)

        if len(samples) < 2:
            screen.write_text(left, (top + bottom) // 2, "collecting samples",
                              1, AXIS_COLOUR, BLACK)
            return True

        low, high = min(samples), max(samples)
        if high - low < 1.0:                      # a flat line still needs a range
            middle = (high + low) / 2
            low, high = middle - 0.5, middle + 0.5
        columns = max(1, min(len(samples), TEMP_HISTORY))
        step = max(1, plot_width // columns)
        scale = (plot_height - 2) / (high - low)

        previous_y: int | None = None
        for index, value in enumerate(samples[-columns:]):
            x = left + index * step
            y = bottom - 1 - int((value - low) * scale)
            colour = HOT_COLOUR if value >= high - (high - low) * 0.1 else LINE_COLOUR
            if previous_y is None:
                screen.draw_rectangle(x, y, step, 2, colour)
            else:
                y0, y1 = sorted((y, previous_y))
                screen.draw_rectangle(x, y0, step, y1 - y0 + 2, colour)
            previous_y = y

        screen.write_text(left, bottom + 10,
                          f"now {samples[-1]:.1f}C  min {low:.1f}C  max {high:.1f}C",
                          1, WHITE, BLACK)
        screen.write_text(left, bottom + 24,
                          f"{len(samples)} samples, one per {TEMP_SAMPLE_SECONDS}s",
                          1, AXIS_COLOUR, BLACK)
        log.info("drew the CPU temperature chart (%d samples, %.1f..%.1fC)",
                 len(samples), min(samples), max(samples))
        return True

    # ---------------- hourly chime ----------------

    def hourly_chime(self, now: datetime.datetime) -> None:
        if now.minute != 0 or now.hour == self._last_hour_chime:
            return
        self._last_hour_chime = now.hour
        if HOURLY_SCROLL:
            self.scroll_time(now)
        if HOURLY_SPEAK and 7 < now.hour < 20:
            self.say(f"it's {now.hour} o'clock")


def _open_light_log(path: str | None = None):
    """Open the ambient-light CSV for appending, or None when it is off."""
    target = LIGHT_LOG if path is None else path
    if not target:
        return None
    try:
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
        fresh = not os.path.exists(target)
        handle = open(target, "a", buffering=1, encoding="utf-8")
        if fresh:
            handle.write("timestamp,ambient,brightness,low,high\n")
        return handle
    except OSError:
        log.warning("could not open the light log %r; continuing without it",
                    target, exc_info=True)
        return None


def _write_light_row(handle, ambient, brightness, curve_range) -> None:
    """Append one calibration row. Never lets a disk problem stop the clock."""
    if handle is None:
        return
    low, high = curve_range
    try:
        handle.write(f"{datetime.datetime.now().isoformat(timespec='seconds')},"
                     f"{'' if ambient is None else int(ambient)},"
                     f"{brightness:.3f},{low:.1f},{high:.1f}\n")
    except Exception:  # noqa: BLE001 — a log file is not worth a crash
        log.debug("could not append to the light log", exc_info=True)


def _cpu_temperature_c() -> float | None:
    """Read the Pi's CPU temperature in °C (None if the file is unavailable)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as handle:
            return int(handle.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _cpu_temperature() -> str:
    """Read the Pi's CPU temperature without needing a cube connection."""
    value = _cpu_temperature_c()
    return "unavailable" if value is None else f"{value:.1f}C"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _ensure_link(cube: Any, *, force: bool = False, quiet: bool = False) -> bool:
    """Keep the daemon connection alive across daemon restarts.

    A dead link is silent: telemetry values simply freeze and display writes
    fail inside the async worker, so the clock can run for hours drawing
    nothing at all. That is why this checks on *every* pass rather than only
    after an exception — the main loop may never raise. `force` is for when
    the failure itself said the link is gone, which is more reliable than
    asking the client; `quiet` keeps a long outage from filling the journal
    with one line per second.
    """
    control = getattr(cube, "control", None)
    if control is None or (control.connected and not force):
        return True
    try:
        cube.reconnect()
    except Exception as exc:  # noqa: BLE001 — the next pass tries again
        if not quiet:
            log.info("daemon is not back yet: %s", exc)
        return False
    return True


def build_curve() -> BrightnessCurve:
    return BrightnessCurve(low=LOW, high=HIGH, minimum=BRIGHTNESS_MIN,
                           maximum=BRIGHTNESS_MAX, gamma=GAMMA,
                           smoothing=SMOOTHING, adapt=ADAPT)


def run(cube: LumiCube, *, seconds: float | None = None,
        poll_interval: float = 0.5, once: bool = False) -> int:
    clock = AmbientClock(cube, brightness=build_curve())
    display = cube.display

    display.set_all(0x000000)

    # Wait for a first light reading so the very first frame is already
    # dimmed correctly (the sensor publishes ~30 frames/s, so this is quick).
    if clock.light is not None:
        try:
            clock.light.wait_for("ambient_light", timeout=5.0)
        except Exception:  # noqa: BLE001 — timeout or telemetry unavailable
            log.warning("no ambient light reading yet; using full brightness")
    brightness = clock.poll_light()
    brightness = clock.brightness() if brightness is None else brightness
    log.info("startup brightness %.3f (range %s)", brightness, clock.curve.range)

    now = datetime.datetime.now()
    clock.render(now, brightness, force=True)
    if once:
        display.flush(timeout=2.0)
        log.info("rendered one frame; exiting")
        return 0

    if hasattr(signal, "SIGTERM"):
        def _on_sigterm(_signum, _frame):
            raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGTERM, _on_sigterm)
        except ValueError:                      # not the main thread
            log.debug("could not install the SIGTERM handler")

    deadline = None if seconds is None else time.monotonic() + seconds
    status_at = time.monotonic() + STATUS_INTERVAL
    light_log = _open_light_log()
    _write_light_row(light_log, clock.last_raw, brightness, clock.curve.range)
    link_lost = False
    try:
        while True:
            if not _ensure_link(clock.cube, quiet=link_lost):
                if not link_lost:
                    log.warning("lost the daemon connection; reconnecting")
                    link_lost = True
                time.sleep(poll_interval)
                continue
            if link_lost:
                log.info("reconnected to the daemon; resuming")
                link_lost = False
                clock.reapply_settings()
                clock.render(datetime.datetime.now(), brightness, force=True)
            now = datetime.datetime.now()
            try:
                clock.poll_buttons(now)
                clock.hourly_chime(now)
                clock.sample_temperature(now)
                clock.refresh_chart(now)
                fresh = clock.poll_light()
                brightness = clock.brightness() if fresh is None else fresh
                clock.render(now, brightness)
            except ControlError as exc:
                # `closed`/`no_daemon` mean the link is gone (typically a
                # daemon restart); anything else is a hiccup in this pass.
                log.warning("clock loop failed; retrying", exc_info=True)
                _ensure_link(clock.cube, force=exc.code in _LINK_GONE_CODES)
                time.sleep(poll_interval)
                continue
            except Exception:  # noqa: BLE001 — keep the clock alive through a hiccup
                log.warning("clock loop failed; retrying", exc_info=True)
                _ensure_link(clock.cube)
                time.sleep(poll_interval)
                continue
            if time.monotonic() >= status_at:
                log.info("ambient %s -> brightness %.2f (range %.0f..%.0f)",
                         clock.last_raw, brightness, *clock.curve.range)
                _write_light_row(light_log, clock.last_raw, brightness,
                                 clock.curve.range)
                status_at = time.monotonic() + STATUS_INTERVAL
            if deadline is not None and time.monotonic() >= deadline:
                log.info("finished %ss run (brightness %.3f, range %s)",
                         seconds, brightness, clock.curve.range)
                break
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        display.set_all(0x000000)
        display.flush(timeout=2.0)
        if light_log is not None:
            light_log.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=SERIAL_DEVICE)
    parser.add_argument("--seconds", type=float, default=None,
                        help="run for this long, then exit (default: forever)")
    parser.add_argument("--once", action="store_true",
                        help="render one frame and exit")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--config", default=None,
                        help="config file to load (default: ambient_clock_config.py "
                             "next to this script, if present)")
    parser.add_argument("--light-log", default=None,
                        help="CSV to append ambient readings to (default: the "
                             "LIGHT_LOG setting; an empty string disables it)")
    parser.add_argument("--no-telemetry", action="store_true",
                        help="skip sensor setup and use a fixed brightness")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.config:
        load_config(args.config)
    if args.light_log is not None:
        globals()["LIGHT_LOG"] = args.light_log

    if args.no_telemetry:
        with open_or_use_hosted(args.port) as cube:
            return run(cube, seconds=args.seconds, once=args.once,
                       poll_interval=args.poll_interval)
    with open_or_use_hosted(args.port, telemetry=True,
                            schema_node_names=(SENSOR_NODE_NAME,)) as cube:
        # Walk the cube board as well: the display, LCD and speaker drivers
        # all resolve their field keys through it. It stays *unsubscribed*,
        # because the cube board's telemetry stream competes with speaker
        # writes for the link and roughly quarters the audio throughput.
        try:
            cube.walk_all_schemas()
        except Exception:  # noqa: BLE001 — a partial walk still runs a clock
            log.warning("could not walk every board's schema; the LCD and the "
                        "speaker may be unavailable", exc_info=True)
        return run(cube, seconds=args.seconds, once=args.once,
                   poll_interval=args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())



