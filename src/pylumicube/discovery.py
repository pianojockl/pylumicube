"""Runtime field-metadata discovery via ENUMERATE_FIELDS (PROTOCOL §4.5).

The wire protocol encodes values according to per-field metadata: type,
size in bytes, and the floor/span of the homogeneous block a key lives
in. `metadata.py` hardcodes the display schema we already know; this
module discovers the rest from the firmware, which is what telemetry
decoding needs — a `PUBLISHED_FIELDS` run cannot be parsed without
knowing each value's width.

The cube firmware does **not** report a block's floor directly. A query
at key `k` returns the metadata of the field covering `k` plus a leading
skip command that is either:

  * the **echo** of `k` — `k` is somewhere inside the block, or
  * the **relative distance** to the next block — `k` fell in a gap.

`_classify` distinguishes the two (including the `rel == k` ambiguity, by
probing `k + 1`), and `_resolve_floor` binary-searches backwards inside
the block's span when the floor is unknown. See PROTOCOL.md §4.5.1.

Derived from `utilities/snapshot_hardware.py`, which was validated
against real hardware (2026-05-14 and 2026-10-23 captures).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .constants import TYPE_ENUMERATE_FIELDS
from .metadata import FieldSpec
from .transport import Transport

log = logging.getLogger(__name__)


# ENUMERATE_FIELDS sub-dictionary keys (BootstrapMetadata.java).
META_NAME = 0
META_TYPE = 1
META_SIZE = 2
META_SPAN = 3
META_GETTABLE = 4
META_SETTABLE = 5
META_IDEMPOTENT = 6
META_MIN_VALUE = 7
META_MAX_VALUE = 8
META_UNITS = 9
META_DEBUG = 10
META_SYSTEM = 11
META_MODULE = 12

_STRING_KEYS = (META_NAME, META_UNITS, META_MODULE)


# Keys on the `cube` base board that host fields the auto-walk steps over
# (the firmware's "next field at floor >= k" hint jumps straight from
# speaker.data to the system fields). Taken from query_key.py probes and
# metadata.py's hardcoded display schema.
CUBE_WELL_KNOWN_KEYS = (
    256, 257, 258, 259, 260, 261, 262, 263, 264, 265,  # brightness, panel_*, gamma_*
    266,     # led_colour (span 999)
    1265,    # spread_spectrum_period
    1266,    # show
)


def _skip_op(value: int) -> bytes:
    """Encode a FlatDictionary skip with the narrowest width that fits.

    The skip command byte is `0x10 | width_in_bytes`. Width 0 means
    "skip 0"; we still emit a width-1 zero skip so the response shape
    stays predictable when called with value=0.
    """
    if value <= 0xFF:
        return bytes([0x11, value])
    if value <= 0xFFFF:
        return bytes([0x12]) + value.to_bytes(2, "little")
    if value <= 0xFFFFFF:
        return bytes([0x13]) + value.to_bytes(3, "little")
    if value <= 0xFFFFFFFF:
        return bytes([0x14]) + value.to_bytes(4, "little")
    raise ValueError(f"skip value {value} doesn't fit in 4 bytes")


def build_query(k0: int, k1: int = 0) -> bytes:
    """Build an ENUMERATE_FIELDS query: outer skip k0, run-of-1, optional sub-skip.

    When ``k1 == 0`` the inner skip is omitted — the cube then returns the
    whole metadata sub-dictionary for the field.
    """
    out = bytearray()
    if k0 > 0:
        out += _skip_op(k0)
    out += bytes([0x01, 0x01])
    if k1 > 0:
        out += _skip_op(k1)
    return bytes(out)


def parse_step(data: bytes, offset: int) -> tuple[int, int, int]:
    """Decode one FlatDictionary command. Returns (discriminator, param, next_offset)."""
    cmd = data[offset]
    width = cmd & 0x0F
    disc = (cmd >> 4) & 0x0F
    param = int.from_bytes(data[offset + 1 : offset + 1 + width], "little") if width else 0
    return disc, param, offset + 1 + width


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    """Read a 1-byte-length-prefixed string (cube firmware uses 1 byte)."""
    length = data[offset]
    end = offset + 1 + length
    return data[offset + 1 : end].decode("utf-8", errors="replace"), end



def _consume_subkey(data: bytes, offset: int, sub_key: int,
                    field_size: int | None) -> tuple[object, int]:
    """Decode one metadata sub-dict value at `sub_key`."""
    if sub_key in _STRING_KEYS:
        return _read_string(data, offset)
    if sub_key == META_SPAN:
        return int.from_bytes(data[offset : offset + 4], "little"), offset + 4
    if sub_key in (META_MIN_VALUE, META_MAX_VALUE):
        # Typed by the field's own type/size, which appears earlier in the
        # same sub-dict. Fall back to 1 byte if size hasn't been seen yet.
        width = field_size if field_size and field_size > 0 else 1
        return int.from_bytes(data[offset : offset + width], "little"), offset + width
    # Everything else (type/size/gettable/settable/idempotent/debug/system)
    # is a single byte UINT/BOOLEAN.
    return data[offset], offset + 1


def parse_metadata_subdict(data: bytes) -> dict[int, object]:
    """Parse a metadata sub-dictionary that runs to the end of `data`.

    ENUMERATE_FIELDS responses do not length-prefix the sub-dict: the cube
    emits it directly after the outer `01 01` run-1 command, with each
    sub-key as its own run-1 and `1N <skip>` ops for unpopulated keys.
    """
    out: dict[int, object] = {}
    key_acc = 0
    offset = 0
    while offset < len(data):
        disc, param, offset = parse_step(data, offset)
        if disc == 1:
            key_acc += param
            continue
        if disc == 2:  # END_DICTIONARY — never observed on the wire
            break
        if disc != 0:
            raise ValueError(f"unknown sub-dict discriminator {disc} at offset {offset}")
        for _ in range(param):
            seen = out.get(META_SIZE)
            field_size = seen if isinstance(seen, int) else None
            value, offset = _consume_subkey(data, offset, key_acc, field_size)
            out[key_acc] = value
            key_acc += 1
    return out


def parse_enumerate_response(data: bytes) -> dict[int, dict]:
    """Parse an ENUMERATE_FIELDS response into {relative_key: metadata}.

    The outer dictionary is constrained: optional leading SKIPs, then at
    most one RUN-1 whose value (a sub-dict) consumes the rest of the
    buffer. An empty result means "no field at the queried key".
    """
    out: dict[int, dict] = {}
    key_acc = 0
    offset = 0
    while offset < len(data):
        disc, param, offset = parse_step(data, offset)
        if disc == 1:
            key_acc += param
            continue
        if disc == 2:
            break
        if disc != 0:
            raise ValueError(f"unknown discriminator {disc} at offset {offset}")
        for _ in range(param):
            meta = parse_metadata_subdict(data[offset:])
            out[key_acc] = meta
            key_acc += 1
            # The sub-dict consumed the remainder of the buffer.
            offset = len(data)
    return out


def spec_from_metadata(floor: int, meta: Mapping[int, object]) -> FieldSpec:
    """Build a `FieldSpec` from a parsed metadata sub-dictionary."""
    def _int(key: int, default: int = 0) -> int:
        value = meta.get(key, default)
        return value if isinstance(value, int) else default

    def _bool(key: int) -> bool:
        return bool(meta.get(key, 0))

    def _str(key: int) -> str:
        value = meta.get(key, "")
        return value if isinstance(value, str) else ""

    min_value = meta.get(META_MIN_VALUE)
    max_value = meta.get(META_MAX_VALUE)
    return FieldSpec(
        floor=floor,
        span=max(1, _int(META_SPAN, 1)),
        type=_int(META_TYPE, 0),
        size=_int(META_SIZE, 0),
        name=_str(META_NAME),
        module=_str(META_MODULE),
        units=_str(META_UNITS),
        min_value=min_value if isinstance(min_value, int) else None,
        max_value=max_value if isinstance(max_value, int) else None,
        gettable=_bool(META_GETTABLE),
        settable=_bool(META_SETTABLE),
        system=_bool(META_SYSTEM),
        debug=_bool(META_DEBUG),
    )


# --------------------- the walk ---------------------


def _signature(meta: Mapping[int, object]) -> tuple:
    """Identity of a field block: two probes returning the same signature
    are looking at the same block (so the walk can skip repeats)."""
    return (
        meta.get(META_NAME),
        meta.get(META_TYPE),
        meta.get(META_SIZE),
        meta.get(META_SPAN),
        meta.get(META_MODULE),
        meta.get(META_SYSTEM),
    )


def probe_key(transport: Transport, node_id: int, key: int, *,
              timeout: float = 3.0) -> tuple[dict, int] | None:
    """Query the metadata of the field covering `key`.

    Returns ``(metadata, relative_key)`` — the parsed sub-dict and the
    relative key the response echoed / pointed at — or None when the
    firmware did not answer.
    """
    try:
        future = transport.send_request(
            dest_id=node_id,
            type_id=TYPE_ENUMERATE_FIELDS,
            payload=build_query(key),
            timeout=timeout,
        )
        response = future.result(timeout=timeout + 1.0)
    except Exception as exc:  # noqa: BLE001 — timeouts are normal for gaps
        log.debug("ENUMERATE_FIELDS probe at key %d failed: %s", key, exc)
        return None
    if not response:
        return None
    try:
        parsed = parse_enumerate_response(response)
    except Exception:  # noqa: BLE001 — malformed response, treat as no answer
        log.debug("ENUMERATE_FIELDS probe at key %d returned unparsable data", key,
                  exc_info=True)
        return None
    if not parsed:
        return None
    rel_key = next(iter(parsed))
    return parsed[rel_key], rel_key


def _classify(transport: Transport, node_id: int, key: int, sig: tuple,
              timeout: float, *, known_rel: int | None = None) -> tuple[str, int]:
    """Decide what the cube's response at `key` means for block `sig`.

    Returns one of:
      ("nope", _)        — different block, or no response
      ("at_floor", key)  — `key` is the block's floor (rel == 0)
      ("echo", key)      — `key` is inside the block, floor unknown
      ("gap", floor)     — `key` is in a gap; the block starts at `floor`

    Pass `known_rel` when the caller already holds the response for `key`
    (its leading skip was `known_rel`) — that saves a redundant probe.

    The ``rel == key`` case is ambiguous (echo of `key`, or a gap whose
    relative skip happens to equal `key`); probing `key + 1` resolves it,
    because under a gap the relative skip decreases by one and under an
    echo it tracks `key`.
    """
    if known_rel is None:
        result = probe_key(transport, node_id, key, timeout=timeout)
        if result is None:
            return "nope", 0
        meta, rel = result
        if _signature(meta) != sig:
            return "nope", 0
    else:
        rel = known_rel
    if rel == 0:
        return "at_floor", key
    if rel != key:
        return "gap", key + rel
    result2 = probe_key(transport, node_id, key + 1, timeout=timeout)
    if result2 is None:
        # Past the end of the schema: `key` was the final block's floor.
        return "at_floor", key
    meta2, rel2 = result2
    if _signature(meta2) != sig:
        # A different block sits at key+1, so `key` was the last key of ours.
        return "echo", key
    if rel2 != key + 1:
        return "gap", (key + 1) + rel2
    return "echo", key


def _resolve_floor(transport: Transport, node_id: int, hit_key: int, hit_rel: int,
                   sig: tuple, timeout: float) -> int:
    """Find the real floor of block `sig`, given a hit at `hit_key` whose
    response leading-skip was `hit_rel`."""
    if hit_rel == 0:
        return hit_key
    if hit_rel != hit_key:
        return hit_key + hit_rel
    kind, value = _classify(transport, node_id, hit_key, sig, timeout,
                            known_rel=hit_rel)
    if kind == "at_floor":
        return value
    if kind == "nope":
        return hit_key
    if kind == "gap":
        return value
    # "echo": the floor lies within [hit_key - span + 1, hit_key].
    span = sig[3] if isinstance(sig[3], int) and sig[3] > 0 else 1
    lo = max(0, hit_key - span + 1)
    hi = hit_key
    while lo < hi:
        mid = (lo + hi) // 2
        kind, value = _classify(transport, node_id, mid, sig, timeout)
        if kind == "nope":
            lo = mid + 1
        elif kind == "at_floor":
            return value
        elif kind == "gap":
            return value
        else:  # still inside the block at mid: floor <= mid
            hi = mid
    return lo


def enumerate_fields(transport: Transport, node_id: int, *,
                     extra_probe_keys: Iterable[int] = (),
                     max_iters: int = 400, max_empty: int = 4,
                     timeout: float = 3.0,
                     max_seconds: float | None = None) -> dict[int, FieldSpec]:
    """Walk a node's ENUMERATE_FIELDS schema.

    Returns ``{floor: FieldSpec}``. The walk follows the firmware's
    "next field at floor >= k" hints, stepping to ``floor + span`` after
    each hit, and gives up after `max_empty` consecutive empty responses
    or once `max_seconds` have elapsed (a partial schema is still useful —
    it just cannot decode the fields it is missing).

    `extra_probe_keys` are probed **first**, on a fresh service session:
    the cube has been observed to stop answering ENUMERATE_FIELDS probes
    at these keys once a long walk has run (see PROTOCOL.md §5.1).
    """
    specs: dict[int, FieldSpec] = {}
    seen: set[tuple] = set()
    deadline = None if max_seconds is None else time.monotonic() + max_seconds

    for key in extra_probe_keys:
        result = probe_key(transport, node_id, key, timeout=timeout)
        if result is None:
            log.debug("extra probe at key %d: no response", key)
            continue
        meta, rel = result
        sig = _signature(meta)
        if sig in seen:
            continue
        seen.add(sig)
        floor = _resolve_floor(transport, node_id, key, rel, sig, timeout)
        specs[floor] = spec_from_metadata(floor, meta)

    abs_key = 0
    empty_streak = 0
    for _ in range(max_iters):
        if deadline is not None and time.monotonic() > deadline:
            log.info("schema walk on node %d hit its %.1fs budget with %d block(s)",
                     node_id, max_seconds, len(specs))
            break
        result = probe_key(transport, node_id, abs_key, timeout=timeout)
        if result is None:
            empty_streak += 1
            if empty_streak >= max_empty:
                break
            abs_key += 1
            continue
        empty_streak = 0
        meta, rel = result
        sig = _signature(meta)
        if sig in seen:
            abs_key += 1
            continue
        seen.add(sig)
        floor = _resolve_floor(transport, node_id, abs_key, rel, sig, timeout)
        spec = spec_from_metadata(floor, meta)
        specs[floor] = spec
        abs_key = floor + max(1, spec.span)

    log.info("discovered %d field block(s) on node %d", len(specs), node_id)
    log.debug("node %d schema: %s", node_id,
              ", ".join(f"{s.floor}:{s.name}" for s in sorted(specs.values(),
                                                             key=lambda s: s.floor)))
    return specs


