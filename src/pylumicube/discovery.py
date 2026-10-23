"""Runtime field-metadata discovery via ENUMERATE_FIELDS (PROTOCOL §4.5).

The wire protocol encodes values according to per-field metadata: type,
size in bytes, and the floor/span of the homogeneous block a key lives in.
`metadata.py` hardcodes the display schema we already know; this module
discovers the rest from the firmware, which is what telemetry decoding and
the module drivers (screen, speaker) need.

Two things about the response format are easy to get wrong, and both were
confirmed against the Java daemon's own parser (`RecursiveDictionary.java`)
and against raw hardware captures:

1. **The leading skip is an absolute field key.** The daemon parses a
   response with its key accumulator starting at 0, so a leading
   ``skip 256`` means "the field in this response is at key 256" — *not*
   "256 past the key you asked about". Reading it as relative produces
   plausible-looking but wrong keys; that is how earlier notes in this repo
   ended up with 3315 for `screen.rectangle_x` (it is 2048) and 60014 for
   `rdp_level` (it is 60000).
2. **A field's ``min_value``/``max_value`` are variable-size**, i.e. they
   carry a 1-byte length prefix, because the bootstrap metadata declares
   them as ``UINT, size = 0``. So ``01 01 01 64`` is max_value = 100, not
   "value 1, then a stray command byte".

The walk follows the daemon's `QueryMetadataTask`: query ``(key0, key1)``
means "skip to field key0, take one field, start its sub-dictionary at
sub-key key1", then continue with ``(lastFieldKey, lastSubKey + 1)``. Once
the requested sub-key is past the end of that field's sub-dictionary, the
firmware advances to the **next field in its internal table order**, which
is not key order — that is how the whole `screen` and `speaker` modules
become reachable at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping

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

#: Sub-keys whose values are UTF8 strings (1-byte length prefix on the cube).
_STRING_KEYS = (META_NAME, META_UNITS, META_MODULE)
#: Sub-keys declared `UINT, size = 0` — also length-prefixed, not fixed width.
_VARIABLE_INT_KEYS = (META_MIN_VALUE, META_MAX_VALUE)


def _skip_op(value: int) -> bytes:
    """Encode a FlatDictionary skip with the narrowest width that fits."""
    if value <= 0xFF:
        return bytes([0x11, value])
    if value <= 0xFFFF:
        return bytes([0x12]) + value.to_bytes(2, "little")
    if value <= 0xFFFFFF:
        return bytes([0x13]) + value.to_bytes(3, "little")
    if value <= 0xFFFFFFFF:
        return bytes([0x14]) + value.to_bytes(4, "little")
    raise ValueError(f"skip value {value} doesn't fit in 4 bytes")


def build_query(key0: int, key1: int = 0) -> bytes:
    """Build an ENUMERATE_FIELDS query in the daemon's shape.

    ``skip key0`` (absolute), ``run 1`` (one field), ``skip key1`` (start
    that field's sub-dictionary at sub-key key1). Both skips carry a 2-byte
    parameter, exactly as `QueryMetadataTask.java` emits them: a sub-key
    past the end of the current field's sub-dictionary is what makes the
    firmware step to the next field in its table.
    """
    if key0 < 0 or key1 < 0:
        raise ValueError("keys must be non-negative")
    return bytes([
        0x12, key0 & 0xFF, (key0 >> 8) & 0xFF,
        0x01, 0x01,
        0x12, key1 & 0xFF, (key1 >> 8) & 0xFF,
    ])


def parse_step(data: bytes, offset: int) -> tuple[int, int, int]:
    """Decode one FlatDictionary command. Returns (discriminator, param, next_offset)."""
    cmd = data[offset]
    width = cmd & 0x0F
    disc = (cmd >> 4) & 0x0F
    param = int.from_bytes(data[offset + 1 : offset + 1 + width], "little") if width else 0
    return disc, param, offset + 1 + width


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    """Read a 1-byte-length-prefixed string (the cube firmware uses 1 byte)."""
    length = data[offset]
    end = offset + 1 + length
    return data[offset + 1 : end].decode("utf-8", errors="replace"), end


def _read_variable_int(data: bytes, offset: int) -> tuple[int, int]:
    """Read a length-prefixed unsigned integer (min_value/max_value)."""
    length = data[offset]
    end = offset + 1 + length
    return int.from_bytes(data[offset + 1 : end], "little"), end


def _consume_subkey(data: bytes, offset: int, sub_key: int) -> tuple[object, int]:
    """Decode one metadata sub-dict value at `sub_key`."""
    if sub_key in _STRING_KEYS:
        return _read_string(data, offset)
    if sub_key == META_SPAN:
        return int.from_bytes(data[offset : offset + 4], "little"), offset + 4
    if sub_key in _VARIABLE_INT_KEYS:
        return _read_variable_int(data, offset)
    # type/size/gettable/settable/idempotent/debug/system are 1-byte UINT/BOOLEAN.
    return data[offset], offset + 1


def parse_metadata_subdict(data: bytes) -> dict[int, object]:
    """Parse a metadata sub-dictionary that runs to the end of `data`.

    ENUMERATE_FIELDS responses do not length-prefix the sub-dictionary: the
    cube emits it directly after the field's run-1 command, with each
    sub-key as its own run-1 and `1N <skip>` ops for unpopulated keys. It
    therefore always occupies the rest of the buffer, which is also why a
    response carries exactly one field.
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
            value, offset = _consume_subkey(data, offset, key_acc)
            out[key_acc] = value
            key_acc += 1
    return out


def parse_metadata_response(data: bytes) -> dict[int, dict]:
    """Parse an ENUMERATE_FIELDS response into ``{field_key: metadata}``.

    Field keys are **absolute**: the outer dictionary starts at key 0, so
    its leading skip carries the field's real key (see the module
    docstring). An empty result means "no field there".
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
            # The sub-dictionary consumed the remainder of the buffer.
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


def query_metadata(transport: Transport, node_id: int, key0: int, key1: int,
                   *, timeout: float = 2.0) -> dict[int, dict] | None:
    """Send one ``(key0, key1)`` metadata query.

    Returns ``{field_key: metadata}`` (normally one field). An **empty dict**
    means the firmware answered and there is no field there (the end of the
    table); ``None`` means it did not answer at all — a distinction the walk
    needs, because a lost reply must be retried rather than taken as the end.
    """
    try:
        future = transport.send_request(
            dest_id=node_id,
            type_id=TYPE_ENUMERATE_FIELDS,
            payload=build_query(key0, key1),
            timeout=timeout,
        )
        response = future.result(timeout=timeout + 1.0)
    except Exception as exc:  # noqa: BLE001 — no answer is a normal outcome
        log.debug("metadata query (%d,%d) on node %d failed: %s", key0, key1, node_id, exc)
        return None
    if not response:
        return {}
    try:
        return parse_metadata_response(response)
    except Exception:  # noqa: BLE001 — treat malformed data as "no answer"
        log.debug("metadata query (%d,%d) on node %d returned unparsable data: %s",
                  key0, key1, node_id, response.hex(), exc_info=True)
        return None


def probe_key(transport: Transport, node_id: int, key: int, *,
              timeout: float = 2.0) -> tuple[dict, int] | None:
    """Query the metadata of the field covering `key`.

    Returns ``(metadata, field_key)`` — the parsed sub-dictionary and the
    field's absolute key — or None when the firmware did not answer.
    """
    parsed = query_metadata(transport, node_id, key, 0, timeout=timeout)
    if not parsed:
        return None
    field_key = next(iter(parsed))
    return parsed[field_key], field_key


def walk_metadata(transport: Transport, node_id: int, *,
                  max_queries: int = 400, timeout: float = 2.0,
                  max_seconds: float | None = None,
                  retries: int = 2, max_failures: int = 4) -> dict[int, FieldSpec]:
    """Walk a node's whole metadata table in the firmware's own order.

    Implements the daemon's `QueryMetadataTask`: each response advances the
    cursor to ``(last field key, last sub-key + 1)``; the walk ends when the
    firmware answers with no field.

    This link is lossy — replies are dropped and the daemon logs
    retransmissions constantly — so a query that goes unanswered is retried
    (`retries` times) before the walk gives up, and only `max_failures`
    consecutive silent queries end it. Without that, one lost reply
    truncates the schema and whole modules (screen, speaker) go missing.
    """
    specs: dict[int, FieldSpec] = {}
    key0, key1 = 0, 0
    failures = 0
    deadline = None if max_seconds is None else time.monotonic() + max_seconds

    for step in range(max_queries):
        if deadline is not None and time.monotonic() > deadline:
            log.info("metadata walk on node %d hit its %.1fs budget with %d field(s)",
                     node_id, max_seconds, len(specs))
            break
        parsed = None
        for _attempt in range(retries + 1):
            parsed = query_metadata(transport, node_id, key0, key1, timeout=timeout)
            if parsed is not None:
                break
        if parsed is None:
            failures += 1
            if failures >= max_failures:
                log.warning("metadata walk on node %d gave up after %d silent "
                            "queries (%d field(s) so far)",
                            node_id, failures, len(specs))
                break
            continue
        if not parsed:
            log.debug("metadata walk on node %d ended after %d queries",
                      node_id, step)
            break
        failures = 0
        for field_key, meta in parsed.items():
            specs[field_key] = spec_from_metadata(field_key, meta)
        last_field = max(parsed)
        last_sub = max(parsed[last_field])
        key0, key1 = last_field, last_sub + 1
    else:
        log.warning("metadata walk on node %d stopped at max_queries=%d (%d field(s))",
                    node_id, max_queries, len(specs))

    log.info("discovered %d field(s) on node %d", len(specs), node_id)
    log.debug("node %d schema: %s", node_id,
              ", ".join(f"{key}:{spec.name}" for key, spec in sorted(specs.items())))
    return specs


def enumerate_fields(transport: Transport, node_id: int, *,
                     extra_probe_keys: Iterable[int] = (),
                     max_iters: int = 400, timeout: float = 2.0,
                     max_seconds: float | None = None) -> dict[int, FieldSpec]:
    """Discover a node's field schema.

    Runs the table-order walk (`walk_metadata`), then probes any
    `extra_probe_keys` explicitly and merges what they report — a safety net
    for fields whose table position the firmware skips.
    """
    specs = walk_metadata(transport, node_id, max_queries=max_iters,
                          timeout=timeout, max_seconds=max_seconds)
    for key in extra_probe_keys:
        result = probe_key(transport, node_id, key, timeout=timeout)
        if result is None:
            log.debug("extra probe at key %d: no response", key)
            continue
        meta, field_key = result
        if field_key not in specs:
            specs[field_key] = spec_from_metadata(field_key, meta)
    return specs

