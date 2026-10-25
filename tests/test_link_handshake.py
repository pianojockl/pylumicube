"""Link-layer handshake test using a fake serial port (no hardware).

Simulates the cube side of the wire: respond to PINGs with PONGs, accept
INITIALISE, send our own INITIALISE, accept ACK back. Verifies that
SerialLink.start() returns successfully and that submit() resolves once
the cube ACKs.
"""

from __future__ import annotations

import threading
import time

import pytest

from collections import deque
from concurrent.futures import Future

import pylumicube.framing as framing
import pylumicube.link as link_module
from pylumicube.constants import (
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    FLUSH_COUNT,
    PROTOCOL_VERSION,
    WINDOW_SIZE,
)


class FakeSerial:
    """Bare-minimum substitute for serial.Serial used by SerialLink."""

    def __init__(self) -> None:
        self._rx_buf = bytearray()              # bytes the SUT will read
        self._tx_buf = bytearray()              # bytes the SUT has written
        self._lock = threading.Lock()
        self._closed = False
        # Constants used by SerialLink:
        self.timeout = 0.0025

    # SerialLink-facing API
    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx_buf.clear()

    def reset_output_buffer(self) -> None:
        with self._lock:
            self._tx_buf.clear()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx_buf)

    def read(self, n: int) -> bytes:
        # Mimic pyserial: block until at least one byte or timeout.
        deadline = time.monotonic() + self.timeout
        while True:
            with self._lock:
                if self._closed:
                    return b""
                if self._rx_buf:
                    take = min(n, len(self._rx_buf))
                    chunk = bytes(self._rx_buf[:take])
                    del self._rx_buf[:take]
                    return chunk
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.001)

    def write(self, data: bytes) -> int:
        with self._lock:
            self._tx_buf.extend(data)
        return len(data)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # Test helpers
    def inject(self, data: bytes) -> None:
        with self._lock:
            self._rx_buf.extend(data)

    def drain_tx(self) -> bytes:
        with self._lock:
            chunk = bytes(self._tx_buf)
            self._tx_buf.clear()
        return chunk


class CubeEmulator:
    """Reads from FakeSerial.tx (what SUT writes) and writes back to its rx
    (what SUT reads). Implements the cube-side of the handshake."""

    def __init__(self, fake: FakeSerial) -> None:
        self.fake = fake
        self.stream = framing.FrameStream()
        self.cube_initialised = False
        self.cube_egress_initialised = False
        self.received_messages: list[bytes] = []
        self.our_seq = 0   # next sequence we will send up to the SUT (cube tx)
        self.their_accept = 0  # what we believe their accept is
        self.stop_flag = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_flag = True
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        # First step: drain stream of PINGs from the SUT, send back PONGs.
        # Also kick off our own egress handshake by sending PINGs ourselves.
        cube_countdown = FLUSH_COUNT
        cube_pings_sent = 0
        last_ping = 0.0
        while not self.stop_flag:
            tx = self.fake.drain_tx()
            self.stream.feed(tx)
            while self.stream.has_frame():
                frame = self.stream.pop()
                if frame is None:
                    break
                self._handle_frame(frame, cube_countdown_ref=lambda: cube_countdown)
                if frame.command == CMD_PONG and cube_countdown > 0:
                    cube_countdown -= 1

            # Cube-side egress: we need to PING the SUT until we have received
            # FLUSH_COUNT PONGs, then send INITIALISE.
            now = time.monotonic()
            if not self.cube_egress_initialised and now - last_ping > 0.001:
                if cube_countdown > 0:
                    self.fake.inject(framing.build_ping())
                    cube_pings_sent += 1
                else:
                    self.fake.inject(framing.build_initialise(self.our_seq))
                last_ping = now

            time.sleep(0.0005)

    def _handle_frame(self, frame: framing.DecodedFrame, cube_countdown_ref) -> None:
        cmd = frame.command
        if cmd == CMD_PING:
            self.fake.inject(framing.build_pong())
        elif cmd == CMD_INITIALISE:
            assert frame.body[0] == PROTOCOL_VERSION
            self.their_accept = frame.body[1]
            self.fake.inject(framing.build_initialised())
            self.cube_initialised = True
        elif cmd == CMD_INITIALISED:
            self.cube_egress_initialised = True
        elif cmd == CMD_PONG:
            pass
        elif cmd == CMD_ACKNOWLEDGE:
            pass
        elif cmd == CMD_MESSAGE:
            seq = frame.body[-1]
            payload = frame.body[:-1]
            if seq == self.their_accept:
                self.fake.inject(framing.build_acknowledge(seq))
                self.received_messages.append(payload)
                self.their_accept = (self.their_accept + 1) & 0xFF
            else:
                self.fake.inject(framing.build_acknowledge((self.their_accept - 1) & 0xFF))


