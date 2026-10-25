"""Native Display coordinate writers (no hardware).

`Display.set_leds_xy` / `set_panel` / `set_3d` / `scroll_text` are the
native counterparts of the upstream community-script display API. The
index mappings themselves are pinned by `tests/test_compat_mappings.py`;
here we drive a real `Display` with a recording transport and decode the
SET_FIELDS payloads, so what actually goes on the wire is checked.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from pylumicube import flat_dictionary
from pylumicube.constants import TYPE_SET_FIELDS
from pylumicube.display import (
    BRIGHTNESS_KEY,
    LED_KEY_OFFSET,
    NUM_LEDS,
    SHOW_KEY,
    Display,
    led_index,
    xy_to_index,
    xyz_to_index,
)
from pylumicube.metadata import display_specs


class RecordingTransport:
    """Captures SET_FIELDS payloads and answers each request immediately."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []
        self.fail = False
        self.expect_response: list[bool] = []
        self.classes: list[tuple[str, bool]] = []

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     timeout: float = 5.0, priority: int = 20,
                     expect_response: bool = True, *, label: str = "control",
                     droppable: bool = False) -> Future:
        assert type_id == TYPE_SET_FIELDS
        self.expect_response.append(expect_response)
        self.classes.append((label, droppable))
        future: Future = Future()
        if self.fail:
            future.set_exception(TimeoutError("nope"))
        else:
            self.payloads.append(payload)
            future.set_result(b"")
        return future

    def leds(self) -> dict[int, int]:
        """Merge every recorded payload into a {led_index: colour} map."""
        out: dict[int, int] = {}
        for payload in self.payloads:
            values = flat_dictionary.deserialise(payload, display_specs())
            for key, value in values.items():
                if LED_KEY_OFFSET <= key < LED_KEY_OFFSET + NUM_LEDS:
                    out[key - LED_KEY_OFFSET] = value
        return out


class DeferredTransport(RecordingTransport):
    """Records payloads but leaves every future unresolved until told to."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[Future] = []

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     timeout: float = 5.0, priority: int = 20,
                     expect_response: bool = True, *, label: str = "control",
                     droppable: bool = False) -> Future:
        assert type_id == TYPE_SET_FIELDS
        self.expect_response.append(expect_response)
        self.classes.append((label, droppable))
        self.payloads.append(payload)
        future: Future = Future()
        self.pending.append(future)
        return future

    def release(self, count: int | None = None) -> None:
        while self.pending and (count is None or count > 0):
            self.pending.pop(0).set_result(b"")
            if count is not None:
                count -= 1


def _display() -> tuple[Display, RecordingTransport]:
    transport = RecordingTransport()
    return Display(transport, node_id=125), transport


# ----- coordinate normalisation ------------------------------------------


def test_led_index_accepts_int_xy_and_xyz() -> None:
    assert led_index(0) == 0
    assert led_index(191) == 191
    assert led_index(192) is None
    assert led_index((0, 0)) == 63
    assert led_index((0, 0, 8)) == 63
    assert led_index((8, 0, 0)) == 127
    assert led_index((0, 8, 0)) == 191
    assert led_index((8, 8)) is None       # unmapped quadrant
    assert led_index("nope") is None
    assert led_index((1, 2, 3)) is None    # interior of the cube
    assert led_index((-1, 0)) is None


def test_panel_ranges_agree_between_2d_and_3d_apis() -> None:
    """The 2D and 3D APIs light the same 64-LED panels.

    Note the per-pixel correspondence is *not* the identity: upstream's 2D
    ``set_panel('right', ...)`` and 3D ``(8, y, z)`` describe the same
    panel but index it mirrored (2D column x ↔ 3D z = 15 - x). Only the
    panel *ranges* are guaranteed to match.
    """
    left_2d = {xy_to_index(x, y) for x in range(8) for y in range(8)}
    right_2d = {xy_to_index(x, y) for x in range(8, 16) for y in range(8)}
    top_2d = {xy_to_index(x, y) for x in range(8) for y in range(8, 16)}
    left_3d = {xyz_to_index(x, y, 8) for x in range(8) for y in range(8)}
    right_3d = {xyz_to_index(8, y, z) for y in range(8) for z in range(8)}
    top_3d = {xyz_to_index(x, 8, z) for x in range(8) for z in range(8)}
    assert left_2d == left_3d == set(range(0, 64))
    assert right_2d == right_3d == set(range(64, 128))
    assert top_2d == top_3d == set(range(128, 192))



# ----- wire output -------------------------------------------------------


def test_set_leds_xy_writes_index_keys_and_show() -> None:
    display, transport = _display()
    display.set_leds_xy({(0, 0): 0xFF0000, 191: 0x00FF00}, await_ack=True)
    values = flat_dictionary.deserialise(transport.payloads[-1], display_specs())
    assert values[LED_KEY_OFFSET + 63] == 0xFF0000
    assert values[LED_KEY_OFFSET + 191] == 0x00FF00
    assert values[SHOW_KEY] == 1


def test_set_leds_xy_drops_off_cube_coordinates() -> None:
    display, transport = _display()
    display.set_leds_xy({(8, 8): 0xFF0000}, await_ack=True)
    assert transport.payloads == []          # nothing to send


def test_set_led_delegates() -> None:
    display, transport = _display()
    display.set_led(0, 0, 0x123456, await_ack=True)
    values = flat_dictionary.deserialise(transport.payloads[-1], display_specs())
    assert values[LED_KEY_OFFSET + 63] == 0x123456


def test_display_frames_are_classified_for_the_link_queue() -> None:
    """Display frames are droppable, so a busy link drops them instead of
    letting them pile up behind the audio (and behind each other)."""
    display, transport = _display()
    display.set_led(0, 0, 0x123456, await_ack=True)
    assert transport.classes == [("display", True)]


def test_set_all_fills_every_led() -> None:
    display, transport = _display()
    display.set_all(0x010203, await_ack=True)
    leds = transport.leds()
    assert set(leds) == set(range(NUM_LEDS))
    assert set(leds.values()) == {0x010203}


@pytest.mark.parametrize("panel,expected", [
    # Panel ranges are pinned to upstream: left 0..63, right 64..127,
    # top 128..191 (the 3D API agrees — see the range test above).
    ("left", set(range(0, 64))),
    ("right", set(range(64, 128))),
    ("top", set(range(128, 192))),
])
def test_set_panel_lights_the_right_leds(panel: str, expected: set[int]) -> None:
    display, transport = _display()
    rows = [[0xAAAAAA] * 8 for _ in range(8)]
    display.set_panel(panel, rows, await_ack=True)
    assert set(transport.leds()) == expected


def test_set_panel_row_zero_is_the_top_row() -> None:
    """Row 0 must land on the panel's top row (y = 7 for the left panel)."""
    display, transport = _display()
    rows = [[0x111111] * 8] + [[0x222222] * 8 for _ in range(7)]
    display.set_panel("left", rows, await_ack=True)
    leds = transport.leds()
    assert {leds[xy_to_index(x, 7)] for x in range(8)} == {0x111111}
    assert {leds[xy_to_index(x, 6)] for x in range(8)} == {0x222222}


