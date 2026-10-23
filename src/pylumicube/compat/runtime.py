"""Compat runtime — recreates the upstream foundry-daemon globals so
community scripts run unchanged.

The coordinate mappings and method signatures below are pinned to
``ref_data/lumicube-daemon/.../python/foundry_api/standard_library.py``
(the Python module the daemon injects into a user script's namespace).
"""

from __future__ import annotations

import concurrent.futures as _cf
import contextlib
import logging
import math
import os
import random
import socket
import sys
import threading
import time
import warnings
from colorsys import hsv_to_rgb
from typing import Any, Callable, Iterator, Mapping

from ..constants import SERIAL_DEVICE
from ..display import NUM_LEDS, Display, led_index, xy_to_index, xyz_to_index
from ..modules import FieldModule
from ..node import LumiCube
from ..screen import HEIGHT, WIDTH, Screen
from ..speaker import SINE, SQUARE, NOISE, Speaker
from . import font as _font  # noqa: F401 — kept for compatibility

log = logging.getLogger(__name__)


# ----- colour constants (verbatim from upstream standard_library.py) -----

BLACK = 0x000000
GREY = 0x808080
WHITE = 0xFFFFFF
RED = 0xFF0000
ORANGE = 0xFF8C00
YELLOW = 0xFFFF00
GREEN = 0x00FF00
CYAN = 0x00FFFF
BLUE = 0x0000FF
MAGENTA = 0xFF00FF
PINK = 0xFF007F
PURPLE = 0x800080


# ----- hosted-cube registry ----------------------------------------------
#
# Native-API scripts (e.g. `scripts/digital_clock.py`) normally open
# their own serial port with `LumiCube(...)`. When the same script is
# launched via the `lumicube-run` runner, the runner has *already*
# opened the cube and a second open would fail with `Resource busy`.
#
# `lumicube-run` registers its opened cube here before exec'ing the
# script; scripts that opt into dual-mode usage check `get_hosted_cube()`
# (or, better, use the `open_or_use_hosted()` context manager) instead of
# instantiating `LumiCube` unconditionally.

_hosted_cube: LumiCube | None = None
_hosted_lock = threading.Lock()


def get_hosted_cube() -> LumiCube | None:
    """Return the LumiCube opened by `lumicube-run`, if running under it.

    Returns ``None`` for a standalone Python invocation. Scripts wanting
    to work under both should call this (or `open_or_use_hosted`) rather
    than constructing `LumiCube()` directly.
    """
    return _hosted_cube


def _set_hosted_cube(cube: LumiCube | None) -> None:
    """Internal: install the LumiCube the runner has opened, or clear it.

    Not part of the public API — `lumicube-run` calls this around its
    `exec()` block.
    """
    global _hosted_cube
    with _hosted_lock:
        _hosted_cube = cube


@contextlib.contextmanager
def open_or_use_hosted(port: str = SERIAL_DEVICE,
                       **lumi_kwargs: Any) -> Iterator[LumiCube]:
    """Yield a started ``LumiCube``, owning it iff we opened it.

    Usage in a native-API script:

        from pylumicube.compat import open_or_use_hosted

        with open_or_use_hosted(args.port, telemetry=True) as cube:
            cube.display.fill(0xFF0000)

    When called under `lumicube-run`, this yields the runner's cube
    unchanged and does not close it on exit. Otherwise it opens a fresh
    ``LumiCube(port, **lumi_kwargs)`` and tears it down on exit, just like
    the bare ``with LumiCube(port) as cube:`` form.
    """
    hosted = get_hosted_cube()
    if hosted is not None:
        yield hosted
        return
    # A running daemon owns the port, so prefer it: scripts then share the
    # cube with the clock and Home Assistant instead of fighting over it.
    from ..client import CubeClient, daemon_available
    if daemon_available():
        client = CubeClient()
        try:
            if lumi_kwargs.get("telemetry"):
                client.enable_telemetry(
                    node_names=lumi_kwargs.get("schema_node_names"),
                    subscribe_except=lumi_kwargs.get("subscribe_except") or ())
            yield client
        finally:
            client.close()
        return
    with LumiCube(port, **lumi_kwargs) as cube:
        yield cube


