"""Secondary LCD driver (the 320x240 screen on the cube base board).

The `screen` module is driven entirely by `SET_FIELDS` writes; the recipes
below are transcribed from the daemon's own method implementations
(`ScreenSetHalfRowMethod`, `ScreenDrawRectangleMethod`,
`ScreenWriteTextMethod` — see PROTOCOL.md §5.3):

* a rectangle is six fields: `rectangle_x/y/width/height/colour` plus
  `rectangle_draw = 1`;
* pixels stream through a window: set `pixel_window_x/y/width/height`,
  raise `start_pixel_streaming`, write one key per pixel from
  `pixel_data_stream`, then raise `stop_pixel_streaming`. The firmware
  accepts at most **80 pixels per batch** (the `pixel_data_stream` block's
  span) and the daemon caps a single call at **160** (half the screen
  width), parking ~8 ms between requests;
* text is written one character per key from `text` (terminated by a `0`
  byte) followed by `text_x/y/size/colour/background_colour` and
  `text_draw = 1`.

Colours on the wire are **RGB565**, not 0xRRGGBB, and the firmware's
conversion truncates: `((rgb >> 19) & 0x1F) << 11 | ((rgb >> 10) & 0x3F) << 5
| ((rgb >> 3) & 0x1F)`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

#: Panel geometry at `resolution_scaling = 1`.
WIDTH = 320
HEIGHT = 240
#: The `pixel_data_stream` block's span: the firmware's per-batch limit.
PIXEL_BATCH = 80
#: The daemon caps one `set_pixels` call at half the screen width.
MAX_PIXELS_PER_CALL = 160
#: `text` block span 128, minus the NUL terminator.
MAX_TEXT = 127
#: The daemon parks this long before each pixel request so the firmware can
#: process the previous row.
BATCH_DELAY = 0.008

BLACK = 0x000000
WHITE = 0xFFFFFF


def rgb565(colour: int) -> int:
    """Convert a 0xRRGGBB colour to the screen's RGB565, as upstream does."""
    colour &= 0xFFFFFF
    return (((colour >> 19) & 0x1F) << 11
            | ((colour >> 10) & 0x3F) << 5
            | ((colour >> 3) & 0x1F))


