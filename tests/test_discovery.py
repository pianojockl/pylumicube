"""ENUMERATE_FIELDS parsing + schema-walk tests (no hardware).

The walk's hard part is that the firmware never states a block's floor:
a probe at key `k` returns the field covering `k` plus a leading skip
that is either the *echo* of `k` (inside a block) or the *relative
distance* to the next block (in a gap), and the two are indistinguishable
when `rel == k`. `FakeEnumTransport` emulates exactly that, so the tests
below pin the disambiguation and the backward binary search.
"""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from pylumicube import discovery
from pylumicube.constants import (
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_ENUM,
    FIELD_TYPE_INT,
    FIELD_TYPE_UINT,
)
from pylumicube.discovery import (
    CUBE_WELL_KNOWN_KEYS,
    build_query,
    enumerate_fields,
    parse_enumerate_response,
    parse_metadata_subdict,
    probe_key,
    spec_from_metadata,
)


# ----- encoders (mirror the cube's emission style) ------------------------


def _string(value: str) -> bytes:
    return bytes([len(value)]) + value.encode()


def encode_metadata(*, name: str = "", type_: int = FIELD_TYPE_UINT, size: int = 1,
                    span: int = 1, module: str = "", units: str = "",
                    min_value: int | None = None, max_value: int | None = None,
                    gettable: bool = True, settable: bool = False,
                    system: bool = False, debug: bool = False) -> bytes:
    """Encode a metadata sub-dictionary the way the cube does.

    Each populated sub-key is its own `01 01 <value>` run-1; unpopulated
    keys are covered by `1N <skip>` commands.
    """
    out = bytearray()
    key_acc = 0

    def emit(sub_key: int, payload: bytes) -> None:
        nonlocal key_acc
        gap = sub_key - key_acc
        if gap > 0:
            out.extend(discovery._skip_op(gap))
        out.extend(bytes([0x01, 0x01]))
        out.extend(payload)
        key_acc = sub_key + 1

    if name:
        emit(discovery.META_NAME, _string(name))
    emit(discovery.META_TYPE, bytes([type_]))
    emit(discovery.META_SIZE, bytes([size]))
    emit(discovery.META_SPAN, span.to_bytes(4, "little"))
    if gettable:
        emit(discovery.META_GETTABLE, bytes([1]))
    if settable:
        emit(discovery.META_SETTABLE, bytes([1]))
    if min_value is not None:
        emit(discovery.META_MIN_VALUE, min_value.to_bytes(max(1, size), "little"))
    if max_value is not None:
        emit(discovery.META_MAX_VALUE, max_value.to_bytes(max(1, size), "little"))
    if units:
        emit(discovery.META_UNITS, _string(units))
    if debug:
        emit(discovery.META_DEBUG, bytes([1]))
    if system:
        emit(discovery.META_SYSTEM, bytes([1]))
    if module:
        emit(discovery.META_MODULE, _string(module))
    return bytes(out)


def encode_response(rel_key: int, subdict: bytes) -> bytes:
    """A full ENUMERATE_FIELDS response: leading skip, run-1, sub-dict."""
    out = bytearray()
    if rel_key > 0:
        out.extend(discovery._skip_op(rel_key))
    out.extend(bytes([0x01, 0x01]))
    out.extend(subdict)
    return bytes(out)


class FakeEnumTransport:
    """Emulates the firmware's ENUMERATE_FIELDS behaviour for a schema."""

    def __init__(self, blocks: list[tuple[int, int, bytes]]) -> None:
        # (floor, span, encoded metadata sub-dict)
        self._blocks = sorted(blocks)
        self.queries: list[int] = []
        self.answer = True

    def _covering(self, key: int) -> tuple[int, int, bytes] | None:
        for floor, span, meta in self._blocks:
            if floor <= key < floor + span:
                return floor, span, meta
        return None

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     timeout: float = 5.0, priority: int = 20) -> Future:
        # Payload is `<skip><01 01>`; recover the queried key.
        key = 0
        if payload and (payload[0] >> 4) == 1:
            width = payload[0] & 0x0F
            key = int.from_bytes(payload[1 : 1 + width], "little")
        self.queries.append(key)
        future: Future = Future()
        if not self.answer:
            future.set_result(b"")
            return future
        hit = self._covering(key)
        if hit is None:
            ahead = [floor for floor, _span, _meta in self._blocks if floor > key]
            if ahead:
                future.set_result(encode_response(ahead[0] - key, self._blocks[0][2]))
            else:
                future.set_result(b"")   # past the last block: no response
            return future
        floor, _span, meta = hit
        # Inside a block the firmware echoes the queried key.
        future.set_result(encode_response(key, meta))
        return future


