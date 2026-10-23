"""`lumicube-say` and `lumicube-do`: small clients for the control socket.

    lumicube-say "The washing machine has finished"
    lumicube-do stats
    lumicube-do read module=light_sensor field=ambient_light
    lumicube-do tone frequency=880 duration=0.3

Both use the daemon when one is running and fall back to opening the serial
port directly, so they work on a fresh install with no daemon too.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from .client import CubeClient, connect_or_open
from .constants import SERIAL_DEVICE
from .control import ControlError, resolve_socket_path

log = logging.getLogger(__name__)


def _parse_value(text: str) -> Any:
    """`k=v` values: numbers and JSON where they parse, else the raw string."""
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("none", "null"):
        return None
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError:
            return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def say_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lumicube-say", description="Speak through the LumiCube's speaker.")
    parser.add_argument("text", nargs="+", help="what to say")
    parser.add_argument("--socket", default=None,
                        help="control socket (default: the daemon's, if any)")
    parser.add_argument("--port", default=SERIAL_DEVICE,
                        help="serial port when no daemon is running")
    parser.add_argument("--volume", type=int, default=None,
                        help="0..200, for this announcement only")
    parser.add_argument("--voice", default=None, help="espeak voice")
    parser.add_argument("--speed", type=int, default=None, help="espeak speed")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    text = " ".join(args.text)
    try:
        with connect_or_open(args.port, args.socket, telemetry=True) as cube:
            if isinstance(cube, CubeClient):
                cube.say(text, voice=args.voice, speed=args.speed,
                         volume=args.volume)
            else:
                cube.say(text, voice=args.voice, speed=args.speed,
                         volume=args.volume)
    except ControlError as exc:
        print(f"lumicube-say: {exc}", file=sys.stderr)
        return 1
    return 0


def do_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lumicube-do",
        description="Send one control operation to the cube and print the reply.")
    parser.add_argument("op", help="hello, stats, read, write, say, tone, ...")
    parser.add_argument("fields", nargs="*", help="key=value pairs")
    parser.add_argument("--socket", default=None,
                        help="control socket (default: the daemon's, if any)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fields: dict[str, Any] = {}
    for item in args.fields:
        key, _, value = item.partition("=")
        if not _:
            print(f"lumicube-do: expected key=value, got {item!r}", file=sys.stderr)
            return 2
        fields[key] = _parse_value(value)

    path = resolve_socket_path(args.socket)
    try:
        with CubeClient(path) as client:
            reply = client.control.request(args.op, timeout=600.0, **fields)
    except ControlError as exc:
        print(f"lumicube-do: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2, sort_keys=True))
    return 0
