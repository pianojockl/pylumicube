"""Ambient-light dimming curve.

Maps a raw `light_sensor.ambient_light` reading to a display brightness in
0..1. The LumiCube's reading is a 32-bit count — hundreds to thousands in
normal room light, much higher in daylight — so the 0..255-style curves
found in older example scripts saturate immediately (see PROTOCOL.md §5.2).

The curve is **auto-ranging**: it tracks the darkest and brightest readings
it has seen and slowly lets those bounds decay back toward the current
reading, so it adapts to a room instead of needing calibration. `low` /
`high` seed that range; pass ``adapt=False`` to pin it instead.

    curve = BrightnessCurve()
    brightness = curve.update(light_sensor.ambient_light)   # 0.0 .. 1.0

`update` is intentionally cheap and allocation-free: it is called on every
telemetry sample.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Measured on a LumiCube in a normally lit room (2026-10-23): readings in
#: the high hundreds to ~2000. A dark room reads much lower, full daylight
#: much higher; these seeds only matter until auto-ranging has seen the room.
DEFAULT_LOW = 50.0
DEFAULT_HIGH = 2000.0

#: Narrower than this, a range is treated as "no information" and the
#: previous brightness is held (avoids dividing by ~0 and wild swings).
MIN_SPAN = 64.0


class BrightnessCurve:
    """Raw ambient-light reading -> 0..1 brightness."""

    def __init__(self, *, low: float | None = None, high: float | None = None,
                 minimum: float = 0.02, maximum: float = 1.0,
                 gamma: float = 1.0, smoothing: float = 0.25,
                 adapt: bool = True, decay: float = 0.0005) -> None:
        if minimum < 0 or maximum > 1 or minimum > maximum:
            raise ValueError("brightness bounds must satisfy 0 <= min <= max <= 1")
        if gamma <= 0:
            raise ValueError("gamma must be positive")
        if not 0.0 <= smoothing < 1.0:
            raise ValueError("smoothing must be in [0, 1)")
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.gamma = float(gamma)
        self.smoothing = float(smoothing)
        self.adapt = bool(adapt)
        self.decay = float(decay)
        self._low = float(low) if low is not None else DEFAULT_LOW
        self._high = float(high) if high is not None else DEFAULT_HIGH
        if self._high <= self._low:
            raise ValueError("high must be greater than low")
        self._value: float | None = None
        self._samples = 0

    # ---------------- state ----------------

    @property
    def value(self) -> float | None:
        """The most recent brightness, or None before the first update."""
        return self._value

    @property
    def range(self) -> tuple[float, float]:
        """The current (low, high) range used for normalisation."""
        return self._low, self._high

    def reset(self) -> None:
        self._value = None
        self._samples = 0

    # ---------------- mapping ----------------

    def update(self, raw: float) -> float:
        """Feed one reading and return the smoothed brightness (0..1)."""
        raw = float(raw)
        self._samples += 1
        if self.adapt:
            self._observe(raw)
        target = self._target(raw)
        if self._value is None:
            # First sample: jump straight to the target so the clock starts
            # at the right brightness instead of ramping into it.
            self._value = target
        else:
            self._value += (target - self._value) * (1.0 - self.smoothing)
        return self._value

    def _observe(self, raw: float) -> None:
        """Widen the range to include `raw`, and let it decay back slowly."""
        if raw < self._low:
            self._low = raw
        elif raw > self._high:
            self._high = raw
        else:
            # Nudge the bounds toward the observed range so a room that gets
            # permanently darker (or brighter) re-centres over time. The
            # decay is per sample, so it is deliberately tiny.
            span = self._high - self._low
            self._low += span * self.decay
            self._high -= span * self.decay

    def _target(self, raw: float) -> float:
        span = self._high - self._low
        if span < MIN_SPAN:
            # Range too narrow to be meaningful: hold the last brightness
            # (or start at the midpoint if this is the first sample).
            if self._value is not None:
                return self._value
            return self.minimum + (self.maximum - self.minimum) / 2
        position = (raw - self._low) / span
        position = 0.0 if position < 0.0 else 1.0 if position > 1.0 else position
        if self.gamma != 1.0:
            position = position ** self.gamma
        return self.minimum + position * (self.maximum - self.minimum)


def scale_colour(colour: int, brightness: float) -> int:
    """Scale a 0xRRGGBB colour by a 0..1 brightness, per channel.

    Scaling the colour (rather than the panel's global brightness field)
    keeps the matrix's 8-bit-per-channel precision, which matters when the
    target is "barely visible in a dark bedroom".
    """
    if brightness >= 1.0:
        return colour & 0xFFFFFF
    if brightness <= 0.0:
        return 0
    r = (colour >> 16) & 0xFF
    g = (colour >> 8) & 0xFF
    b = colour & 0xFF
    r = int(r * brightness) & 0xFF
    g = int(g * brightness) & 0xFF
    b = int(b * brightness) & 0xFF
    return (r << 16) | (g << 8) | b