# ----- metadata parsing ---------------------------------------------------


def test_parse_metadata_subdict_round_trip() -> None:
    raw = encode_metadata(name="ambient_light", type_=FIELD_TYPE_UINT, size=4, span=1,
                          module="light_sensor", gettable=True)
    meta = parse_metadata_subdict(raw)
    assert meta[discovery.META_NAME] == "ambient_light"
    assert meta[discovery.META_TYPE] == FIELD_TYPE_UINT
    assert meta[discovery.META_SIZE] == 4
    assert meta[discovery.META_SPAN] == 1
    assert meta[discovery.META_MODULE] == "light_sensor"
    assert meta[discovery.META_GETTABLE] == 1


def test_parse_metadata_subdict_handles_skips() -> None:
    """Only `module` populated: everything before it is skipped."""
    raw = encode_metadata(module="buttons")
    meta = parse_metadata_subdict(raw)
    assert meta[discovery.META_MODULE] == "buttons"
    assert discovery.META_NAME not in meta


def test_parse_enumerate_response_relative_key() -> None:
    sub = encode_metadata(name="top_pressed", type_=FIELD_TYPE_BOOLEAN, size=1)
    response = encode_response(6, sub)
    parsed = parse_enumerate_response(response)
    assert list(parsed) == [6]
    assert parsed[6][discovery.META_NAME] == "top_pressed"


def test_spec_from_metadata_carries_flags_and_limits() -> None:
    meta = parse_metadata_subdict(encode_metadata(
        name="brightness", type_=FIELD_TYPE_UINT, size=1, span=1, module="display",
        min_value=0, max_value=100, settable=True, system=True, debug=True))
    spec = spec_from_metadata(256, meta)
    assert (spec.floor, spec.span, spec.type, spec.size) == (256, 1, FIELD_TYPE_UINT, 1)
    assert spec.name == "brightness"
    assert spec.module == "display"
    assert (spec.min_value, spec.max_value) == (0, 100)
    assert spec.settable and spec.system and spec.debug and spec.gettable
    assert spec.covers(256) and not spec.covers(257)


def test_build_query_encodes_skip_width() -> None:
    assert build_query(0) == bytes([0x01, 0x01])
    assert build_query(6) == bytes([0x11, 0x06, 0x01, 0x01])
    assert build_query(0x1234) == bytes([0x12, 0x34, 0x12, 0x01, 0x01])


# ----- the walk -----------------------------------------------------------


def _sensor_like_schema() -> FakeEnumTransport:
    return FakeEnumTransport([
        (0, 1, encode_metadata(name="top_pressed", type_=FIELD_TYPE_BOOLEAN, size=1,
                               module="buttons")),
        (1, 1, encode_metadata(name="top_pressed_count", type_=FIELD_TYPE_UINT, size=4,
                               module="buttons")),
        (6, 1, encode_metadata(name="ambient_light", type_=FIELD_TYPE_UINT, size=4,
                               module="light_sensor")),
    ])


def test_probe_key_returns_none_past_last_block() -> None:
    transport = FakeEnumTransport([(0, 1, encode_metadata(name="a"))])
    assert probe_key(transport, 124, 5) is None


def test_walk_finds_contiguous_and_gapped_blocks() -> None:
    transport = _sensor_like_schema()
    specs = enumerate_fields(transport, 124, max_empty=2)
    assert sorted(specs) == [0, 1, 6]
    assert specs[0].name == "top_pressed"
    assert specs[1].name == "top_pressed_count"
    assert specs[6].name == "ambient_light"
    assert specs[6].module == "light_sensor"
    assert specs[6].size == 4