def test_set_panel_rejects_unknown_panel() -> None:
    display, _transport = _display()
    with pytest.raises(ValueError):
        display.set_panel("sideways", [[0]])


def test_set_3d_writes_face_coordinates() -> None:
    display, transport = _display()
    display.set_3d({(0, 0, 8): 0xFF0000, (8, 7, 7): 0x00FF00, (7, 8, 0): 0x0000FF},
                   await_ack=True)
    leds = transport.leds()
    assert leds[63] == 0xFF0000
    assert leds[127 - 7 - 8 * 7] == 0x00FF00
    assert leds[191 - 0 - 8 * 7] == 0x0000FF


def test_set_3d_ignores_non_face_coordinates() -> None:
    display, transport = _display()
    display.set_3d({(1, 1, 1): 0xFF0000}, await_ack=True)
    assert transport.payloads == []


def test_brightness_write_uses_the_display_key() -> None:
    display, transport = _display()
    display.set_brightness(128, await_ack=True)
    values = flat_dictionary.deserialise(transport.payloads[-1], display_specs())
    assert values[BRIGHTNESS_KEY] == 128
    with pytest.raises(ValueError):
        display.set_brightness(256)


def _led_frame(payload: bytes) -> dict[int, int]:
    """Decode one payload into {led_index: colour}."""
    values = flat_dictionary.deserialise(payload, display_specs())
    return {key - LED_KEY_OFFSET: value for key, value in values.items()
            if LED_KEY_OFFSET <= key < LED_KEY_OFFSET + NUM_LEDS}


def test_scroll_text_emits_frames_and_clears() -> None:
    display, transport = _display()
    display.scroll_text("AB", colour=0x00FF00, speed=1000)   # near-zero sleep
    assert len(transport.payloads) > 5
    frames = [_led_frame(payload) for payload in transport.payloads]
    # The text scrolls into view...
    assert any(0x00FF00 in frame.values() for frame in frames)
    # ...and off again, leaving the display blank rather than a stray column.
    assert set(frames[-1].values()) == {0x000000}
    # The last frame must have been flushed, not left queued.
    pending = display._pending_async  # noqa: SLF001
    assert pending is None or pending.done()


def test_display_frames_wait_for_the_link_ack_not_a_service_response() -> None:
    """SET_FIELDS responses are unreliable: 22 were lost in an hour on this cube.

    Waiting for one costs a full timeout on a frame that *has* arrived, which
    is the intermittent lag. Every display write — the scroll's exact end
    frame included — waits only for the link's acknowledgement, which the
    link guarantees by retransmitting until the firmware ACKs.
    """
    display, transport = _display()
    display.scroll_pixels([[0xFFFFFF] * 12], speed=1000)
    assert transport.expect_response, "nothing was sent"
    assert not any(transport.expect_response), "a frame waited for a response"


def test_scroll_pipelines_frames_without_blocking() -> None:
    """More than one frame is on the wire at a time, so latency is not the rate."""
    transport = DeferredTransport()
    display = Display(transport, node_id=125)
    finished: list[bool] = []

    def scroll() -> None:
        display.scroll_pixels([[0xFFFFFF] * 20], speed=1000)
        finished.append(True)

    thread = threading.Thread(target=scroll, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while len(transport.pending) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(transport.pending) >= 2, "frames are sent one at a time"
    assert not finished, "the scroll waited for a frame"
    while thread.is_alive():
        transport.release()
        thread.join(timeout=0.05)
    assert finished


def test_scroll_writes_only_the_pixels_that_change() -> None:
    """A steady-state frame is a column or two, not the whole 16x8 window."""
    display, transport = _display()
    display.scroll_pixels([[0xFFFFFF] * 20], speed=1000)
    per_frame = [len(_led_frame(payload)) for payload in transport.payloads]
    assert len(per_frame) > 10
    assert max(per_frame) <= 32                        # one column is 8 LEDs
    assert sum(per_frame) < 128 * len(per_frame) / 4    # not a full frame each time

