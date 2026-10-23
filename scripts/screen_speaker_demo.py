#!/usr/bin/env python3
"""Demo: the secondary LCD and the speaker (native API).

Draws on the 320x240 screen, then plays a tone and speaks a sentence. Use it
to sanity-check the hardware after a change, or as a starting point for your
own script.

    python scripts/screen_speaker_demo.py
    python scripts/screen_speaker_demo.py --no-speech   # screen only
    python scripts/screen_speaker_demo.py --text "hi"   # custom greeting

Notes from bringing these up on real hardware (see PROTOCOL.md §5.3):

* Screen commands are ~100-200 ms each on the firmware side, so a full-screen
  image (960 pixel batches) takes tens of seconds. `set_resolution_scaling(2)`
  quarters that if you do not need full resolution.
* Audio needs ~286 frames/s (112 samples per write at 32 kHz). The link
  sustains ~300/s when the cube board is *not* streaming telemetry, so this
  script subscribes to the sensor board only and walks the cube board's
  schema separately.
* `speaker.say()` needs `espeak-ng` or `espeak` installed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pylumicube import LumiCube  # noqa: E402
from pylumicube.constants import SERIAL_DEVICE  # noqa: E402
from pylumicube.speaker import SINE, SQUARE, TRIANGLE  # noqa: E402

RED = 0xFF0000
GREEN = 0x00FF00
BLUE = 0x0000FF
YELLOW = 0xFFFF00
WHITE = 0xFFFFFF


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=SERIAL_DEVICE)
    parser.add_argument("--text", default="hello from pylumicube")
    parser.add_argument("--volume", type=int, default=150,
                        help="speaker volume 0..200")
    parser.add_argument("--no-speech", action="store_true")
    parser.add_argument("--no-screen", action="store_true")
    parser.add_argument("--scaling", type=int, default=1,
                        help="screen resolution divisor (1 = 320x240)")
    args = parser.parse_args(argv)

    with LumiCube(args.port, telemetry=True,
                  schema_node_names=("button_and_light_sensor",)) as cube:
        # The cube board is not subscribed (its telemetry would halve the
        # audio throughput) but we need its field keys.
        cube.walk_all_schemas()
        print(f"nodes: {cube.node_names}")

        if not args.no_screen:
            screen = cube.screen
            screen.set_resolution_scaling(args.scaling)
            width, height = screen.max_width, screen.max_height
            print(f"screen: {width}x{height} (scaling {args.scaling})")

            screen.clear(BLUE)
            time.sleep(0.6)
            screen.draw_rectangle(20, 20, width - 40, height - 40, GREEN)
            time.sleep(0.6)
            for index, colour in enumerate((RED, YELLOW, WHITE)):
                screen.set_pixel(30 + index * 10, 30, colour)
            screen.write_text(20, height // 2, "pylumicube", 3, WHITE, GREEN)
            print("screen: drew a border, pixels and text")
            time.sleep(1.0)

        if not args.no_speech:
            speaker = cube.speaker
            speaker.volume = args.volume
            print(f"speaker: volume {speaker.volume}")
            for waveform in (SINE, SQUARE, TRIANGLE):
                speaker.tone(frequency=440.0, duration=0.3, amplitude=0.3,
                             function=waveform)
            print(f"speaker: tones done "
                  f"({speaker.written_chunks} chunks, {speaker.dropped_chunks} dropped)")
            try:
                speaker.say(args.text)
                print(f"speaker: said {args.text!r} "
                      f"({speaker.written_chunks} chunks total, "
                      f"{speaker.dropped_chunks} dropped)")
            except RuntimeError as exc:
                print(f"speaker: {exc}")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
