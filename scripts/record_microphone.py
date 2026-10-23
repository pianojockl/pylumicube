#!/usr/bin/env python3
"""Record from the cube's microphone — and optionally play it back.

    python scripts/record_microphone.py --seconds 5
    python scripts/record_microphone.py --seconds 5 --play

`--play` sends the recording back through the cube's own speaker, which is
the quickest way to confirm the capture is real audio: the microphone's
board is unsubscribed first, because its telemetry competes with speaker
writes for the link (see `pylumicube.microphone`).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pylumicube.compat import open_or_use_hosted  # noqa: E402
from pylumicube.constants import SERIAL_DEVICE  # noqa: E402
from pylumicube.microphone import SAMPLE_RATE  # noqa: E402

log = logging.getLogger("record_microphone")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=SERIAL_DEVICE)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--out", default="/tmp/lumicube-mic.wav")
    parser.add_argument("--play", action="store_true",
                        help="play the recording back through the cube")
    parser.add_argument("--volume", type=int, default=None, help="0..200 when playing")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with open_or_use_hosted(args.port, telemetry=True) as cube:
        mic = cube.microphone
        print(f"recording {args.seconds:.0f}s at {SAMPLE_RATE} Hz ...")
        started = time.monotonic()
        samples = mic.record(args.out, args.seconds)
        elapsed = time.monotonic() - started
        print(f"captured {samples} samples in {elapsed:.1f}s "
              f"({mic.stats['frames']} frames, {mic.stats['dropped']} dropped)")
        mic.disable()                      # give the link back to the speaker
        if samples == 0:
            print("no audio arrived — is the microphone published on this board?")
            return 1
        if args.play:
            if args.volume is not None:
                cube.speaker.volume = args.volume
            print("playing it back ...")
            cube.speaker.play(args.out)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