# ----- helpers (verbatim semantics from upstream standard_library.py) -----


def hsv_colour(hue: float, sat: float, val: float) -> int:
    hue = hue % 1
    sat = max(0.0, min(float(sat), 1.0))
    val = max(0.0, min(float(val), 1.0))
    r, g, b = hsv_to_rgb(hue, sat, val)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def random_colour() -> int:
    return hsv_colour(random.random(), 1, 1)


# ----- async helper -----

_pool_lock = threading.Lock()
_pool: _cf.ThreadPoolExecutor | None = None


def run_async(task: Callable[..., Any], *args: Any, **kwargs: Any) -> _cf.Future:
    if not callable(task):
        raise ValueError("Expected function, method, or other callable")
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _cf.ThreadPoolExecutor()
        return _pool.submit(task, *args, **kwargs)


# ----- OpenSimplex noise helpers -----

_noise_lock = threading.Lock()
_noise_gen: Any = None


def _ensure_noise() -> Any:
    global _noise_gen
    with _noise_lock:
        if _noise_gen is None:
            try:
                import opensimplex  # type: ignore
            except ImportError as exc:
                raise ImportError(
                    "opensimplex is required for noise_2d/3d/4d; "
                    "install with `pip install opensimplex`"
                ) from exc
            # Match upstream seed so noise textures look the same.
            _noise_gen = opensimplex.OpenSimplex(seed=123456)
        return _noise_gen


def noise_2d(x: float, y: float) -> float:
    return _ensure_noise().noise2(x, y)


def noise_3d(x: float, y: float, z: float) -> float:
    return _ensure_noise().noise3(x, y, z)


def noise_4d(x: float, y: float, z: float, w: float) -> float:
    return _ensure_noise().noise4(x, y, z, w)


# ----- waveform sentinels (only sine_wave / white_noise / square_wave) -----

sine_wave = "sine_wave"
square_wave = "square_wave"
white_noise = "white_noise"


# ----- Display shim --------------------------------------------------------

# The coordinate mappings now live in the native API (`pylumicube.display`).
# These aliases keep the shim — and tests/test_compat_mappings.py, which pins
# them to the upstream formulae — pointing at a single implementation.
_xy_to_index = xy_to_index
_xyz_to_index = xyz_to_index
_coerce_index = led_index


class DisplayShim:
    """Wraps a `pylumicube.Display` with the upstream community-script API.

    Coordinate semantics are pinned to the daemon's
    ``foundry_api/standard_library.py``. Where upstream uses ``run_async``
    to send updates from a background thread, we do the SET_FIELDS write
    inline — pylumicube's transport layer already serialises one frame at
    a time, and inline calls give scripts predictable timing.
    """

    def __init__(self, display: Display) -> None:
        self._display = display
        # Cached so reads of `display.brightness` don't need a wire round-trip;
        # 100 matches the upstream default exposed in api.txt.
        self._brightness = 100

    # ----- field-like properties -----

    @property
    def brightness(self) -> int:
        return self._brightness

    @brightness.setter
    def brightness(self, value: int) -> None:
        value = int(value)
        # Upstream API documents brightness as 0..100; pylumicube's wire
        # field is 0..255. Translate so old scripts behave the same.
        wire = max(0, min(255, round(value * 255 / 100)))
        self._display.set_brightness(wire)
        self._brightness = max(0, min(100, value))

    # ----- LED writers -----

    def set_led(self, x: int, y: int, colour: int, show: bool = True) -> None:
        self.set_leds({(x, y): colour}, show=show)

    def set_leds(self, xy_to_colour: Mapping[Any, int], show: bool = True) -> None:
        self._display.set_leds_xy(xy_to_colour, show=show)

    def set_all(self, colour: int, show: bool = True) -> None:
        self._display.set_all(colour, show=show)

    def set_panel(self, panel: str, colour_array: list[list[int]], show: bool = True) -> None:
        self._display.set_panel(panel, colour_array, show=show)

    def set_3d(self, xyz_to_colour: Mapping[tuple[int, int, int], int],
               show: bool = True) -> None:
        self._display.set_3d(xyz_to_colour, show=show)

    # ----- text scroller -----

    def scroll_text(self, text: str, colour: int = WHITE,
                    background_colour: int = BLACK, speed: float = 1.0,
                    with_gap: bool = True) -> None:
        """Scroll ``text`` once across the 16x8 LED area."""
        self._display.scroll_text(text, colour=colour,
                                  background_colour=background_colour,
                                  speed=speed, with_gap=with_gap)

    # ----- text scroller -----

    def scroll_text(self, text: str, colour: int = WHITE,
                    background_colour: int = BLACK, speed: float = 1.0,
                    with_gap: bool = True) -> None:
        """Scroll ``text`` once across the 16x8 LED area."""
        self._display.scroll_text(text, colour=colour,
                                  background_colour=background_colour,
                                  speed=speed, with_gap=with_gap)