def test_walk_resolves_floor_inside_a_wide_block() -> None:
    """The ambiguous `rel == k` case: probe lands inside a 999-wide block."""
    transport = FakeEnumTransport([
        (266, 999, encode_metadata(name="led_colour", type_=FIELD_TYPE_UINT, size=3,
                                   span=999, module="display")),
    ])
    specs = enumerate_fields(transport, 125, max_empty=1)
    assert list(specs) == [266]
    assert specs[266].span == 999


def test_walk_resolves_floor_when_gap_distance_equals_key() -> None:
    """`rel == k` can also mean a gap whose distance happens to equal k."""
    transport = FakeEnumTransport([
        (10, 1, encode_metadata(name="late", type_=FIELD_TYPE_INT, size=2)),
    ])
    specs = enumerate_fields(transport, 124, max_empty=1)
    assert list(specs) == [10]
    assert specs[10].name == "late"


def test_walk_extra_probes_run_first() -> None:
    """Extra probes must precede the walk (the firmware stops answering
    those keys after a long walk — see PROTOCOL.md §5.1)."""
    transport = FakeEnumTransport([
        (0, 1, encode_metadata(name="data", type_=FIELD_TYPE_INT, size=2, span=112,
                               module="microphone")),
        (256, 1, encode_metadata(name="brightness", type_=FIELD_TYPE_UINT, size=1,
                                 module="display")),
    ])
    specs = enumerate_fields(transport, 125, extra_probe_keys=(256,), max_empty=1)
    assert sorted(specs) == [0, 256]
    assert transport.queries[0] == 256
    assert specs[256].name == "brightness"


def test_walk_respects_time_budget() -> None:
    transport = _sensor_like_schema()
    specs = enumerate_fields(transport, 124, max_seconds=0.0)
    assert specs == {}


def test_walk_gives_up_after_consecutive_empty_responses() -> None:
    transport = FakeEnumTransport([(0, 1, encode_metadata(name="only"))])
    specs = enumerate_fields(transport, 124, max_empty=3)
    assert sorted(specs) == [0]
    # 0 (hit) + 1,2,3 (empty streak of 3) = 4 queries.
    assert transport.queries == [0, 1, 2, 3]


def test_cube_well_known_keys_are_probed() -> None:
    """The cube's walk skips the display block's non-LED fields."""
    transport = FakeEnumTransport([
        (0, 1, encode_metadata(name="data", type_=FIELD_TYPE_INT, size=2, span=112,
                               module="microphone")),
        (256, 1, encode_metadata(name="brightness", type_=FIELD_TYPE_UINT, size=1,
                                 module="display")),
        (266, 999, encode_metadata(name="led_colour", type_=FIELD_TYPE_UINT, size=3,
                                   span=999, module="display")),
    ])
    specs = enumerate_fields(transport, 125, extra_probe_keys=CUBE_WELL_KNOWN_KEYS,
                             max_empty=1)
    assert 256 in specs and specs[256].name == "brightness"
    assert 266 in specs


def test_walk_stops_when_firmware_never_answers() -> None:
    transport = _sensor_like_schema()
    transport.answer = False
    assert enumerate_fields(transport, 124, max_empty=2) == {}


def test_enum_type_is_decoded_as_signed_metadata() -> None:
    meta = parse_metadata_subdict(encode_metadata(name="last_gesture",
                                                  type_=FIELD_TYPE_ENUM, size=1))
    spec = spec_from_metadata(10, meta)
    assert spec.type == FIELD_TYPE_ENUM
    assert spec.signed is True


@pytest.mark.parametrize("key", [0, 1, 6])
def test_probe_key_hits_every_block(key: int) -> None:
    transport = _sensor_like_schema()
    result = probe_key(transport, 124, key)
    assert result is not None
    meta, rel = result
    assert rel == key
    assert discovery.META_NAME in meta


