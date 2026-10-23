#!/usr/bin/env python3
"""Find the brightness you can read in this room, and what to set.

Walks the display through a series of brightness levels — showing the clock's
own digits, in the clock's own colours — and prints the live ambient reading
at each one. Answer whether each level is readable and it finishes with
ready-to-paste settings for both ways of driving the curve:

  * **keep auto-ranging, raise the floor.** With `ADAPT = True` the darkest
    reading the curve has seen sits at the *bottom* of its range, so in a
    dark room the position is ~0 and `GAMMA` has nothing to lift: the floor
    (`BRIGHTNESS_MIN`) is the only thing that decides how bright the clock
    is. Simple and stable, but every dark room gets the same brightness.

  * **pin the range and lift the dark end.** `ADAPT = False` with `LOW` /
    `HIGH` measured over a whole day, and `GAMMA < 1` to brighten the dark
    end. More work, but an evening and the middle of the night can differ.

Stop the clock first — it owns the serial port:

    systemctl --user stop pylumicube-clock
    .venv/bin/python scripts/calibrate_dimming.py
    systemctl --user start pylumicube-clock

`--report` reads the CSV the clock logs (`LIGHT_LOG`, by default
`~/.local/state/pylumicube/light.csv`) and summarises the room by hour, which
is the data you want for the second option after a day or two:

    .venv/bin/python scripts/calibrate_dimming.py --report
"""

from __future__ import annotations

import argparse
import datetime
import logging
import math
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pylumicube import LumiCube  # noqa: E402
from pylumicube.compat import open_or_use_hosted  # noqa: E402
from pylumicube.constants import SERIAL_DEVICE  # noqa: E402

import ambient_clock  # noqa: E402

log = logging.getLogger("calibrate_dimming")

SENSOR_NODE_NAME = ambient_clock.SENSOR_NODE_NAME
DEFAULT_LEVELS = "0.15,0.25,0.35,0.45,0.6,0.8,1.0"

#: The floor/ceiling the pinned-range suggestion starts from.
SUGGEST_FLOOR = 0.1
SUGGEST_CEILING = 1.0


def parse_levels(text: str) -> list[float]:
    """Parse a comma-separated list of brightnesses, ascending, in 0..1."""
    levels: list[float] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        value = float(chunk)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"brightness {value} is outside 0..1")
        levels.append(value)
    if not levels:
        raise ValueError("no brightness levels given")
    return sorted(set(levels))


def suggest_gamma(ambient: float, level: float, low: float, high: float, *,
                  floor: float = SUGGEST_FLOOR,
                  ceiling: float = SUGGEST_CEILING) -> float | None:
    """Gamma that puts `level` at `ambient`, given a pinned range.

    The curve is ``floor + position ** gamma * (ceiling - floor)`` with
    ``position = (ambient - low) / (high - low)``; solve it for gamma.
    Returns None when the numbers cannot express the request.
    """
    span = high - low
    if span <= 0 or ceiling <= floor:
        return None
    position = (ambient - low) / span
    if not 0.0 < position < 1.0:
        return None
    target = (level - floor) / (ceiling - floor)
    if not 0.0 < target < 1.0:
        return None
    return math.log(target) / math.log(position)


def suggest_config(observations: list[tuple[float, list[int]]],
                   readable_level: float | None, *,
                   low: float | None = None, high: float | None = None) -> str:
    """Config-file text for both ways of driving the curve.

    `observations` is [(level, [ambient readings while that level was up])],
    `readable_level` the first level the user called readable, and `low` /
    `high` optional wider bounds from the clock's own log.
    """
    readings = [value for _level, values in observations for value in values]
    observed_low = min(readings) if readings else None
    observed_high = max(readings) if readings else None
    pinned_low = low if low is not None else observed_low
    pinned_high = high if high is not None else observed_high

    lines = ["# ---- what this run measured ----"]
    if observed_low is not None:
        lines.append(f"# ambient during the run: {observed_low}..{observed_high}")
    if readable_level is not None:
        values = observations[[level for level, _ in observations]
                              .index(readable_level)][1]
        ambient = statistics.median(values) if values else None
        lines.append(f"# readable from {readable_level:.2f}"
                     + (f" (at ambient ~{ambient:.0f})" if ambient is not None else ""))
    lines.append("")

    if readable_level is None:
        lines.append("# Nothing was marked readable — try again in more light, or")
        lines.append("# start from a higher floor than this run reached.")
        return "\n".join(lines)

    lines += [
        "# Option 1 — keep the auto-ranging curve and raise its floor.",
        "# In a dark room the clock sits exactly here, so pick the level you",
        "# want for the darkest room you read it in.",
        "ADAPT = True",
        f"BRIGHTNESS_MIN = {readable_level:.2f}",
        "",
        "# Option 2 — pin the range and lift the dark end with gamma.",
    ]

    if pinned_low is None or pinned_high is None:
        lines.append("# (needs ambient readings; run with the sensor connected)")
        return "\n".join(lines)

    ambient = None
    if readable_level in [level for level, _ in observations]:
        values = observations[[level for level, _ in observations]
                              .index(readable_level)][1]
        ambient = statistics.median(values) if values else None
    gamma = (suggest_gamma(ambient, readable_level, pinned_low, pinned_high)
             if ambient is not None else None)

    lines += [
        "# LOW/HIGH are the darkest and brightest readings the room ever gives.",
        "ADAPT = False",
        f"LOW = {pinned_low:.0f}",
        f"HIGH = {pinned_high:.0f}",
        f"BRIGHTNESS_MIN = {SUGGEST_FLOOR}",
        f"BRIGHTNESS_MAX = {SUGGEST_CEILING}",
    ]
    if gamma is None:
        lines.append("# GAMMA: cannot be derived from this run — the readable "
                     "level sits at")
        lines.append("# the very edge of the range, so pick one by eye (0.3-0.6 "
                     "usually works).")
    else:
        lines.append(f"GAMMA = {gamma:.2f}   # <1 brightens the dark end")
    return "\n".join(lines)


