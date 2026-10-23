"""Telemetry store tests: subscription + PUBLISHED_FIELDS decoding.

Offline — `FakeTransport` implements just the two methods the store uses
(`add_broadcast_handler`, `send_request`) and lets the test inject
broadcasts the way the link layer would deliver them.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from pylumicube import flat_dictionary
from pylumicube.constants import (
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_UINT,
    TYPE_NODE_STATUS,
    TYPE_PUBLISHED_FIELDS,
    TYPE_SUBSCRIBE_DEFAULT_FIELDS,
)
from pylumicube.metadata import FieldSpec
from pylumicube.telemetry import (
    DEFAULT_BANDWIDTH,
    MAX_DURATION,
    TelemetryStore,
)
from pylumicube.uavcan import Broadcast


SENSOR_SCHEMA = {
    0: FieldSpec(0, 1, FIELD_TYPE_BOOLEAN, 1, "top_pressed", module="buttons"),
    6: FieldSpec(6, 1, FIELD_TYPE_UINT, 4, "ambient_light", module="light_sensor"),
}


class FakeTransport:
    """Minimal stand-in for `pylumicube.transport.Transport`."""

    def __init__(self) -> None:
        self.requests: list[tuple[int, int, bytes]] = []
        self.handlers: list = []
        self.fail_requests = False

    def add_broadcast_handler(self, handler) -> None:
        self.handlers.append(handler)

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     timeout: float = 5.0, priority: int = 20) -> Future:
        self.requests.append((dest_id, type_id, payload))
        future: Future = Future()
        if self.fail_requests:
            future.set_exception(TimeoutError("no response"))
        else:
            future.set_result(b"")
        return future

    def emit(self, source_id: int, type_id: int, payload: bytes) -> None:
        transfer = Broadcast(source_id=source_id, type_id=type_id, transfer_id=0,
                             priority=20, payload=payload)
        for handler in list(self.handlers):
            handler(transfer)


def _store(cadence: float = 15.0) -> tuple[TelemetryStore, FakeTransport]:
    transport = FakeTransport()
    store = TelemetryStore(transport, cadence=cadence)
    store.set_schema(124, SENSOR_SCHEMA)
    # A store decodes frames only once armed, the way
    # `LumiCube.enable_telemetry` arms it after the schema walk.
    store.start()
    return store, transport


def _frame(values: dict[int, int]) -> bytes:
    return flat_dictionary.serialise(values, SENSOR_SCHEMA)


# ----- subscription -------------------------------------------------------


def test_subscribe_sends_duration_and_bandwidth() -> None:
    store, transport = _store(cadence=15.0)
    assert store.subscribe() == [124]
    assert transport.requests == [
        (124, TYPE_SUBSCRIBE_DEFAULT_FIELDS, bytes([30, DEFAULT_BANDWIDTH]))
    ]
    assert store.subscribed_nodes == {124}


def test_subscribe_duration_is_double_the_cadence() -> None:
    store, transport = _store(cadence=7.5)
    store.subscribe()
    assert transport.requests[0][2] == bytes([15, DEFAULT_BANDWIDTH])


def test_subscribe_duration_is_capped_at_one_byte() -> None:
    store, transport = _store(cadence=1000.0)
    store.subscribe()
    assert transport.requests[0][2][0] == MAX_DURATION


def test_subscribe_only_targets_registered_schemas() -> None:
    store, transport = _store()
    store.set_schema(125, SENSOR_SCHEMA)
    assert store.subscribe() == [124, 125]
    assert [request[0] for request in transport.requests] == [124, 125]


def test_subscribe_failure_is_logged_not_raised() -> None:
    store, transport = _store()
    transport.fail_requests = True
    assert store.subscribe() == []
    assert store.subscribed_nodes == set()


def test_renewal_thread_resubscribes() -> None:
    store, transport = _store(cadence=0.05)
    store.subscribe()
    store.start()
    try:
        deadline = time.monotonic() + 2.0
        while len(transport.requests) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        store.stop()
    assert len(transport.requests) >= 2


# ----- decoding -----------------------------------------------------------


def test_published_fields_are_decoded_into_the_cache() -> None:
    store, transport = _store()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 1712, 0: 1}))
    assert store.get(124, 6) == 1712
    assert store.get(124, 0) == 1
    assert store.snapshot(124) == {0: 1, 6: 1712}
    assert store.has_data(124)
    assert store.last_update(124) is not None


def test_partial_frames_merge_into_the_cache() -> None:
    store, transport = _store()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 100}))
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({0: 1}))
    assert store.snapshot(124) == {0: 1, 6: 100}


def test_frames_from_unknown_nodes_are_ignored() -> None:
    store, transport = _store()
    transport.emit(125, TYPE_PUBLISHED_FIELDS, _frame({6: 999}))
    assert store.snapshot(125) == {}
    assert store.stats()["frames"] == 0


def test_other_broadcast_types_are_ignored() -> None:
    store, transport = _store()
    transport.emit(124, TYPE_NODE_STATUS, b"\x01\x02\x03")
    assert store.stats()["frames"] == 0


def test_undecodable_frame_keeps_previous_values() -> None:
    store, transport = _store()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 1712}))
    # Key 60000 has no metadata: the stream cannot be parsed past it.
    bad = flat_dictionary.serialise({60000: 7}, {
        60000: FieldSpec(60000, 1, FIELD_TYPE_UINT, 4, "mystery"),
    })
    transport.emit(124, TYPE_PUBLISHED_FIELDS, bad)
    assert store.get(124, 6) == 1712
    assert store.stats()["decode_errors"] == {124: 1}


def test_decode_errors_are_counted_per_node() -> None:
    store, transport = _store()
    store.set_schema(125, {0: FieldSpec(0, 1, FIELD_TYPE_UINT, 1, "x")})
    bad = flat_dictionary.serialise({60000: 7}, {
        60000: FieldSpec(60000, 1, FIELD_TYPE_UINT, 4, "mystery"),
    })
    transport.emit(124, TYPE_PUBLISHED_FIELDS, bad)
    transport.emit(124, TYPE_PUBLISHED_FIELDS, bad)
    transport.emit(125, TYPE_PUBLISHED_FIELDS, bad)
    assert store.stats()["decode_errors"] == {124: 2, 125: 1}


def test_on_update_callback_receives_decoded_values() -> None:
    seen: list[tuple[int, dict[int, int]]] = []
    transport = FakeTransport()
    store = TelemetryStore(transport,
                           on_update=lambda node, values: seen.append((node, values)))
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 42}))
    assert seen == [(124, {6: 42})]


def test_frames_before_start_are_not_decoded() -> None:
    """The walk is still running then; callbacks must wait for the namespace."""
    seen: list[tuple[int, dict[int, int]]] = []
    transport = FakeTransport()
    store = TelemetryStore(transport,
                           on_update=lambda node, values: seen.append((node, values)))
    store.set_schema(124, SENSOR_SCHEMA)
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 42}))
    assert seen == []
    assert store.get(124, 6) is None
    store.start()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 43}))
    assert seen == [(124, {6: 43})]


def test_on_update_exception_does_not_break_ingest() -> None:
    def boom(node: int, values: dict[int, int]) -> None:
        raise RuntimeError("callback exploded")

    transport = FakeTransport()
    store = TelemetryStore(transport, on_update=boom)
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 42}))
    assert store.get(124, 6) == 42


# ----- reads --------------------------------------------------------------


def test_get_returns_default_for_missing_values() -> None:
    store, _transport = _store()
    assert store.get(124, 6) is None
    assert store.get(124, 6, -1) == -1
    assert store.get(99, 6, -1) == -1


def test_wait_for_returns_once_published() -> None:
    store, transport = _store()

    def publish() -> None:
        time.sleep(0.05)
        transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 555}))

    thread = threading.Thread(target=publish)
    thread.start()
    try:
        assert store.wait_for(124, 6, timeout=2.0) == 555
    finally:
        thread.join()


def test_wait_for_times_out() -> None:
    store, _transport = _store()
    with pytest.raises(TimeoutError):
        store.wait_for(124, 6, timeout=0.05)
    assert store.wait_for(124, 6, timeout=0.05, default=-1) == -1


def test_stats_reports_nodes_and_frames() -> None:
    store, transport = _store()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _frame({6: 1}))
    stats = store.stats()
    assert stats["frames"] == 1
    assert stats["nodes"] == [124]
    assert stats["decode_errors"] == {}


def test_start_is_idempotent_and_stop_is_safe() -> None:
    store, _transport = _store(cadence=5.0)
    store.start()
    store.start()
    store.stop()
    store.stop()


# ----- schema repair (transiently missed blocks) -------------------------


def _bad_frame() -> bytes:
    """A frame whose key 2 has no metadata in SENSOR_SCHEMA."""
    return flat_dictionary.serialise({2: 1}, {
        2: FieldSpec(2, 1, FIELD_TYPE_BOOLEAN, 1, "middle_pressed", module="buttons"),
    })


def test_unknown_key_schedules_a_schema_repair() -> None:
    transport = FakeTransport()
    repaired: list[tuple[int, int]] = []

    def repair(node_id: int, key: int) -> bool:
        repaired.append((node_id, key))
        # Simulate LumiCube probing the key and extending the schema.
        specs = dict(store.schemas[node_id])
        specs[key] = FieldSpec(key, 1, FIELD_TYPE_BOOLEAN, 1, "middle_pressed",
                               module="buttons")
        store.set_schema(node_id, specs)
        return True

    store = TelemetryStore(transport, repair_schema=repair)
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()

    transport.emit(124, TYPE_PUBLISHED_FIELDS, _bad_frame())
    deadline = time.monotonic() + 2.0
    while not repaired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert repaired == [(124, 2)]

    # The next frame of the same shape now decodes.
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _bad_frame())
    assert store.get(124, 2) == 1


def test_repair_is_bounded_per_node() -> None:
    transport = FakeTransport()
    calls: list[int] = []
    store = TelemetryStore(transport, repair_schema=lambda node, key: calls.append(key) or True,
                           max_repairs_per_node=1)
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _bad_frame())
    deadline = time.monotonic() + 2.0
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    # Budget exhausted: the same (unrepaired) key is not probed again.
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _bad_frame())
    time.sleep(0.05)
    assert calls == [2]


def test_repair_does_not_block_the_ingest_path() -> None:
    """Repairs run on a worker thread: the I/O thread must not stall."""
    transport = FakeTransport()
    done = threading.Event()

    def slow_repair(node_id: int, key: int) -> bool:
        time.sleep(0.5)
        done.set()
        return False

    store = TelemetryStore(transport, repair_schema=slow_repair)
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()
    started = time.monotonic()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _bad_frame())
    elapsed = time.monotonic() - started
    assert elapsed < 0.2, f"ingest blocked for {elapsed:.3f}s"
    assert done.wait(timeout=2.0)


def test_repair_failure_is_counted_not_raised() -> None:
    transport = FakeTransport()

    def broken(node_id: int, key: int) -> bool:
        raise RuntimeError("probe failed")

    store = TelemetryStore(transport, repair_schema=broken)
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()
    transport.emit(124, TYPE_PUBLISHED_FIELDS, _bad_frame())
    time.sleep(0.1)
    assert store.stats()["decode_errors"] == {124: 1}
    assert store.stats()["schema_repairs"] == {124: 1}