def _coerce_index(key: Any) -> int | None:
    """Normalise a set_leds key into a 0..191 LED index.

    Accepts:
      * int                — used directly (range-checked).
      * (x, y) 2-tuple     — upstream 16×16 mapping.
      * (x, y, z) 3-tuple  — falls through to the 3D mapping.
    Anything else → None (silently dropped, mirroring upstream).
    """
    if isinstance(key, int):
        if 0 <= key < NUM_LEDS:
            return key
        return None
    if isinstance(key, tuple):
        if len(key) == 2:
            return _xy_to_index(int(key[0]), int(key[1]))
        if len(key) == 3:
            return _xyz_to_index(int(key[0]), int(key[1]), int(key[2]))
    return None


# ----- Telemetry-backed modules -------------------------------------------


class TelemetryModuleShim:
    """Read-only module backed by the telemetry cache (upstream field API).

    ``light_sensor.ambient_light`` and friends. Semantics mirror the
    foundry-daemon proxies:

      * an unknown field name raises AttributeError (upstream raises too),
      * a field the node has published reads as its current value,
      * while the backing node has published *nothing* yet, reads raise
        AttributeError as well — that is what makes the upstream idiom
        ``while True: try: x = light_sensor.ambient_light except: sleep()``
        work, and it keeps scripts from acting on a bogus 0/None.
    """

    def __init__(self, module: FieldModule) -> None:
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        module: FieldModule = object.__getattribute__(self, "_module")
        try:
            ref = module.ref(name)
        except AttributeError:
            raise AttributeError(
                f"'{module.name}' module has no field '{name}'") from None
        value = module.get(name)
        if value is None and not module.has_data():
            raise AttributeError(
                f"'{module.name}.{name}' is not available yet "
                f"(no telemetry from node {ref.node_id})")
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        raise AttributeError(f"'{self._module.name}' fields are read-only")

    @property
    def field_names(self) -> list[str]:
        return self._module.field_names

    def wait_for(self, name: str, timeout: float | None = None,
                 default: int | None = None) -> int | None:
        """Block until `name` has been published (compat extension)."""
        return self._module.wait_for(name, timeout, default)

    def __repr__(self) -> str:
        return f"<TelemetryModuleShim {self._module.name!r}>"


