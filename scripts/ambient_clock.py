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
    bottom = CPU-temperature chart on the LCD (needs the `screen` module —
    logged, not drawn, until that lands);
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
`.example`); without it the defaults below apply.
"""

from __future__ import annotations

import argparse
import colorsys
import datetime
import logging
import os
import random
import subprocess
import sys
import time
from collections.abc import Callable

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pylumicube import LumiCube  # noqa: E402
from pylumicube.compat import open_or_use_hosted  # noqa: E402
from pylumicube.constants import SERIAL_DEVICE  # noqa: E402
from pylumicube.dimming import BrightnessCurve, scale_colour  # noqa: E402

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

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import ambient_clock_config as _cfg  # type: ignore[import-not-found]
except ImportError:
    _cfg = None

if _cfg is not None:
    for _name in ("BRIGHTNESS_MIN", "BRIGHTNESS_MAX", "GAMMA", "SMOOTHING", "ADAPT",
                  "LOW", "HIGH", "HUE_FOLLOWS_HOUR", "HUE", "SATURATION", "JITTER",
                  "ALLOW_REBOOT", "HOURLY_SCROLL", "HOURLY_SPEAK", "SCROLL_SPEED"):
        if hasattr(_cfg, _name):
            globals()[_name] = getattr(_cfg, _name)


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
        self.light = self._find_module("light_sensor")
        self.buttons = self._find_module("buttons")
        self._speaker = self._find_module("speaker")

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
        return self.curve.update(raw)

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
        self.cube.display.set_panel("left", render_text(hours, colour_for))
        self.cube.display.set_panel("right", render_text(minutes, colour_for))
        self._rendered = state
        return True

    def scroll_time(self, now: datetime.datetime) -> None:
        colour = scale_colour(hsv_colour(0.25, 1.0, 1.0), self.brightness())
        self.cube.display.scroll_text(now.strftime("%H:%M"), colour,
                                      speed=SCROLL_SPEED)
        self._rendered = None      # the scroll overwrote the digits

    def say(self, text: str) -> None:
        """Speak `text` if a speaker is available (else log and move on)."""
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
        self.scroll_time(now)

    def _on_middle(self, now: datetime.datetime) -> None:
        if not ALLOW_REBOOT:
            log.info("middle button pressed, but ALLOW_REBOOT is off")
            return
        self.say("getting ready to reboot")
        log.warning("rebooting on middle button")
        subprocess.run(["/usr/sbin/reboot"], check=False)

    def _on_bottom(self, now: datetime.datetime) -> None:
        # The original drew a CPU-temperature chart on the secondary LCD.
        # That needs the `screen` module, which pylumicube does not drive
        # yet, so log the reading instead — the button stays useful and the
        # hook is in the right place for when the LCD lands.
        log.info("bottom button pressed — LCD temperature chart pending "
                 "(CPU temperature: %s)", _cpu_temperature())

    # ---------------- hourly chime ----------------

    def hourly_chime(self, now: datetime.datetime) -> None:
        if now.minute != 0 or now.hour == self._last_hour_chime:
            return
        self._last_hour_chime = now.hour
        if HOURLY_SCROLL:
            self.scroll_time(now)
        if HOURLY_SPEAK and 7 < now.hour < 20:
            self.say(f"it's {now.hour} o'clock")


def _cpu_temperature() -> str:
    """Read the Pi's CPU temperature without needing a cube connection."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as handle:
            return f"{int(handle.read().strip()) / 1000.0:.1f}C"
    except OSError:
        return "unavailable"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


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

    deadline = None if seconds is None else time.monotonic() + seconds
    try:
        while True:
            now = datetime.datetime.now()
            clock.poll_buttons(now)
            clock.hourly_chime(now)
            fresh = clock.poll_light()
            brightness = clock.brightness() if fresh is None else fresh
            clock.render(now, brightness)
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
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=SERIAL_DEVICE)
    parser.add_argument("--seconds", type=float, default=None,
                        help="run for this long, then exit (default: forever)")
    parser.add_argument("--once", action="store_true",
                        help="render one frame and exit")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--no-telemetry", action="store_true",
                        help="skip sensor setup and use a fixed brightness")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.no_telemetry:
        with open_or_use_hosted(args.port) as cube:
            return run(cube, seconds=args.seconds, once=args.once,
                       poll_interval=args.poll_interval)
    with open_or_use_hosted(args.port, telemetry=True,
                            schema_node_names=(SENSOR_NODE_NAME,)) as cube:
        return run(cube, seconds=args.seconds, once=args.once,
                   poll_interval=args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())