@pytest.fixture
def fake_link(monkeypatch):
    fake = FakeSerial()

    class _SerialPatch:
        Serial = lambda *a, **kw: fake
        EIGHTBITS = 8
        PARITY_NONE = "N"
        STOPBITS_ONE = 1

        class SerialException(Exception):
            pass

    # Patch the symbol that link.py imports
    monkeypatch.setattr(link_module, "serial", _SerialPatch)
    return fake


def test_handshake_and_message(fake_link, monkeypatch):
    fake = fake_link
    cube = CubeEmulator(fake)
    try:
        link = link_module.SerialLink("dummy")
        link.start(handshake_timeout=10.0)
        try:
            future = link.submit(b"\x00\x01\x02\x03\x04\xaa\xbb")
            future.result(timeout=5.0)
            # Wait briefly for cube to process and append
            for _ in range(100):
                if cube.received_messages:
                    break
                time.sleep(0.01)
            assert cube.received_messages, "cube did not receive the MESSAGE"
            assert cube.received_messages[0] == b"\x00\x01\x02\x03\x04\xaa\xbb"
        finally:
            link.stop()
    finally:
        cube.stop()


# ----- metrics -------------------------------------------------------------


def test_link_stats_report_queue_depth_and_latency() -> None:
    """A lagging link raises nothing, so the queue depth is the only tell."""
    link = link_module.SerialLink("/dev/null", message_handler=None)
    assert link.stats()["pending"] == 0
    assert link.stats()["latency_ms"] == {"p50": 0.0, "p90": 0.0,
                                          "p99": 0.0, "max": 0.0}

    link._running = True                                    # noqa: SLF001
    for _ in range(7):
        link.submit(b"payload", label="display", droppable=True)
    link._latencies_ms.extend([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    link._latencies_by_label["audio"] = deque([1.0, 2.0])   # noqa: SLF001
    link._latencies_by_label["control"] = deque([60.0])     # noqa: SLF001
    link._completed = 10                                    # noqa: SLF001

    stats = link.stats()
    assert stats["pending"] == 7, "the queue depth is the lag metric"
    assert stats["window_size"] == WINDOW_SIZE
    assert stats["completed"] == 10
    assert stats["latency_ms"]["p50"] == 6.0
    assert stats["latency_ms"]["max"] == 10.0
    assert stats["latency_ms"]["p99"] >= stats["latency_ms"]["p90"]
    assert stats["by_label"] == {"display": 7}
    # A control request's ACK waits for the firmware to service it; a
    # streaming frame's does not, so the classes are reported separately.
    assert stats["latency_by_label"]["audio"]["p50"] == 2.0   # nearest-rank
    assert stats["latency_by_label"]["audio"]["max"] == 2.0
    assert stats["latency_by_label"]["control"]["p50"] == 60.0


def _drain(link) -> list[bytes]:
    """Pop the queue the way the egress driver does."""
    import heapq
    order = []
    with link._pending_lock:                                # noqa: SLF001
        while link._pending:                                # noqa: SLF001
            order.append(heapq.heappop(link._pending).payload)   # noqa: SLF001
    return order


def test_audio_jumps_ahead_of_display_frames() -> None:
    """Speech must not queue behind LEDs — that is the audible stutter."""
    link = link_module.SerialLink("/dev/null", message_handler=None)
    link._running = True                                    # noqa: SLF001
    link.submit(b"display-1", label="display", droppable=True)
    link.submit(b"audio", label="audio", droppable=True)
    link.submit(b"display-2", label="display", droppable=True)
    assert _drain(link) == [b"audio", b"display-1", b"display-2"]


def test_a_full_queue_drops_the_oldest_droppable_frame() -> None:
    """Bounded latency: drop rather than fall further behind."""
    bound = link_module.SerialLink._MAX_PENDING             # noqa: SLF001
    link = link_module.SerialLink("/dev/null", message_handler=None)
    link._running = True                                    # noqa: SLF001
    for index in range(bound):
        link.submit(f"display-{index}".encode(), label="display",
                    droppable=True)
    link.submit(b"display-new", label="display", droppable=True)

    stats = link.stats()
    assert stats["pending"] == bound, "the queue grew past its bound"
    assert stats["dropped"] == 1
    remaining = _drain(link)
    assert b"display-0" not in remaining, "the oldest frame should go, not the newest"
    assert b"display-new" in remaining


def test_a_reliable_frame_is_never_dropped() -> None:
    """Control ops would rather wait than lose their answer."""
    bound = link_module.SerialLink._MAX_PENDING             # noqa: SLF001
    link = link_module.SerialLink("/dev/null", message_handler=None)
    link._running = True                                    # noqa: SLF001
    for index in range(bound + 8):
        link.submit(f"control-{index}".encode(), label="control")
    stats = link.stats()
    assert stats["dropped"] == 0
    assert stats["pending"] == bound + 8


def test_a_deep_ingress_backlog_is_shed() -> None:
    """Upstream drops bytes above its 16 KiB backlog to keep latency bounded."""
    link = link_module.SerialLink("/dev/null", message_handler=None)
    bound = link_module.SerialLink._MAX_BACKLOG               # noqa: SLF001
    fake = FakeSerial()
    link._serial = fake                                       # noqa: SLF001
    link._running = True                                      # noqa: SLF001

    fake.inject(b"\x00" * (bound + 500))
    link._read_available()                                    # noqa: SLF001
    assert link.stats()["skipped_bytes"] == 500

    fake.inject(b"\x00" * (bound - 1))
    link._read_available()                                    # noqa: SLF001
    assert link.stats()["skipped_bytes"] == 500, "shed data it should not have"


def test_egress_is_paced_within_a_byte_budget() -> None:
    """Upstream sends at most `MAX_BATCH_BYTES` per 2.5 ms pass."""
    link = link_module.SerialLink("/dev/null", message_handler=None)
    fake = FakeSerial()
    link._serial = fake                                       # noqa: SLF001
    link._running = True                                      # noqa: SLF001
    link._egress_initialised = True                           # noqa: SLF001

    for _ in range(16):
        link.submit(b"x" * 200, label="display", droppable=True)
    link._service_egress()                                    # noqa: SLF001
    first = len(fake.drain_tx())
    assert 0 < first <= link_module.SerialLink._MAX_BATCH_BYTES, first  # noqa: SLF001

    link._service_egress()                                    # noqa: SLF001
    assert len(fake.drain_tx()) > 0, "the rest never went out"


def test_a_stalled_window_is_re_initialised() -> None:
    """One frame the firmware never accepts must not wedge the link forever.

    Measured after a display flood: `last_ack` froze at 99 with 16 frames in
    the window and 75 queued behind them, and only a daemon restart cleared
    it. The firmware's receive sequence moves only when it *accepts* a frame,
    so the link has to reset itself.
    """
    link = link_module.SerialLink("/dev/null", message_handler=None)
    fake = FakeSerial()
    link._serial = fake                                       # noqa: SLF001
    link._running = True                                      # noqa: SLF001
    link._egress_initialised = True                           # noqa: SLF001

    stuck = link.submit(b"x" * 100)
    link._service_egress()                                    # noqa: SLF001
    assert link.stats()["window"] == 1

    # No ACK arrived for longer than the stall threshold.
    link._head_advanced_ns = (                                 # noqa: SLF001
        time.monotonic_ns() - int(2 * link_module.SerialLink._WINDOW_STALL_S * 1e9))
    link._service_egress()                                    # noqa: SLF001

    stats = link.stats()
    assert stats["resets"] == 1, "the watchdog did not fire"
    assert stats["window"] == 0, "the stuck window was not cleared"
    with pytest.raises(Exception):
        stuck.result(timeout=0)          # the caller hears about it
    assert link._egress_initialised is False, "no re-handshake"  # noqa: SLF001
    assert fake.drain_tx(), "nothing was sent to re-initialise"