def observe(sensor, seconds: float, *, poll: float = 0.5) -> list[int]:
    """Collect ambient readings for `seconds`, printing each one."""
    readings: list[int] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = sensor.get("ambient_light")
        if value is not None:
            readings.append(int(value))
            print(f"\r    ambient {value:>6}  ", end="", flush=True)
        time.sleep(poll)
    print()
    return readings


def ask_readable(level: float, *, interactive: bool) -> bool | None:
    """Ask whether the level on screen is readable (None = stop)."""
    if not interactive:
        return None
    while True:
        answer = input(f"    is {level:.2f} readable? [y/N/q] ").strip().lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("q", "quit"):
            return None


def report(path: str) -> int:
    """Summarise the clock's light log by hour."""
    try:
        with open(path, encoding="utf-8") as handle:
            rows = [line.strip().split(",") for line in handle if line.strip()]
    except OSError as exc:
        print(f"cannot read {path}: {exc}")
        return 1
    if rows and rows[0][0].startswith("timestamp"):
        rows = rows[1:]
    if not rows:
        print(f"{path} has no readings yet")
        return 1

    by_hour: dict[int, list[tuple[float, float]]] = {}
    for row in rows:
        if len(row) < 3:
            continue
        try:
            stamp = datetime.datetime.fromisoformat(row[0])
            ambient = float(row[1])
            brightness = float(row[2])
        except ValueError:
            continue
        by_hour.setdefault(stamp.hour, []).append((ambient, brightness))

    print(f"{'hour':>4}  {'n':>4}  {'ambient min':>11} {'avg':>7} {'max':>7}   "
          f"{'brightness avg':>14}")
    for hour in sorted(by_hour):
        values = by_hour[hour]
        ambient = [value for value, _ in values]
        brightness = [value for _, value in values]
        print(f"{hour:>4}  {len(values):>4}  {min(ambient):>11.0f} "
              f"{statistics.mean(ambient):>7.0f} {max(ambient):>7.0f}   "
              f"{statistics.mean(brightness):>14.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=SERIAL_DEVICE)
    parser.add_argument("--levels", default=DEFAULT_LEVELS,
                        help=f"brightness levels to show (default {DEFAULT_LEVELS})")
    parser.add_argument("--hold", type=float, default=10.0,
                        help="seconds to hold each level (default 10)")
    parser.add_argument("--config", default=None,
                        help="config file to read the settings from, as the clock does")
    parser.add_argument("--report", action="store_true",
                        help="summarise the clock's light log instead of measuring")
    parser.add_argument("--readable", type=float, default=None,
                        help="treat this level as readable instead of asking "
                             "(for a non-interactive run)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.config:
        ambient_clock.load_config(args.config)

    if args.report:
        return report(ambient_clock.LIGHT_LOG)

    levels = parse_levels(args.levels)
    interactive = sys.stdin.isatty() and args.readable is None
    if not sys.stdin.isatty():
        print("note: stdin is not a terminal, so the readability prompts are off.")
        print("      Run it under `ssh -t` for prompts, or pass --readable LEVEL.")
    observations: list[tuple[float, list[int]]] = []
    readable: float | None = args.readable

    with open_or_use_hosted(args.port, telemetry=True,
                            schema_node_names=(SENSOR_NODE_NAME,)) as cube:
        cube.walk_all_schemas()
        sensor = cube.light_sensor
        if sensor is None:
            print("no light sensor available; nothing to calibrate against")
            return 1
        try:
            sensor.wait_for("ambient_light", timeout=5.0)
        except Exception:  # noqa: BLE001 — carry on with whatever arrives
            print("no ambient reading yet; continuing anyway")

        clock = ambient_clock.AmbientClock(cube, brightness=ambient_clock.build_curve())
        print(f"levels: {', '.join(f'{level:.2f}' for level in levels)}"
              f"  ({'interactive' if interactive else f'{args.hold:.0f}s each'})")
        print("watch the cube — the ambient reading below is what the sensor sees\n")

        for level in levels:
            clock.render(datetime.datetime.now(), level, force=True)
            print(f"  level {level:.2f}")
            readings = observe(sensor, args.hold)
            observations.append((level, readings))
            if readings:
                print(f"    ambient {min(readings)}..{max(readings)} "
                      f"(median {statistics.median(readings):.0f})")
            if readable is None:
                answer = ask_readable(level, interactive=interactive)
                if answer is None:
                    break
                if answer:
                    readable = level

        cube.display.set_all(0x000000)
        cube.display.flush(timeout=2.0)

    print()
    print(suggest_config(observations, readable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
