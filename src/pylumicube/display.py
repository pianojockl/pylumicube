"""Display module helper.

Wraps SET_FIELDS service requests to the LED matrix. PROTOCOL §4.4 + §4.7
+ §4.8.

Two write modes:

  await_ack=True (sync)
    The call returns once the firmware has ACK'd the final SET_FIELDS
    batch. Errors raise from the call site.

  await_ack=False (async, the default)
    The call returns immediately. A single background worker thread
    actually pushes the SET_FIELDS batches over the wire and waits for
    each ACK, so the caller's loop can compute the next frame in
    parallel with the previous frame's wire transmission. At most one
    frame is in flight on the wire (firmware constraint — see
    PROTOCOL.md §7 item 6); the next async call blocks until the
    previous one is done. `flush()` and `close()` drain the queue.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Optional

from . import flat_dictionary
from . import font
from .constants import MAX_UAVCAN_PAYLOAD, TYPE_SET_FIELDS
from .metadata import display_specs
from .transport import Transport

log = logging.getLogger(__name__)


# Field key offsets on the `cube` node (verified via ENUMERATE_FIELDS).
# See metadata.py for the full layout.
LED_KEY_OFFSET = 266
NUM_LEDS = 192
SHOW_KEY = 1266
BRIGHTNESS_KEY = 256


# ----------------------------------------------------------------------
# Coordinate mappings — verbatim from the upstream daemon's
# `foundry_api/standard_library.py` (`_Display.set_leds` / `.set_3d`).
# Community scripts address LEDs by coordinate, so these formulae are the
# compatibility contract; `tests/test_compat_mappings.py` pins them.
# ----------------------------------------------------------------------


def xy_to_index(x: int, y: int) -> int | None:
    """Map a 2D (x, y) coordinate to a 0..191 LED index, or None if off-cube.

    The 16x16 coordinate space covers three of the cube's panels:

    * ``x < 8 and y < 8``   — left panel
    * ``8 <= x < 16, y < 8`` — top panel
    * ``x < 8, 8 <= y < 16`` — right panel
    """
    if x < 0 or y < 0:
        return None
    if x < 8 and y < 8:
        return 63 - x - 8 * y
    if x < 16 and y < 8:
        return 7 - y + 8 * x
    if x < 8 and y < 16:
        return 176 + y - 8 * x
    return None


def xyz_to_index(x: int, y: int, z: int) -> int | None:
    """Map a 3D (x, y, z) face coordinate to a 0..191 LED index.

    Face coordinates sit on the 8x8x8 cube's surface: ``z == 8`` is the
    left panel, ``x == 8`` the right panel, ``y == 8`` the top panel.
    """
    if x < 0 or y < 0 or z < 0:
        return None
    if x < 8 and y < 8 and z == 8:
        return 63 - x - 8 * y
    if y < 8 and z < 8 and x == 8:
        return 127 - y - 8 * z
    if z < 8 and x < 8 and y == 8:
        return 191 - z - 8 * x
    return None


def led_index(key: object) -> int | None:
    """Normalise a coordinate key into a 0..191 LED index.

    Accepts an ``int`` (used as an index directly), an ``(x, y)`` tuple, or
    an ``(x, y, z)`` tuple. Anything else — including off-cube coordinates
    — returns None, matching upstream's "silently ignore" behaviour.
    """
    if isinstance(key, int):
        return key if 0 <= key < NUM_LEDS else None
    if isinstance(key, tuple):
        if len(key) == 2:
            return xy_to_index(int(key[0]), int(key[1]))
        if len(key) == 3:
            return xyz_to_index(int(key[0]), int(key[1]), int(key[2]))
    return None


class Display:
    """High-level write API for the display module."""

    def __init__(self, transport: Transport, node_id: int) -> None:
        self._transport = transport
        self._node_id = node_id
        self._specs = display_specs()
        # Async worker state. Lazily created on first async write so
        # purely-sync callers never spawn a thread.
        self._lock = threading.Lock()
        self._async_executor: Optional[ThreadPoolExecutor] = None
        self._pending_async: Optional[Future] = None

    @property
    def node_id(self) -> int:
        return self._node_id

    # ------------------------------------------------------------------
    # LED writes
    # ------------------------------------------------------------------

    def set_leds(self, leds: Mapping[int, int], show: bool = True,
                 *, await_ack: bool = False,
                 timeout: float = 2.0) -> Optional[Future]:
        """Set LED colours by 0-based index. `leds` maps index -> 0xRRGGBB.

        With ``await_ack=False`` (the default) the write is dispatched on
        a background worker; the call returns a Future you can ignore.
        With ``await_ack=True`` the call blocks until every batch has been
        ACK'd and returns ``None``.
        """
        values: dict[int, int] = {}
        for idx, colour in leds.items():
            if not 0 <= idx < NUM_LEDS:
                raise ValueError(f"led index {idx} out of range")
            values[LED_KEY_OFFSET + idx] = colour & 0xFFFFFF
        if show:
            values[SHOW_KEY] = 1
        return self._dispatch(values, await_ack=await_ack, timeout=timeout)

    def fill(self, colour: int, show: bool = True,
             *, await_ack: bool = False,
             timeout: float = 2.0) -> Optional[Future]:
        leds = {i: colour for i in range(NUM_LEDS)}
        return self.set_leds(leds, show=show, await_ack=await_ack, timeout=timeout)

    def show(self, *, await_ack: bool = False,
             timeout: float = 2.0) -> Optional[Future]:
        return self._dispatch({SHOW_KEY: 1}, await_ack=await_ack, timeout=timeout)

    def set_brightness(self, brightness: int,
                       *, await_ack: bool = False,
                       timeout: float = 2.0) -> Optional[Future]:
        if not 0 <= brightness <= 255:
            raise ValueError("brightness must be 0..255")
        return self._dispatch({BRIGHTNESS_KEY: brightness},
                              await_ack=await_ack, timeout=timeout)

    # ------------------------------------------------------------------
    # Coordinate writers (upstream-compatible)
    # ------------------------------------------------------------------

    def set_leds_xy(self, mapping: Mapping[Any, int], show: bool = True,
                    *, await_ack: bool = False,
                    timeout: float = 2.0) -> Optional[Future]:
        """Set LEDs keyed by coordinate: an int index, ``(x, y)``, or ``(x, y, z)``.

        Off-cube coordinates are dropped silently, as upstream does.
        """
        leds: dict[int, int] = {}
        for key, colour in mapping.items():
            idx = led_index(key)
            if idx is None:
                continue
            leds[idx] = colour & 0xFFFFFF
        if not leds:
            return None
        return self.set_leds(leds, show=show, await_ack=await_ack, timeout=timeout)

    def set_led(self, x: int, y: int, colour: int, show: bool = True,
                *, await_ack: bool = False,
                timeout: float = 2.0) -> Optional[Future]:
        """Set one LED by 2D panel coordinate."""
        return self.set_leds_xy({(x, y): colour}, show=show,
                                await_ack=await_ack, timeout=timeout)

    def set_all(self, colour: int, show: bool = True,
                *, await_ack: bool = False,
                timeout: float = 2.0) -> Optional[Future]:
        """Fill every LED (upstream's ``display.set_all``)."""
        return self.fill(colour, show=show, await_ack=await_ack, timeout=timeout)

    def set_panel(self, panel: str, colour_array: Sequence[Sequence[int]],
                  show: bool = True, *, await_ack: bool = False,
                  timeout: float = 2.0) -> Optional[Future]:
        """Draw an 8x8 grid onto one panel: ``"left"``, ``"right"`` or ``"top"``.

        ``colour_array`` is a list of 8 rows of 8 colours. Row 0 is the
        panel's top row. Matches upstream's `set_panel` layout (left panel
        starts at y=7 descending, right at x=8, top at y=15).
        """
        start_x = 0
        y = 7
        if panel == "top":
            y += 8
        elif panel == "right":
            start_x += 8
        elif panel != "left":
            raise ValueError("The panel must be 'left', 'right', or 'top'")
        leds: dict[tuple[int, int], int] = {}
        for row in colour_array:
            x = start_x
            for colour in row:
                leds[(x, y)] = colour
                x += 1
            y -= 1
        return self.set_leds_xy(leds, show=show, await_ack=await_ack, timeout=timeout)

    def set_3d(self, mapping: Mapping[tuple[int, int, int], int], show: bool = True,
               *, await_ack: bool = False,
               timeout: float = 2.0) -> Optional[Future]:
        """Set LEDs by 3D face coordinate (upstream's ``display.set_3d``)."""
        leds: dict[int, int] = {}
        for key, colour in mapping.items():
            if not isinstance(key, tuple) or len(key) != 3:
                continue
            idx = xyz_to_index(int(key[0]), int(key[1]), int(key[2]))
            if idx is None:
                continue
            leds[idx] = colour & 0xFFFFFF
        if not leds:
            return None
        return self.set_leds(leds, show=show, await_ack=await_ack, timeout=timeout)

    def scroll_text(self, text: str, colour: int = 0xFFFFFF,
                    background_colour: int = 0x000000, speed: float = 1.0,
                    with_gap: bool = True, *, timeout: float = 2.0) -> None:
        """Scroll ``text`` once across the 16x8 LED area (blocking).

        Lays the rendered glyphs into a row buffer 16 columns wider than
        the visible area (so text enters from the right and exits to the
        left), then walks an 8-row window across it at ``0.05 / speed``
        seconds per column. Uses the built-in 5x7 font (`pylumicube.font`)
        rather than upstream's PIL-rendered ``slkscr.ttf``.

        This is a timed animation, so it blocks for its duration and
        returns once the last frame has been ACK'd.
        """
        glyph_rows = font.render_text(text)
        if not glyph_rows or not glyph_rows[0]:
            return
        text_width = len(glyph_rows[0])
        pad = 16
        width = text_width + pad * 2
        rows = [[0] * width for _ in range(8)]
        for r, glyph_row in enumerate(glyph_rows):
            target_row = r + 1  # one-pixel top margin, fits the 8-tall area
            if target_row >= 8:
                continue
            for x, pixel in enumerate(glyph_row):
                rows[target_row][pad + x] = pixel

        period = 0.05 / max(speed, 0.0001)
        for shift in range(width - 16):
            frame_start = time.monotonic()
            window = [row[shift : shift + 17] for row in rows]
            leds: dict[tuple[int, int], int] = {}
            for r, row in enumerate(window):
                for x, pixel in enumerate(row):
                    plotted_x = x - 1 if (with_gap and x > 8) else x
                    if plotted_x >= 16:
                        continue
                    leds[(plotted_x, 7 - r)] = colour if pixel else background_colour
            self.set_leds_xy(leds, timeout=timeout)
            elapsed = time.monotonic() - frame_start
            if elapsed < period:
                time.sleep(period - elapsed)
        # Ensure the last frame is on the wire before returning.
        self.flush(timeout=timeout)

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    def flush(self, timeout: Optional[float] = None) -> None:
        """Block until all queued async writes have been ACK'd.

        Re-raises any exception from the most recent async batch.
        """
        with self._lock:
            pending = self._pending_async
        if pending is not None:
            pending.result(timeout=timeout)

    def close(self) -> None:
        """Drain any in-flight async write and tear down the worker thread.

        Called automatically by `LumiCube.stop()` / `__exit__`.
        Idempotent; safe to call from teardown handlers.
        """
        try:
            self.flush(timeout=5.0)
        except Exception:  # noqa: BLE001
            log.warning("display drain on close failed", exc_info=True)
        with self._lock:
            executor = self._async_executor
            self._async_executor = None
            self._pending_async = None
        if executor is not None:
            # `wait=False`: the only outstanding task is the one we just
            # awaited above (max_workers=1 + block-on-previous backpressure
            # = no queued tasks behind it).
            executor.shutdown(wait=False)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, values: Mapping[int, int], *,
                  await_ack: bool, timeout: float) -> Optional[Future]:
        if await_ack:
            self._send_set_fields(values, timeout)
            return None
        return self._submit_async(values, timeout)

    def _submit_async(self, values: Mapping[int, int], timeout: float) -> Future:
        with self._lock:
            executor = self._async_executor
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="lumicube-display",
                )
                self._async_executor = executor
            prev = self._pending_async

        # Backpressure: block until the previous async write completes
        # before queueing a new one. Ensures at most one SET_FIELDS frame
        # is on the wire (firmware constraint, see PROTOCOL.md §7 item 6)
        # and gives a runaway producer a natural rate limit.
        if prev is not None:
            try:
                prev.result()
            except Exception:  # noqa: BLE001
                # Log but don't propagate: the failure belonged to a frame
                # the caller already moved on from. The next call's result
                # is what they care about.
                log.warning("previous async display write failed", exc_info=True)

        fut = executor.submit(self._send_set_fields, values, timeout)
        with self._lock:
            self._pending_async = fut
        return fut

    def _send_set_fields(self, values: Mapping[int, int], timeout: float) -> None:
        # Split into batches that fit within one MESSAGE frame, then send
        # each one and *wait for its ACK before sending the next*.
        #
        # The cube firmware services exactly one SET_FIELDS service
        # request at a time — pipelining the batches causes the second
        # and third to be dropped (no ServiceResponse, requester times
        # out). The Java daemon also issues them strictly sequentially.
        # See PROTOCOL.md §7 item 6.
        batches = list(_batch_for_frame(values, self._specs))
        log.debug("set_fields: %d values across %d batch(es)", len(values), len(batches))
        for batch in batches:
            payload = flat_dictionary.serialise(batch, self._specs)
            future = self._transport.send_request(
                dest_id=self._node_id,
                type_id=TYPE_SET_FIELDS,
                payload=payload,
                timeout=timeout,
            )
            # Java returns empty body on SET_FIELDS success; we just block for ACK.
            future.result(timeout=timeout)