class Screen:
    """Write API for the secondary LCD.

    `write` is the cube's name-based field writer (`LumiCube.write_named`),
    so this class never needs to know key numbers.
    """

    def __init__(self, write: Callable[..., None], *,
                 batch_delay: float = BATCH_DELAY,
                 timeout: float = 2.0,
                 await_response: bool = False) -> None:
        self._write = write
        self._batch_delay = batch_delay
        self._timeout = timeout
        # The firmware applies screen writes but does not reliably answer
        # them (the daemon's own methods tolerate a missing response and
        # report "busy"), so by default we wait only for the link-level ACK.
        self.await_response = await_response
        # Tracked locally: there is no field-read service, so a value we
        # cannot observe is remembered from our own last write.
        self.scaling = 1
        self.rotation = 0
        self.invert_colours = False

    def _send(self, values: dict) -> None:
        self._write("screen", values, timeout=self._timeout,
                    await_response=self.await_response)

    # ---------------- configuration ----------------

    def set_resolution_scaling(self, scaling: int) -> None:
        """Set the screen's resolution divisor (1 = full 320x240)."""
        if not 1 <= scaling <= 8:
            raise ValueError("scaling must be 1..8")
        self._send({"resolution_scaling": scaling})
        self.scaling = scaling

    def set_rotation(self, rotation: int) -> None:
        self._send({"rotation": rotation})
        self.rotation = rotation

    def set_invert_colours(self, invert: bool) -> None:
        self._send({"invert_colours": 1 if invert else 0})
        self.invert_colours = bool(invert)

    @property
    def max_width(self) -> int:
        return WIDTH // self.scaling

    @property
    def max_height(self) -> int:
        return HEIGHT // self.scaling

    # ---------------- rectangles ----------------

    def draw_rectangle(self, x: int, y: int, width: int, height: int,
                       colour: int = WHITE) -> None:
        """Fill a rectangle. A 1x1 rectangle is a single pixel."""
        if width <= 0 or height <= 0:
            return
        self._send({
            "rectangle_x": x,
            "rectangle_y": y,
            "rectangle_width": width,
            "rectangle_height": height,
            "rectangle_colour": rgb565(colour),
            "rectangle_draw": 1,
        })

    def set_pixel(self, x: int, y: int, colour: int) -> None:
        """Set one pixel (a 1x1 rectangle, exactly as upstream does)."""
        self.draw_rectangle(x, y, 1, 1, colour)

    def clear(self, colour: int = BLACK) -> None:
        """Fill the whole screen with one colour."""
        self.draw_rectangle(0, 0, self.max_width, self.max_height, colour)

    # ---------------- pixel streaming ----------------

    def set_half_row(self, x: int, y: int, pixels: Sequence[int]) -> None:
        """Write up to ``PIXEL_BATCH`` pixels starting at (x, y)."""
        if not pixels:
            return
        if len(pixels) > PIXEL_BATCH:
            raise ValueError(f"at most {PIXEL_BATCH} pixels per batch, "
                             f"got {len(pixels)}")
        values: dict[str, int] = {
            "pixel_window_x": x,
            "pixel_window_y": y,
            "pixel_window_width": len(pixels),
            "pixel_window_height": 1,
            "start_pixel_streaming": 1,
        }
        for index, colour in enumerate(pixels):
            values[f"pixel_data_stream+{index}"] = rgb565(colour)
        values["stop_pixel_streaming"] = 1
        if self._batch_delay:
            time.sleep(self._batch_delay)
        self._send(values)

    def set_pixels(self, x: int, y: int, width: int, height: int,
                   pixels: Sequence[int]) -> None:
        """Write a `width` x `height` image at (x, y), row by row.

        `pixels` is row-major with `width * height` entries. Rows are split
        into ``PIXEL_BATCH``-sized chunks, and no single call carries more
        than ``MAX_PIXELS_PER_CALL`` pixels — the same limits the daemon
        applies.
        """
        if width <= 0 or height <= 0:
            return
        if len(pixels) != width * height:
            raise ValueError(f"expected {width * height} pixels, got {len(pixels)}")
        if x < 0 or y < 0 or x + width > self.max_width or y + height > self.max_height:
            raise ValueError(f"image {width}x{height} at ({x},{y}) is off-screen "
                             f"(max {self.max_width}x{self.max_height})")
        written = 0
        for row in range(height):
            row_pixels = pixels[row * width : (row + 1) * width]
            for start in range(0, width, PIXEL_BATCH):
                if written >= MAX_PIXELS_PER_CALL:
                    written = 0        # start a fresh call, as the daemon does
                batch = row_pixels[start : start + PIXEL_BATCH]
                self.set_half_row(x + start, y + row, batch)
                written += len(batch)

    # ---------------- text ----------------

    def write_text(self, x: int, y: int, text: str, size: int = 1,
                   colour: int = WHITE, background_colour: int = BLACK) -> None:
        """Draw `text` at (x, y) using the firmware's own font."""
        if len(text) > MAX_TEXT:
            raise ValueError(f"at most {MAX_TEXT} characters, got {len(text)}")
        values: dict[str, int] = {
            f"text+{index}": ord(char) for index, char in enumerate(text)
        }
        values[f"text+{len(text)}"] = 0            # NUL terminator
        values["text_x"] = x
        values["text_y"] = y
        values["text_size"] = size
        values["text_colour"] = rgb565(colour)
        values["text_background_colour"] = rgb565(background_colour)
        values["text_draw"] = 1
        self._send(values)

    # ---------------- images ----------------

    def draw_image(self, path: str, x: int = 0, y: int = 0,
                   width: int | None = None, height: int | None = None) -> None:
        """Draw an image file, resized to `width` x `height` (default: full screen).

        Needs Pillow — install the `[screen]` extra:
        ``pip install -e '.[screen]'``.
        """
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "draw_image needs Pillow: pip install -e '.[screen]'") from exc

        width = self.max_width if width is None else width
        height = self.max_height if height is None else height
        with Image.open(path) as image:
            resized = image.convert("RGB").resize((width, height))
            pixels = [(r << 16) | (g << 8) | b for (r, g, b) in resized.getdata()]
        self.set_pixels(x, y, width, height, pixels)

