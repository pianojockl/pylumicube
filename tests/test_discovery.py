"""Metadata discovery tests (no hardware).

The fixtures are **real firmware bytes** captured from the cube
(2026-10-23), because the two things that are easy to get wrong here are
invisible to synthetic data written from the same misunderstanding:

* the response's leading skip is an *absolute* field key (so
  `speaker.data` is at 128, `display.brightness` at 256, `screen.rectangle_x`
  at 2048 — not the 241/3315 an earlier relative reading produced);
* `min_value`/`max_value` are length-prefixed variable-size integers
  (`01 01 01 64` is max_value = 100).

`FakeFirmware` emulates the daemon's walk protocol: `(key0, key1)` returns
field `key0` with its sub-dictionary from `key1` onward, and once `key1`
is past that sub-dictionary the *next field in table order* comes back.
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
    build_query,
    enumerate_fields,
    parse_metadata_response,
    parse_metadata_subdict,
    probe_key,
    spec_from_metadata,
    walk_metadata,
)


# ----- real captured responses -------------------------------------------


SPEAKER_DATA = "11800101010104646174610101050101020101700000001108010107737065616b6572"
DISPLAY_BRIGHTNESS = (
    "120001010101010a6272696768746e657373010104010101010101000000"
    "1104010101641103010107646973706c6179"
)
LED_COLOUR = (
    "126101010101010a6c65645f636f6c6f75720101040101030101e70300001108010107646973706c6179"
)
SCREEN_RECTANGLE_X = (
    "120008010101010b72656374616e676c655f78010104010102010101000000110801010673637265656e"
)
BUTTON_PRESSED = (
    "120010010101010e627574746f6e5f70726573736564010103010101010101000000"
    "11010101001106010107627574746f6e73"
)
SENSOR_TOP_PRESSED = (
    "010101010b746f705f7072657373656401010301010101010100000011010101001106010107627574746f6e73"
)


def _only_field(hex_data: str) -> tuple[int, dict]:
    parsed = parse_metadata_response(bytes.fromhex(hex_data))
    key = next(iter(parsed))
    return key, parsed[key]


def test_leading_skip_is_an_absolute_field_key() -> None:
    """Probing a gap returns the *next* field, with its real key."""
    parsed = parse_metadata_response(bytes.fromhex(SPEAKER_DATA))
    assert list(parsed) == [128]                      # not 113 + 128
    assert parsed[128][discovery.META_NAME] == "data"
    assert parsed[128][discovery.META_MODULE] == "speaker"


@pytest.mark.parametrize("hex_data,expected_key,expected_name,expected_module", [
    (DISPLAY_BRIGHTNESS, 256, "brightness", "display"),
    (LED_COLOUR, 353, "led_colour", "display"),
    (SCREEN_RECTANGLE_X, 2048, "rectangle_x", "screen"),
    (BUTTON_PRESSED, 4096, "button_pressed", "buttons"),
    (SENSOR_TOP_PRESSED, 0, "top_pressed", "buttons"),
])
def test_captured_responses_decode_to_their_real_keys(
        hex_data: str, expected_key: int, expected_name: str,
        expected_module: str) -> None:
    parsed = parse_metadata_response(bytes.fromhex(hex_data))
    assert list(parsed) == [expected_key]
    spec = spec_from_metadata(expected_key, parsed[expected_key])
    assert spec.name == expected_name
    assert spec.module == expected_module


def test_min_and_max_value_are_length_prefixed() -> None:
    """`01 01 01 64` is max_value = 100, not "1" plus a stray command byte."""
    _key, meta = _only_field(DISPLAY_BRIGHTNESS)
    assert meta[discovery.META_MAX_VALUE] == 100
    assert discovery.META_MIN_VALUE not in meta        # not reported by the firmware


def test_captured_screen_field_metadata() -> None:
    spec = spec_from_metadata(*_only_field(SCREEN_RECTANGLE_X))
    assert (spec.name, spec.type, spec.size, spec.span, spec.module) == (
        "rectangle_x", FIELD_TYPE_UINT, 2, 1, "screen")


# ----- encoders (mirror the cube's emission style) ------------------------


def _string(value: str) -> bytes:
    return bytes([len(value)]) + value.encode()


def encode_metadata(*, name: str = "", type_: int = FIELD_TYPE_UINT, size: int = 1,
                    span: int = 1, module: str = "", units: str = "",
                    min_value: int | None = None, max_value: int | None = None,
                    gettable: bool = True, settable: bool = False,
                    system: bool = False, debug: bool = False) -> bytes:
    """Encode a metadata sub-dictionary the way the cube does."""
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

    def emit_variable(sub_key: int, value: int) -> None:
        payload = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "little")
        emit(sub_key, bytes([len(payload)]) + payload)

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
        emit_variable(discovery.META_MIN_VALUE, min_value)
    if max_value is not None:
        emit_variable(discovery.META_MAX_VALUE, max_value)
    if units:
        emit(discovery.META_UNITS, _string(units))
    if debug:
        emit(discovery.META_DEBUG, bytes([1]))
    if system:
        emit(discovery.META_SYSTEM, bytes([1]))
    if module:
        emit(discovery.META_MODULE, _string(module))
    return bytes(out)


def encode_response(field_key: int, subdict: bytes) -> bytes:
    """A full response: absolute skip to the field key, run-1, sub-dict."""
    out = bytearray()
    if field_key > 0:
        out.extend(discovery._skip_op(field_key))
    out.extend(bytes([0x01, 0x01]))
    out.extend(subdict)
    return bytes(out)


class FakeFirmware:
    """Emulates the firmware's metadata table + the daemon's query shape."""

    def __init__(self, table: list[tuple[int, int, bytes]]) -> None:
        # (key, span, encoded metadata sub-dict), in *table order*.
        self.table = list(table)
        self.queries: list[tuple[int, int]] = []
        self.answer = True

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     timeout: float = 5.0, priority: int = 20) -> Future:
        # Payload: skip key0, run 1, skip key1.
        key0 = int.from_bytes(payload[1:3], "little")
        key1 = int.from_bytes(payload[6:8], "little")
        self.queries.append((key0, key1))
        future: Future = Future()
        if not self.answer:
            future.set_result(b"")
            return future
        # "Skip to key0" means the first field *at or after* key0: the
        # firmware answers a gap query with the next field's real key.
        index = next((i for i, (key, _span, _meta) in enumerate(self.table)
                      if key >= key0), None)
        if index is None:
            future.set_result(b"")
            return future
        key, _span, meta = self.table[index]
        if key == key0 and key1 <= max(parse_metadata_subdict(meta)):
            future.set_result(encode_response(key, meta))
            return future
        # Either the query landed in a gap, or the sub-key is past the end of
        # this field: hand back the next field in table order.
        if key == key0:
            index += 1
        if index < len(self.table):
            next_key, _next_span, next_meta = self.table[index]
            future.set_result(encode_response(next_key, next_meta))
        else:
            future.set_result(b"")
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


def test_parse_metadata_subdict_handles_skips() -> None:
    meta = parse_metadata_subdict(encode_metadata(module="buttons"))
    assert meta[discovery.META_MODULE] == "buttons"
    assert discovery.META_NAME not in meta


def test_spec_from_metadata_carries_flags_and_limits() -> None:
    meta = parse_metadata_subdict(encode_metadata(
        name="brightness", type_=FIELD_TYPE_UINT, size=1, span=1, module="display",
        min_value=0, max_value=100, settable=True, system=True, debug=True))
    spec = spec_from_metadata(256, meta)
    assert (spec.floor, spec.span, spec.type, spec.size) == (256, 1, FIELD_TYPE_UINT, 1)
    assert (spec.min_value, spec.max_value) == (0, 100)
    assert spec.settable and spec.system and spec.debug and spec.gettable
    assert spec.covers(256) and not spec.covers(257)


def test_build_query_matches_the_daemon_shape() -> None:
    assert build_query(0, 0) == bytes([0x12, 0, 0, 0x01, 0x01, 0x12, 0, 0])
    assert build_query(256, 13) == bytes([0x12, 0x00, 0x01, 0x01, 0x01, 0x12, 0x0D, 0x00])


# ----- the walk -----------------------------------------------------------


def _sensor_table() -> FakeFirmware:
    return FakeFirmware([
        (0, 1, encode_metadata(name="top_pressed", type_=FIELD_TYPE_BOOLEAN, size=1,
                               module="buttons")),
        (1, 1, encode_metadata(name="top_pressed_count", type_=FIELD_TYPE_UINT, size=4,
                               module="buttons")),
        (6, 1, encode_metadata(name="ambient_light", type_=FIELD_TYPE_UINT, size=4,
                               module="light_sensor")),
    ])


def test_walk_visits_every_field_in_table_order() -> None:
    transport = _sensor_table()
    specs = walk_metadata(transport, 124)
    assert sorted(specs) == [0, 1, 6]
    assert [specs[k].name for k in sorted(specs)] == [
        "top_pressed", "top_pressed_count", "ambient_light"]
    assert transport.queries[0] == (0, 0)


def test_walk_finds_fields_whose_keys_are_not_contiguous() -> None:
    """The screen/speaker case: table order differs from key order."""
    transport = FakeFirmware([
        (128, 112, encode_metadata(name="data", type_=FIELD_TYPE_INT, size=2, span=112,
                                   module="speaker")),
        (240, 1, encode_metadata(name="volume", type_=FIELD_TYPE_UINT, size=2,
                                 module="speaker")),
        (2048, 1, encode_metadata(name="rectangle_x", type_=FIELD_TYPE_UINT, size=2,
                                  module="screen")),
        (4096, 1, encode_metadata(name="button_pressed", type_=FIELD_TYPE_BOOLEAN,
                                  size=1, module="buttons")),
    ])
    specs = walk_metadata(transport, 125)
    assert sorted(specs) == [128, 240, 2048, 4096]
    assert specs[2048].module == "screen"
    assert specs[128].span == 112


def test_walk_terminates_when_the_firmware_stops_answering() -> None:
    transport = _sensor_table()
    specs = walk_metadata(transport, 124)
    assert len(transport.queries) == len(specs) + 1     # one empty query ends it
    assert transport.queries[-1][1] > 12                # sub-key overflow


def test_walk_stops_when_nothing_answers_at_all() -> None:
    transport = _sensor_table()
    transport.answer = False
    assert walk_metadata(transport, 124) == {}


def test_walk_respects_its_time_budget() -> None:
    transport = _sensor_table()
    assert walk_metadata(transport, 124, max_seconds=0.0) == {}


def test_walk_respects_max_queries() -> None:
    transport = _sensor_table()
    assert len(walk_metadata(transport, 124, max_queries=1)) == 1


def test_probe_key_returns_the_fields_real_key() -> None:
    transport = FakeFirmware([
        (2048, 1, encode_metadata(name="rectangle_x", type_=FIELD_TYPE_UINT, size=2,
                                  module="screen")),
    ])
    result = probe_key(transport, 125, 2048)
    assert result is not None
    meta, key = result
    assert key == 2048
    assert meta[discovery.META_NAME] == "rectangle_x"


def test_probe_key_returns_none_when_there_is_no_field() -> None:
    assert probe_key(FakeFirmware([]), 124, 5) is None


def test_walk_ends_when_the_firmware_answers_with_no_field() -> None:
    """An empty *answer* ends the walk; silence is retried instead."""
    transport = _sensor_table()
    walk_metadata(transport, 124)
    # The last query is the one that returned an empty body.
    assert len(transport.queries) == len(_sensor_table().table) + 1


def test_walk_retries_a_lost_reply() -> None:
    """One dropped reply must not truncate the schema (lossy link)."""
    class LossyFirmware(FakeFirmware):
        def __init__(self, table, drop_at: int) -> None:
            super().__init__(table)
            self.drop_at = drop_at
            self.dropped = 0

        def send_request(self, dest_id, type_id, payload, timeout=5.0, priority=20):
            key0 = int.from_bytes(payload[1:3], "little")
            if key0 == self.drop_at and self.dropped == 0:
                self.dropped += 1
                self.queries.append((key0, -1))
                future: Future = Future()
                future.set_exception(TimeoutError("reply lost"))   # no answer
                return future
            return super().send_request(dest_id, type_id, payload, timeout, priority)

    transport = LossyFirmware(_sensor_table().table, drop_at=1)
    specs = walk_metadata(transport, 124)
    assert sorted(specs) == [0, 1, 6]           # the retry recovered key 1
    assert transport.dropped == 1


def test_walk_gives_up_after_repeated_silence() -> None:
    class SilentFirmware(FakeFirmware):
        def send_request(self, dest_id, type_id, payload, timeout=5.0, priority=20):
            self.queries.append((0, 0))
            future: Future = Future()
            future.set_exception(TimeoutError("silence"))
            return future

    transport = SilentFirmware([])
    assert walk_metadata(transport, 124, retries=1, max_failures=3) == {}
    # 3 failures x (1 + 1 retry) = 6 queries before giving up.
    assert len(transport.queries) == 6


def test_extra_probe_keys_are_merged_into_the_walk() -> None:

    """A field the walk cannot reach is still available by direct probe."""
    meta0 = encode_metadata(name="data", type_=FIELD_TYPE_INT, size=2, span=112,
                            module="microphone")
    meta256 = encode_metadata(name="brightness", type_=FIELD_TYPE_UINT, size=1,
                              module="display")

    class ProbeOnlyFirmware(FakeFirmware):
        """Answers the walk with field 0 only; a direct probe finds 256."""

        def send_request(self, dest_id: int, type_id: int, payload: bytes,
                         timeout: float = 5.0, priority: int = 20) -> Future:
            key0 = int.from_bytes(payload[1:3], "little")
            key1 = int.from_bytes(payload[6:8], "little")
            self.queries.append((key0, key1))
            future: Future = Future()
            if key0 == 256:
                future.set_result(encode_response(256, meta256))
            elif key0 == 0 and key1 == 0:
                future.set_result(encode_response(0, meta0))
            else:
                future.set_result(b"")
            return future

    transport = ProbeOnlyFirmware([])
    specs = enumerate_fields(transport, 125, extra_probe_keys=(256,))
    assert sorted(specs) == [0, 256]
    assert specs[256].name == "brightness"
    assert (256, 0) in transport.queries


def test_enum_type_is_decoded_as_signed_metadata() -> None:
    meta = parse_metadata_subdict(encode_metadata(name="last_gesture",
                                                  type_=FIELD_TYPE_ENUM, size=1))
    spec = spec_from_metadata(10, meta)
    assert spec.type == FIELD_TYPE_ENUM
    assert spec.signed is True