class PiModule:
    """The daemon-local ``pi`` pseudo-module (no hardware involved).

    Mirrors ``global_service/service.py``'s ``pi_*`` helpers: the same
    methods, reading the same Linux interfaces.
    """

    _THERMAL = "/sys/class/thermal/thermal_zone0/temp"

    def cpu_temp(self) -> float:
        """CPU temperature in degrees Celsius."""
        with open(self._THERMAL) as fh:
            return int(fh.read().strip()) / 1000.0

    def cpu_percent(self) -> float:
        """CPU utilisation percentage since the previous call (0..100)."""
        fields = self._read_proc_stat()
        if fields is None:
            return 0.0
        total = sum(fields)
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        prev = getattr(self, "_prev_stat", None)
        self._prev_stat = (total, idle)
        if prev is None:
            return 0.0
        total_delta = total - prev[0]
        idle_delta = idle - prev[1]
        if total_delta <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (total_delta - idle_delta) / total_delta))

    def ram_percent_used(self) -> float:
        """Used RAM as a percentage of total."""
        info: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    info[parts[0][:-1]] = int(parts[1])
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        if total <= 0:
            return 0.0
        return 100.0 * (total - available) / total

    def disk_percent(self, path: str = "/") -> float:
        """Used disk space on the filesystem holding `path`, as a percentage."""
        stat = os.statvfs(path)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        if total <= 0:
            return 0.0
        return 100.0 * (total - free) / total

    def ip_address(self) -> str:
        """Primary outbound IP address (no packets are actually sent)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    @staticmethod
    def _read_proc_stat() -> list[int] | None:
        try:
            with open("/proc/stat") as fh:
                first = fh.readline().split()
        except OSError:
            return None
        if not first or first[0] != "cpu":
            return None
        try:
            return [int(value) for value in first[1:]]
        except ValueError:
            return None


# ----- Stubs for not-yet-implemented modules ------------------------------


class StubModule:

    """Placeholder for a hardware module we haven't wired up yet.

    Attribute lookups split on the ``available_fields`` allowlist:
      * Names *in* the allowlist are treated as data fields and return
        plain ``0`` (numeric, falsey, supports arithmetic — matches what
        a freshly-booted sensor reports before its first reading).
      * Names *not* in the allowlist are treated as method handles and
        return a callable that accepts any args and returns ``None``.

    Either path emits a one-time RuntimeWarning per attribute on first
    access. Scripts that only poke unimplemented modules degrade
    silently after the warning instead of crashing.
    """

    _warned: set[str] = set()

    def __init__(self, module_name: str, *, available_fields: tuple[str, ...] = ()) -> None:
        self._module_name = module_name
        self._available_fields = set(available_fields)

    def _warn(self, what: str) -> None:
        key = f"{self._module_name}.{what}"
        if key in StubModule._warned:
            return
        StubModule._warned.add(key)
        msg = (
            f"pylumicube compat: '{self._module_name}.{what}' is not yet "
            f"implemented; returning a no-op value. Script may not behave correctly."
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=3)
        log.warning(msg)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for missing attrs, so this is safe.
        self._warn(name)
        if name in self._available_fields:
            # Numeric zero behaves correctly under arithmetic (`% 2`,
            # `+= 1`, `> threshold`, etc.) and is falsey for `if`-checks.
            return 0
        return _StubCallable(self._module_name, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._warn(name)
        # Silently accept the write so e.g. `speaker.volume = 10` doesn't blow up.


class _StubCallable:
    """Method-handle stub: callable, returns None, falsey for bool checks."""

    def __init__(self, module: str, attr: str) -> None:
        self._module = module
        self._attr = attr

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<stub {self._module}.{self._attr}>"

    def __eq__(self, other: Any) -> bool:
        return other is None or isinstance(other, _StubCallable)

    def __hash__(self) -> int:
        return hash((self._module, self._attr))


class _LazyModule:
    """Defers building a compat module until the script first touches it.

    ``build_globals`` pre-populates every module name, but a display-only
    community script must not pay for telemetry setup (schema discovery +
    subscription) just because ``light_sensor`` exists in its namespace.
    """

    def __init__(self, factory: Callable[[], Any], name: str) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_module_name", name)
        object.__setattr__(self, "_resolved", None)

    def _resolve(self) -> Any:
        resolved = object.__getattribute__(self, "_resolved")
        if resolved is None:
            factory = object.__getattribute__(self, "_factory")
            resolved = factory()
            object.__setattr__(self, "_resolved", resolved)
        return resolved

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._resolve(), name, value)

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_module_name")
        resolved = object.__getattribute__(self, "_resolved")
        state = "resolved" if resolved is not None else "lazy"
        return f"<LazyModule {name!r} ({state})>"


class ScreenShim:
    """Upstream `screen` API on top of `pylumicube.screen.Screen`."""

    def __init__(self, screen: Screen) -> None:
        object.__setattr__(self, "_screen", screen)

    def set_pixel(self, x: int, y: int, colour: int) -> None:
        self._screen.set_pixel(x, y, colour)

    def set_pixels(self, x: int = 0, y: int = 0, width: int = WIDTH,
                   height: int = HEIGHT, pixels: list[int] | None = None) -> None:
        self._screen.set_pixels(x, y, width, height, list(pixels or []))

    def draw_rectangle(self, x: int, y: int, width: int, height: int,
                       colour: int) -> None:
        self._screen.draw_rectangle(x, y, width, height, colour)

    def write_text(self, x: int, y: int, text: str, size: int = 1,
                   colour: int = 0xFFFFFF, background_colour: int = 0x000000) -> None:
        self._screen.write_text(x, y, text, size, colour, background_colour)

    def draw_image(self, relative_or_absolute_path: str, x: int = 0, y: int = 0,
                   width: int | None = None, height: int | None = None) -> None:
        """Draw an image; a relative path resolves against ``~/Desktop``.

        That is what the daemon's `screen_draw_image` did, and community
        scripts rely on it (e.g. the clock writing `temperature-chart.jpg`).
        """
        self._screen.draw_image(_resolve_script_path(relative_or_absolute_path),
                                x, y, width, height)

    # ----- field-like properties -----

    @property
    def resolution_scaling(self) -> int:
        return self._screen.scaling

    @resolution_scaling.setter
    def resolution_scaling(self, value: int) -> None:
        self._screen.set_resolution_scaling(int(value))

    @property
    def rotation(self) -> int:
        return self._screen.rotation

    @rotation.setter
    def rotation(self, value: int) -> None:
        self._screen.set_rotation(int(value))

    @property
    def invert_colours(self) -> bool:
        return self._screen.invert_colours

    @invert_colours.setter
    def invert_colours(self, value: bool) -> None:
        self._screen.set_invert_colours(bool(value))


class SpeakerShim:
    """Upstream `speaker` API on top of `pylumicube.speaker.Speaker`."""

    #: Upstream exposes waveform sentinels as strings; map them to waveforms.
    _WAVEFORMS = {sine_wave: SINE, square_wave: SQUARE, white_noise: NOISE}

    def __init__(self, speaker: Speaker) -> None:
        object.__setattr__(self, "_speaker", speaker)

    def say(self, text: str) -> None:
        self._speaker.say(text)

    def play(self, relative_or_absolute_path: str) -> None:
        self._speaker.play(_resolve_script_path(relative_or_absolute_path))

    def tone(self, frequency: float = 261.626, duration: float = 0.5,
             amplitude: float = 0.25, function: str = sine_wave) -> None:
        self._speaker.tone(frequency, duration, amplitude,
                           self._WAVEFORMS.get(function, SINE))

    def stop(self) -> None:
        self._speaker.stop()

    @property
    def volume(self) -> int:
        return self._speaker.volume

    @volume.setter
    def volume(self, value: int) -> None:
        self._speaker.volume = int(value)


def _resolve_script_path(path: str) -> str:
    """Absolute path unchanged; a relative one resolves against ``~/Desktop``.

    Matches the daemon's Python service, which prefixed ``~/Desktop``.
    """
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    return os.path.join(os.path.expanduser("~/Desktop"), expanded)


# ----- Top-level facade ----------------------------------------------------


class LumiCubeCompat:
    """The ``cube`` object exposed to community scripts.

    Attributes match upstream's ``LumiCube``: ``display``, ``microphone``,
    ``speaker``, ``screen``, ``buttons``, ``light_sensor``, ``imu``,
    ``env_sensor``, ``pi``.

    ``display`` is hardware-backed directly. The sensor modules
    (``light_sensor``, ``buttons``, ``env_sensor``, ``imu``) are backed by
    node telemetry: the first access to one of them enables telemetry on
    the cube (schema discovery + subscription), so display-only scripts
    never pay for it. ``pi`` is implemented locally. ``screen``,
    ``speaker`` and ``microphone`` remain warn+no-op stubs.
    """

    #: Modules served from node telemetry (lazy: enabled on first access).
    _TELEMETRY_MODULES = ("light_sensor", "buttons", "env_sensor", "imu")
    #: Modules served by native drivers (also lazy).
    _DRIVER_MODULES = ("screen", "speaker")

    #: Wall-clock budget per node for the initial schema walk. Deliberately
    #: small: the cube base board's walk is slow (many probes, wide blocks)
    #: and any block it misses is re-probed on demand when a telemetry
    #: frame references it. Display-only scripts never pay this at all.
    _SCHEMA_BUDGET = 6.0

    def __init__(self, cube: LumiCube) -> None:
        self._cube = cube
        self.display = DisplayShim(cube.display)
        # Telemetry-backed and driver-backed modules are installed on first
        # access; until then the attribute is absent from __dict__ and
        # __getattr__ builds it (which is where telemetry gets switched on).
        self.microphone = StubModule("microphone", available_fields=("enable",))
        self.pi = PiModule()

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._TELEMETRY_MODULES and name not in self._DRIVER_MODULES:
            raise AttributeError(name)
        module = (self._telemetry_module(name) if name in self._TELEMETRY_MODULES
                  else self._driver_module(name))
        object.__setattr__(self, name, module)
        return module

    def lazy(self, name: str) -> _LazyModule:
        """A module handle that only enables telemetry when actually used."""
        if name in self._TELEMETRY_MODULES:
            return _LazyModule(lambda: self._telemetry_module(name), name)
        if name in self._DRIVER_MODULES:
            return _LazyModule(lambda: self._driver_module(name), name)
        return _LazyModule(lambda: getattr(self, name), name)

    def _ensure_telemetry(self, name: str) -> bool:
        """Enable telemetry if needed. Returns True if it is available."""
        cube: LumiCube = object.__getattribute__(self, "_cube")
        if cube.telemetry is not None:
            return True
        log.info("compat: enabling telemetry for '%s' access", name)
        try:
            # Walk every board (so `screen` / `speaker` have their keys) but
            # subscribe only to the sensor board: the cube board's own
            # telemetry stream competes with our writes for the link, and
            # subscribing to it cuts speaker throughput ~4x, which an
            # upstream-style script that calls speaker.say() would notice.
            cube.enable_telemetry(budget=self._SCHEMA_BUDGET,
                                  subscribe_except=("cube",))
        except Exception:  # noqa: BLE001 — callers fall back to a stub
            log.exception("compat: telemetry unavailable; '%s' stays stubbed", name)
            return False
        return cube.telemetry is not None

    def _driver_module(self, name: str):
        """Build `screen` / `speaker`, falling back to a stub if unavailable."""
        cube: LumiCube = object.__getattribute__(self, "_cube")
        if self._ensure_telemetry(name):
            try:
                if name == "screen":
                    return ScreenShim(cube.screen)
                if name == "speaker":
                    return SpeakerShim(cube.speaker)
            except Exception:  # noqa: BLE001 — missing module on this cube
                log.exception("compat: could not build the %r driver", name)
        warnings.warn(
            f"pylumicube compat: module '{name}' is not available on this "
            f"cube; returning a no-op stub. Script may not behave correctly.",
            RuntimeWarning, stacklevel=3)
        return StubModule(name, available_fields=("volume",) if name == "speaker" else ())

    def _telemetry_module(self, name: str):
        cube: LumiCube = object.__getattribute__(self, "_cube")
        field_module: FieldModule | None = None
        if self._ensure_telemetry(name):
            try:
                field_module = cube.modules.get(name)
            except Exception:  # noqa: BLE001
                log.exception("compat: could not build the module namespace")
        if field_module is None:
            warnings.warn(
                f"pylumicube compat: module '{name}' is not available on this "
                f"cube; returning a no-op stub. Script may not behave correctly.",
                RuntimeWarning, stacklevel=3)
            return StubModule(name)
        return TelemetryModuleShim(field_module)




# ----- Globals builder + runner -------------------------------------------


def build_globals(cube: LumiCube | LumiCubeCompat) -> dict[str, Any]:
    """Construct the namespace dict that mirrors what foundry-daemon
    injects into a script before ``exec()``ing it.
    """
    compat = cube if isinstance(cube, LumiCubeCompat) else LumiCubeCompat(cube)
    ns: dict[str, Any] = {
        # Pre-imported standard library modules — upstream does the same.
        "time": time,
        "math": math,
        "random": random,
        # Hardware facade + per-module top-level aliases. The sensor
        # modules are lazy handles: touching one is what switches
        # telemetry on, so display-only scripts stay cheap.
        "cube": compat,
        "display": compat.display,
        "buttons": compat.lazy("buttons"),
        "light_sensor": compat.lazy("light_sensor"),
        "env_sensor": compat.lazy("env_sensor"),
        "imu": compat.lazy("imu"),
        "screen": compat.lazy("screen"),
        "speaker": compat.lazy("speaker"),
        "microphone": compat.microphone,
        "pi": compat.pi,
        # Colour constants (both spellings — upstream `api.txt` uses
        # American "color" elsewhere, but the canonical ones are British).
        "black": BLACK,
        "grey": GREY,
        "white": WHITE,
        "red": RED,
        "orange": ORANGE,
        "yellow": YELLOW,
        "green": GREEN,
        "cyan": CYAN,
        "blue": BLUE,
        "magenta": MAGENTA,
        "pink": PINK,
        "purple": PURPLE,
        # Helpers.
        "hsv_colour": hsv_colour,
        "random_colour": random_colour,
        "noise_2d": noise_2d,
        "noise_3d": noise_3d,
        "noise_4d": noise_4d,
        "run_async": run_async,
        # Speaker waveform sentinels.
        "sine_wave": sine_wave,
        "square_wave": square_wave,
        "white_noise": white_noise,
        # Class for "another cube" usage; we don't yet support a remote
        # IP-addressed cube, so calling with an address falls through to
        # the local serial port.
        "LumiCube": LumiCubeCompat,
        "Cube": LumiCubeCompat,
        # Module file metadata so traceback shows the user's script name.
        "__name__": "__main__",
    }
    return ns


def run_script(
    path: str,
    *,
    cube: LumiCube | None = None,
    port: str = SERIAL_DEVICE,
    extra_globals: Mapping[str, Any] | None = None,
) -> None:
    """Run ``path`` as if loaded by the foundry-daemon script host.

    If ``cube`` is None, a `LumiCube` is opened on ``port`` for the
    duration of the script and closed cleanly on exit.
    """
    with open(path, "rb") as fh:
        source = fh.read()
    code = compile(source, path, "exec")

    own_cube = cube is None
    if own_cube:
        cube = LumiCube(port=port)
        cube.start()
    try:
        assert cube is not None
        ns = build_globals(cube)
        ns["__file__"] = path
        if extra_globals:
            ns.update(extra_globals)
        # Inject the script's directory into sys.path so relative
        # imports/file references (e.g. chime files) work the same way
        # they did under foundry-daemon.
        import os
        script_dir = os.path.dirname(os.path.abspath(path)) or "."
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        exec(code, ns)
    finally:
        if own_cube and cube is not None:
            cube.stop()