def _batch_for_frame(values: Mapping[int, int],
                     specs: Mapping[int, "object"]) -> list[dict[int, int]]:
    """Split a {key: value} map into chunks whose serialised FlatDictionary
    fits into one UAVCAN payload (<= MAX_UAVCAN_PAYLOAD bytes).

    Uses `flat_dictionary.SizeTracker` to maintain the encoded size as
    each key is appended — O(n) over the whole input. Previously this
    re-serialised the entire candidate batch on every key (O(n²)), which
    dominated frame time for full-matrix updates.
    """
    sorted_keys = sorted(values)
    if not sorted_keys:
        return []
    # `_FloorIndex` is a `flat_dictionary` internal but both modules are
    # part of the same package; using it directly avoids reimplementing
    # the floor-key lookup. A missing spec is propagated as KeyError —
    # the caller would have failed at `serialise()` time anyway.
    floor_lookup = flat_dictionary._FloorIndex(specs)
    batches: list[dict[int, int]] = []
    current: dict[int, int] = {}
    tracker = flat_dictionary.SizeTracker()
    for key in sorted_keys:
        spec = floor_lookup.lookup(key)
        if tracker.size + tracker.cost_delta(key, spec) > MAX_UAVCAN_PAYLOAD and current:
            # Flush and start a fresh batch beginning at `key`.
            batches.append(current)
            current = {}
            tracker = flat_dictionary.SizeTracker()
        tracker.commit(key, spec)
        current[key] = values[key]
    if current:
        batches.append(current)
    return batches
