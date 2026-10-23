"""Snapshot the live cube's discoverable state into a single text file.

Run this on the Pi while the original daemon is stopped but the hardware
is in a known-good state. Captures, for every discovered node:

- node id, preferred name (GET_PREFERRED_NAME)
- the full ENUMERATE_FIELDS schema (via `pylumicube.discovery`): per-field
  key, name, type, size, span, module string, units string, flags

Intended as a reference snapshot before retiring the current OS image.
Output goes to stdout; redirect to a file and commit it under `ref_data/`.

Usage:
    python utilities/snapshot_hardware.py > ref_data/cube_snapshot.txt
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, "src")

from pylumicube import LumiCube  # noqa: E402
from pylumicube.discovery import probe_key  # noqa: E402
from pylumicube.metadata import FieldSpec  # noqa: E402


def _print_header() -> None:
    print(f"  {'KEY':>6} | {'NAME':28s} | {'TYPE':>4} | {'SIZE':>4} | "
          f"{'SPAN':>6} | {'MODULE':18s} | {'UNITS':10s} | FLAGS")


def _print_row(spec: FieldSpec) -> None:
    flags = "".join([
        "g" if spec.gettable else "-",
        "s" if spec.settable else "-",
        "d" if spec.debug else "-",
        "S" if spec.system else "-",
    ])
    print(f"  {spec.floor:>6} | {spec.name:28s} | {spec.type!s:>4} | "
          f"{spec.size!s:>4} | {spec.span!s:>6} | {spec.module:18s} | "
          f"{spec.units:10s} | {flags}")


def dump_node_fields(cube: LumiCube, node_id: int, *,
                     extra_probe_keys: tuple[int, ...] = ()) -> int:
    """Walk a node's schema via the library and print one row per block."""
    _print_header()
    specs = cube.discover_schema(node_id)
    for spec in sorted(specs.values(), key=lambda s: s.floor):
        _print_row(spec)

    if extra_probe_keys:
        print()
        print("  -- direct probes at well-known keys:")
        for key in extra_probe_keys:
            result = probe_key(cube.transport, node_id, key)
            if result is None:
                print(f"  probe @{key:>6}: (no response)")
                continue
            meta, _rel = result
            name = meta.get(0, "?")
            module = meta.get(12, "")
            print(f"  probe @{key:>6}: name={name!r} module={module!r}")
    return len(specs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--handshake-timeout", type=float, default=10.0)
    parser.add_argument("--schema-timeout", type=float, default=3.0)
    parser.add_argument("--schema-budget", type=float, default=120.0)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("# lumicube hardware snapshot")
    print(f"# captured: {stamp}")
    print(f"# port:     {args.port}")
    print()

    with LumiCube(args.port, handshake_timeout=args.handshake_timeout,
                  schema_timeout=args.schema_timeout,
                  schema_budget=args.schema_budget) as cube:
        # The LumiCube.start() path already runs allocator + passive discovery
        # + GET_PREFERRED_NAME for everything it sees.
        allocated = dict(cube._allocated)  # noqa: SLF001 — script-only access
        names = dict(cube._names)          # noqa: SLF001
        time.sleep(0.5)  # let any in-flight broadcasts settle

        print(f"## Discovered nodes ({len(allocated)})")
        for node_id, identity in sorted(allocated.items()):
            origin = "allocated" if identity else "passive"
            uid = str(identity) if identity else "-"
            name = names.get(node_id, "<unknown>")
            print(f"  node {node_id:>3}  name={name!r:30s} origin={origin}  uuid={uid}")
        print()

        for node_id in sorted(allocated):
            name = names.get(node_id, "<unknown>")
            print(f"## Node {node_id} ({name!r}) — metadata walk")
            count = dump_node_fields(cube, node_id)
            print(f"  ({count} field entries)")
            print()

    print("# end of snapshot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
