"""Screen driver tests (no hardware).

The driver writes fields by name; `RecordingWriter` captures what would go
on the wire, so the tests can decode the pixel stream back into an image
and check the recipes against the daemon's own method implementations.
"""

from __future__ import annotations

import sys
import types

import pytest

from pylumicube.screen import (
    BATCH_DELAY,
    BLACK,
    HEIGHT,
    MAX_PIXELS_PER_CALL,
    MAX_TEXT,
    PIXEL_BATCH,
    WIDTH,
    Screen,
    rgb565,
)


class RecordingWriter:
    """Stands in for `LumiCube.write_named`."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []
        self.await_response: list[bool] = []

    def __call__(self, module: str, values: dict, timeout: float | None = None,
                 await_response: bool = True) -> None:
        self.writes.append((module, dict(values)))
        self.await_response.append(await_response)

    # ----- helpers -----

    def last(self) -> tuple[str, dict]:
        return self.writes[-1]

    def pixel_batches(self) -> list[tuple[int, int, int, list[int]]]:
        """Decode pixel-stream writes into (x, y, width, pixels) tuples."""
        out = []
        for module, values in self.writes:
            if module != "screen" or "start_pixel_streaming" not in values:
                continue
            width = values["pixel_window_width"]
            pixels = [values[f"pixel_data_stream+{i}"] for i in range(width)]
            out.append((values["pixel_window_x"], values["pixel_window_y"],
                        width, pixels))
        return out


def _screen(delay: float = 0.0) -> tuple[Screen, RecordingWriter]:
    writer = RecordingWriter()
    return Screen(writer, batch_delay=delay), writer


# ----- colours ------------------------------------------------------------


def test_rgb565_matches_the_upstream_conversion() -> None:
    assert rgb565(0xFF0000) == 0xF800
    assert rgb565(0x00FF00) == 0x07E0
    assert rgb565(0x0000FF) == 0x001F
    assert rgb565(0xFFFFFF) == 0xFFFF
    assert rgb565(BLACK) == 0x0000
    assert rgb565(0x101010) == 0x1082
    assert rgb565(0x123456) == 0x11AA
    # The conversion truncates rather than rounding: the low 3/2/3 bits go.
    assert rgb565(0x080808) == 0x0841


# ----- rectangles ---------------------------------------------------------


def test_draw_rectangle_writes_the_daemon_recipe() -> None:
    screen, writer = _screen()
    screen.draw_rectangle(10, 20, 30, 40, 0xFF0000)
    module, values = writer.last()
    assert module == "screen"
    assert values == {
        "rectangle_x": 10,
        "rectangle_y": 20,
        "rectangle_width": 30,
        "rectangle_height": 40,
        "rectangle_colour": 0xF800,
        "rectangle_draw": 1,
    }


def test_set_pixel_is_a_one_by_one_rectangle() -> None:
    screen, writer = _screen()
    screen.set_pixel(5, 6, 0x00FF00)
    _module, values = writer.last()
    assert values["rectangle_width"] == 1
    assert values["rectangle_height"] == 1
    assert values["rectangle_x"] == 5 and values["rectangle_y"] == 6


def test_clear_fills_the_screen() -> None:
    screen, writer = _screen()
    screen.clear(0x0000FF)
    _module, values = writer.last()
    assert values["rectangle_width"] == WIDTH
    assert values["rectangle_height"] == HEIGHT
    assert values["rectangle_colour"] == 0x001F


def test_zero_sized_rectangle_writes_nothing() -> None:
    screen, writer = _screen()
    screen.draw_rectangle(0, 0, 0, 10, 0xFFFFFF)
    assert writer.writes == []


def test_screen_writes_do_not_wait_for_a_service_response() -> None:
    """The firmware applies screen writes without reliably answering them."""
    screen, writer = _screen()
    screen.draw_rectangle(0, 0, 1, 1, 0xFFFFFF)
    screen.set_half_row(0, 0, [0xFFFFFF])
    assert writer.await_response == [False, False]


def test_await_response_can_be_requested_explicitly() -> None:
    writer = RecordingWriter()
    screen = Screen(writer, batch_delay=0.0, await_response=True)
    screen.clear()
    assert writer.await_response == [True]


# ----- pixel streaming ----------------------------------------------------


def test_set_half_row_writes_the_stream_recipe() -> None:
    screen, writer = _screen()
    screen.set_half_row(4, 7, [0xFF0000, 0x00FF00])
    module, values = writer.last()
    assert module == "screen"
    assert values["pixel_window_x"] == 4
    assert values["pixel_window_y"] == 7
    assert values["pixel_window_width"] == 2
    assert values["pixel_window_height"] == 1
    assert values["start_pixel_streaming"] == 1
    assert values["stop_pixel_streaming"] == 1
    assert values["pixel_data_stream+0"] == 0xF800
    assert values["pixel_data_stream+1"] == 0x07E0


def test_set_half_row_rejects_oversized_batches() -> None:
    screen, _writer = _screen()
    with pytest.raises(ValueError):
        screen.set_half_row(0, 0, [0xFFFFFF] * (PIXEL_BATCH + 1))


def test_set_pixels_reconstructs_the_image() -> None:
    screen, writer = _screen()
    width, height = 10, 3
    pixels = [0x010203 * (index + 1) for index in range(width * height)]
    screen.set_pixels(2, 1, width, height, pixels)
    batches = writer.pixel_batches()
    assert len(batches) == height                      # one batch per row
    for row, (x, y, batch_width, batch) in enumerate(batches):
        assert (x, y) == (2, 1 + row)
        assert batch_width == width
        assert batch == [rgb565(colour) for colour in pixels[row * width:(row + 1) * width]]


def test_set_pixels_splits_wide_rows_into_batches() -> None:
    screen, writer = _screen()
    width = PIXEL_BATCH * 2 + 5
    screen.set_pixels(0, 0, width, 1, [0xFFFFFF] * width)
    batches = writer.pixel_batches()
    assert [b[2] for b in batches] == [PIXEL_BATCH, PIXEL_BATCH, 5]
    assert [b[0] for b in batches] == [0, PIXEL_BATCH, PIXEL_BATCH * 2]


def test_set_pixels_validates_size_and_bounds() -> None:
    screen, _writer = _screen()
    with pytest.raises(ValueError):
        screen.set_pixels(0, 0, 4, 4, [0] * 15)
    with pytest.raises(ValueError):
        screen.set_pixels(WIDTH - 1, 0, 4, 1, [0] * 4)
    with pytest.raises(ValueError):
        screen.set_pixels(0, HEIGHT - 1, 1, 4, [0] * 4)


def test_batch_delay_is_applied(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("pylumicube.screen.time.sleep", slept.append)
    screen, _writer = _screen(delay=BATCH_DELAY)
    screen.set_half_row(0, 0, [0xFFFFFF])
    assert slept == [BATCH_DELAY]


# ----- text ---------------------------------------------------------------


def test_write_text_writes_characters_and_attributes() -> None:
    screen, writer = _screen()
    screen.write_text(10, 20, "Hi", size=2, colour=0xFF0000,
                      background_colour=0x0000FF)
    module, values = writer.last()
    assert module == "screen"
    assert values["text+0"] == ord("H")
    assert values["text+1"] == ord("i")
    assert values["text+2"] == 0                      # NUL terminator
    assert values["text_x"] == 10
    assert values["text_y"] == 20
    assert values["text_size"] == 2
    assert values["text_colour"] == 0xF800
    assert values["text_background_colour"] == 0x001F
    assert values["text_draw"] == 1


def test_write_text_rejects_too_much_text() -> None:
    screen, _writer = _screen()
    with pytest.raises(ValueError):
        screen.write_text(0, 0, "x" * (MAX_TEXT + 1))


# ----- configuration ------------------------------------------------------


def test_resolution_scaling_affects_the_usable_area() -> None:
    screen, writer = _screen()
    assert (screen.max_width, screen.max_height) == (WIDTH, HEIGHT)
    screen.set_resolution_scaling(2)
    _module, values = writer.last()
    assert values == {"resolution_scaling": 2}
    assert (screen.max_width, screen.max_height) == (WIDTH // 2, HEIGHT // 2)


def test_resolution_scaling_is_validated() -> None:
    screen, _writer = _screen()
    with pytest.raises(ValueError):
        screen.set_resolution_scaling(0)


def test_rotation_and_inversion_are_written_and_tracked() -> None:
    screen, writer = _screen()
    screen.set_rotation(90)
    assert writer.last()[1] == {"rotation": 90}
    assert screen.rotation == 90
    screen.set_invert_colours(True)
    assert writer.last()[1] == {"invert_colours": 1}
    assert screen.invert_colours is True


# ----- images -------------------------------------------------------------


class _FakeImage:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def convert(self, mode: str):
        assert mode == "RGB"
        return self

    def resize(self, size: tuple[int, int]):
        self.size = size
        return self

    def getdata(self):
        return [(1, 2, 3)] * (self.size[0] * self.size[1])


@pytest.fixture
def fake_pillow(monkeypatch):
    module = types.ModuleType("PIL")
    image_module = types.ModuleType("PIL.Image")
    image_module.open = lambda path: _FakeImage((640, 480))
    module.Image = image_module
    monkeypatch.setitem(sys.modules, "PIL", module)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_module)
    return image_module


def test_draw_image_resizes_and_streams(fake_pillow) -> None:
    screen, writer = _screen()
    screen.draw_image("/tmp/whatever.jpg", width=4, height=2)
    batches = writer.pixel_batches()
    assert len(batches) == 2
    assert all(batch[2] == 4 for batch in batches)
    assert batches[0][3] == [rgb565(0x010203)] * 4


def test_draw_image_defaults_to_the_whole_screen(fake_pillow) -> None:
    screen, writer = _screen()
    screen.draw_image("/tmp/whatever.jpg")
    batches = writer.pixel_batches()
    # A 320-pixel row needs 4 batches (the stream block's span is 80).
    assert len(batches) == (WIDTH // PIXEL_BATCH) * HEIGHT
    assert sum(batch[2] for batch in batches) == WIDTH * HEIGHT


def test_draw_image_without_pillow_explains_the_extra(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)
    screen, _writer = _screen()
    with pytest.raises(RuntimeError, match="screen"):
        screen.draw_image("/tmp/whatever.jpg")

