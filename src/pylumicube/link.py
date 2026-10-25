"""Reliable link layer for the LumiCube serial protocol.

Implements the bidirectional handshake (PING/INITIALISE) and the 16-slot
sliding window with 8-bit sequence numbers from PROTOCOL.md §2.3-§2.4.

Threading model: a single I/O worker thread polls the serial port for
incoming bytes (parsing them via FrameStream), services its egress duties
(handshake state machine, retransmits, queued responses), and dispatches
received MESSAGE bodies to a user callback. App threads call `submit()`
to enqueue an outgoing MESSAGE; it returns a `Future` that resolves when
the frame is ACKed (or rejected, e.g. on shutdown).
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

import serial  # pyserial

from . import framing
from .constants import (
    BAUD,
    DROPPED,
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    CMD_UNINITIALISED,
    FLUSH_COUNT,
    PROTOCOL_VERSION,
    WINDOW_SIZE,
)

log = logging.getLogger(__name__)


MessageHandler = Callable[[bytes], None]


@dataclass(order=True)
class _Queued:
    """A submitted frame waiting for a window slot.

    Ordered by `priority` (lower first, as on the wire), then by arrival, so
    audio at priority 0 is pulled before display traffic at 20 — upstream used
    a single FIFO and relied on its bounded service table to keep the queue
    short, which is why a stutter shows up when the queue grows.
    """

    priority: int
    order: int
    payload: bytes = field(compare=False, default=b"")
    future: Future = field(compare=False, default=None)      # type: ignore[assignment]
    submitted_ns: int = field(compare=False, default=0)
    label: str = field(compare=False, default="control")
    droppable: bool = field(compare=False, default=False)


@dataclass
class _Slot:
    payload: bytes = b""              # raw uavcan body (without seq)
    encoded: bytes = b""              # full COBS-framed bytes (including seq+CRC+delimiter)
    last_sent_ns: int = 0
    future: Future | None = None
    submitted_ns: int = 0             # when the caller handed it to `submit()`
    label: str = "control"            # frame class, for the latency breakdown


class LinkBusy(RuntimeError):
    """The link refused a frame because its queue is full."""


class HandshakeError(RuntimeError):
    pass


#: Local queue order by frame class. The *wire* priority stays at upstream's 20
#: for everything (it is inert on a point-to-point link, and the firmware's
#: handling of it is untested), so this is purely about which frame the link
#: picks up next: real-time audio first, then control ops, then the bulk display
#: traffic that can afford to wait and be dropped.
_QUEUE_ORDER = {"audio": 0, "control": 1, "display": 2}


class SerialLink:
    """Reliable link layer over a serial port. PROTOCOL §2."""

    # Period at which the worker loops while egress is active (handshake or window non-empty).
    _ACTIVE_PERIOD_S = 0.0025
    # Idle period when egress window is empty after handshake.
    _IDLE_PERIOD_S = 0.05
    # Retransmit a slot if it has not been ACKed within this many seconds.
    # Upstream's equivalent (`CURSOR_RESET_DELAY_NANOSECONDS`) is 12.5 ms, and
    # a lost frame is on the critical path for whatever is waiting on it.
    _RETRANSMIT_AFTER_S = 0.0125
    # Latency samples kept for the percentiles in `stats()` (a fixed window).
    _LATENCY_SAMPLES = 256
    #: Bytes the egress may put on the wire per pass. Upstream derives the
    #: same figure from the UART's capacity and its 2.5 ms active period
    #: (`ALLOWABLE_BANDWIDTH * ACTIVE_PERIOD_NANOSECONDS / 1e9` = 750 bytes
    #: at 3 Mbit/s), so that a burst cannot overrun the firmware's receive
    #: buffer. Frames still go out as fast as they are acknowledged; this
    #: only stops one pass from emptying a full window in a single burst.
    #: How long the window may sit without its head advancing before the link
    #: re-handshakes. The firmware's receive sequence only moves when it
    #: *accepts* a frame, so one frame it never accepts wedges the link for
    #: good: measured after a display flood, `last_ack` froze at 99 with 16
    #: frames in the window and 75 queued behind them, and only a daemon
    #: restart cleared it. Upstream never meets this because its frames are
    #: always acceptable; this turns a permanent wedge into a hiccup.
    _WINDOW_STALL_S = 1.0
    _MAX_BATCH_BYTES = 750
    #: Ingress backlog above which bytes are *dropped* rather than read.
    #: Upstream's `MAX_BACKLOG` ("Maximum ingress latency in bytes") is the
    #: same 16 KiB, for the same reason: a backlog this deep is stale, and
    #: reading it out only adds latency to everything behind it.
    _MAX_BACKLOG = 16384
    #: Queue bound. Reliable frames are never dropped, but a droppable one is
    #: dropped once the queue is this deep — bounded latency instead of the
    #: ever-growing delay an unbounded queue produces. Upstream refuses the
    #: same way ("Busy, retry later"); this is the same rule, applied locally.
    _MAX_PENDING = 64

    def __init__(self, port: str, baudrate: int = BAUD,
                 message_handler: MessageHandler | None = None) -> None:
        self._port_path = port
        self._baudrate = baudrate
        self._message_handler = message_handler

        self._serial: serial.Serial | None = None
        self._stream = framing.FrameStream()
        self._thread: threading.Thread | None = None
        self._running = False
        self._wire_lock = threading.Lock()  # serialise writes (worker + responder paths)

        # Egress state (mirrors EgressThread.java)
        self._head = 0  # oldest unacked sequence
        self._tail = 0  # next sequence to allocate
        self._egress_initialised = False
        self._countdown = FLUSH_COUNT
        self._slots: list[_Slot] = [_Slot() for _ in range(256)]
        self._pending: list[_Queued] = []
        self._order = 0                    # arrival counter, for stable ordering
        self._dropped = 0

        # Metrics for `stats()`: the queue depth is what turns a busy link into
        # a *lagging* one, and it is invisible without a number. Upstream
        # exposes the same thing (`inboxBacklog()`, `outboxBacklog()`,
        # `collectorBacklog()`).
        self._submitted = 0
        self._completed = 0
        self._skipped_bytes = 0
        self._acks = 0
        self._last_ack = -1
        self._resets = 0
        self._head_advanced_ns = time.monotonic_ns()
        self._latencies_ms: deque[float] = deque(maxlen=self._LATENCY_SAMPLES)
        #: Per-class samples: a control request's ACK waits for the firmware to
        #: service it (50-105 ms), while a streaming frame's does not, so one
        #: aggregate figure would hide exactly the thing worth watching.
        self._latencies_by_label: dict[str, deque[float]] = {}
        self._pending_lock = threading.Lock()

        # Ingress state (mirrors IngressThread.java)
        self._ingress_initialised = False
        self._accept = 0

        # Handshake bookkeeping
        self._handshake_event = threading.Event()  # set when both directions are initialised

    # ------------------------------------------------------------------ public

    def start(self, *, handshake_timeout: float = 5.0) -> None:
        if self._thread is not None:
            return
        self._serial = serial.Serial(
            self._port_path,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self._ACTIVE_PERIOD_S,
            write_timeout=1.0,
        )
        # Drain any stale bytes that arrived before we opened.
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

        self._running = True
        self._thread = threading.Thread(target=self._run, name="lumicube-serial", daemon=True)
        self._thread.start()

        if not self._handshake_event.wait(handshake_timeout):
            self.stop()
            raise HandshakeError(
                f"handshake did not complete within {handshake_timeout}s "
                f"(egress_init={self._egress_initialised}, ingress_init={self._ingress_initialised})"
            )

    def stop(self) -> None:
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        # Cancel any unresolved futures
        with self._pending_lock:
            for entry in self._pending:
                if not entry.future.done():
                    entry.future.set_exception(RuntimeError("link stopped"))
            self._pending.clear()
        for slot in self._slots:
            if slot.future is not None and not slot.future.done():
                slot.future.set_exception(RuntimeError("link stopped"))
            slot.payload = b""
            slot.encoded = b""
            slot.last_sent_ns = 0
            slot.future = None

    def submit(self, uavcan_payload: bytes, *, label: str = "control",
               droppable: bool = False) -> Future:
        """Enqueue a MESSAGE for transmission; the Future resolves on ACK.

        `label` is the frame's class ("audio", "control", "display") and
        decides the order the link picks frames up in, so real-time audio is
        not stuck behind display frames. `droppable` marks frames whose loss
        is survivable: when the queue is over `_MAX_PENDING` the oldest such
        frame is dropped instead of queueing, which keeps latency bounded.
        Reliable frames are never dropped — they would rather wait.
        """
        if not self._running:
            raise RuntimeError("link not started")
        fut: Future = Future()
        with self._pending_lock:
            entry = _Queued(_QUEUE_ORDER.get(label, 1), self._order,
                            uavcan_payload, fut, time.monotonic_ns(),
                            label, droppable)
            self._order += 1
            if droppable and len(self._pending) >= self._MAX_PENDING:
                victim = self._oldest_droppable()
                if victim is not None:
                    self._pending.remove(victim)
                    heapq.heapify(self._pending)
                    victim.future.set_result(DROPPED)
                    self._dropped += 1
            heapq.heappush(self._pending, entry)
            self._submitted += 1
        return fut

    def _oldest_droppable(self) -> "_Queued | None":
        """The droppable frame that has waited longest (caller holds the lock).

        The *oldest*, not the newest: for a display the freshest frame is the
        one worth showing, and for audio the schedule is absolute, so a stale
        chunk is worthless either way.
        """
        victim: _Queued | None = None
        for entry in self._pending:
            if entry.droppable and (victim is None or entry.order < victim.order):
                victim = entry
        return victim

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    # ------------------------------------------------------------------ worker

    def _run(self) -> None:
        try:
            while self._running:
                self._read_available()
                self._service_egress()
                # short sleep when window is empty and initialised
                if self._egress_initialised and self._head == self._tail and not self._has_pending():
                    time.sleep(self._IDLE_PERIOD_S)
                else:
                    # serial.read already blocks up to ACTIVE_PERIOD_S waiting for data
                    pass
        except Exception:
            log.exception("serial worker crashed")
            self._running = False

    def _read_available(self) -> None:
        assert self._serial is not None
        try:
            waiting = self._serial.in_waiting
            if waiting > self._MAX_BACKLOG:
                # Drop the stale head of the backlog, exactly as upstream does
                # ("Skipped over {} bytes to reduce ingress latency"). The
                # frame stream resyncs on the next delimiter, and the sender's
                # window retransmits whatever went with it.
                skip = waiting - self._MAX_BACKLOG
                self._serial.read(skip)
                self._skipped_bytes += skip
                log.warning("skipped %d bytes of stale ingress (%d waiting)",
                            skip, waiting)
            # serial.read with timeout returns up to N bytes within the timeout.
            # Read whatever is in the OS buffer, plus block briefly for new data.
            chunk = self._serial.read(self._serial.in_waiting or 1)
        except serial.SerialException:
            log.exception("read failure")
            self._running = False
            return
        if chunk:
            self._stream.feed(chunk)
        while self._stream.has_frame():
            frame = self._stream.pop()
            if frame is None:
                break
            self._dispatch(frame)

    # ---------- ingress dispatch ----------

    def _dispatch(self, frame: framing.DecodedFrame) -> None:
        cmd = frame.command
        if cmd == CMD_PING:
            self._send_raw(framing.build_pong())
        elif cmd == CMD_PONG:
            if len(frame.body) < 1 or frame.body[0] < 1:
                log.warning("PONG with bad version: %r", frame.body)
                return
            if self._countdown > 0:
                self._countdown -= 1
        elif cmd == CMD_INITIALISE:
            if len(frame.body) >= 2 and frame.body[0] == PROTOCOL_VERSION:
                self._accept = frame.body[1]
                self._ingress_initialised = True
                self._send_raw(framing.build_initialised())
                self._maybe_signal_handshake()
            else:
                self._send_raw(framing.build_uninitialised())
        elif cmd == CMD_INITIALISED:
            self._egress_initialised = True
            self._maybe_signal_handshake()
        elif cmd == CMD_UNINITIALISED:
            log.info("counterparty uninitialised; resetting handshake")
            self._egress_initialised = False
            self._countdown = FLUSH_COUNT
        elif cmd == CMD_ACKNOWLEDGE:
            if len(frame.body) >= 1:
                self._handle_ack(frame.body[0])
        elif cmd == CMD_MESSAGE:
            self._handle_message(frame.body)
        else:
            log.warning("unsupported command 0x%02x", cmd)

    def _maybe_signal_handshake(self) -> None:
        if self._egress_initialised and self._ingress_initialised:
            self._handshake_event.set()

    def _handle_ack(self, ack_seq: int) -> None:
        self._acks += 1
        self._last_ack = ack_seq
        # Walk head over any acknowledged slots in window. See EgressThread.handleFeedback().
        if _in_window(ack_seq, self._head, self._tail):
            new_head = (ack_seq + 1) & 0xFF
            # Fail-fast resolve futures for slots [old_head .. ack_seq]
            cursor = self._head
            while cursor != new_head:
                slot = self._slots[cursor]
                if slot.future is not None and not slot.future.done():
                    slot.future.set_result(None)
                if slot.submitted_ns:
                    elapsed = (time.monotonic_ns() - slot.submitted_ns) / 1e6
                    self._latencies_ms.append(elapsed)
                    series = self._latencies_by_label.setdefault(
                        slot.label, deque(maxlen=self._LATENCY_SAMPLES))
                    series.append(elapsed)
                    self._completed += 1
                slot.payload = b""
                slot.encoded = b""
                slot.last_sent_ns = 0
                slot.future = None
                slot.submitted_ns = 0
                slot.label = "control"
                cursor = (cursor + 1) & 0xFF
            self._head = new_head
            self._head_advanced_ns = time.monotonic_ns()

    def _handle_message(self, body: bytes) -> None:
        # body = [uavcan ...][seq]; we already stripped command and CRC.
        if len(body) < 1:
            return
        seq = body[-1]
        uavcan_payload = body[:-1]
        if not self._ingress_initialised:
            self._send_raw(framing.build_uninitialised())
            return
        if seq == self._accept:
            self._accept = (self._accept + 1) & 0xFF
            self._send_raw(framing.build_acknowledge(seq))
            handler = self._message_handler
            if handler is not None:
                try:
                    handler(uavcan_payload)
                except Exception:
                    log.exception("message handler raised")
        else:
            # Already have this one (or it's out of order). Tell sender what we still need.
            self._send_raw(framing.build_acknowledge((self._accept - 1) & 0xFF))

    # ---------- egress driver ----------

    def _service_egress(self) -> None:
        if not self._egress_initialised:
            self._service_handshake()
            return
        self._check_window_stall()
        if not self._egress_initialised:      # the watchdog just reset us
            self._service_handshake()
            return

        # Pull pending submits into the window
        with self._pending_lock:
            while self._pending and ((self._tail - self._head) & 0xFF) < WINDOW_SIZE:
                entry = heapq.heappop(self._pending)
                seq = self._tail
                slot = self._slots[seq]
                slot.payload = entry.payload
                slot.encoded = framing.build_message(entry.payload, seq)
                slot.last_sent_ns = 0
                slot.future = entry.future
                slot.submitted_ns = entry.submitted_ns
                slot.label = entry.label
                self._tail = (self._tail + 1) & 0xFF

        # Transmit slots that have never been sent or whose retry timer
        # expired, within this pass's byte budget. Whatever is left goes out
        # on the next pass (2.5 ms later), which is upstream's pacing: it
        # keeps a burst from filling the firmware's receive buffer. The first
        # frame always goes, so an oversized frame can never wedge the link.
        now = time.monotonic_ns()
        retry_threshold = self._RETRANSMIT_AFTER_S * 1e9
        budget = self._MAX_BATCH_BYTES
        cursor = self._head
        while cursor != self._tail:
            slot = self._slots[cursor]
            if slot.encoded and (slot.last_sent_ns == 0 or now - slot.last_sent_ns >= retry_threshold):
                # Decide *before* sending, as upstream does (it adds the
                # length and breaks if that would breach the limit), so a pass
                # never overshoots the budget. The head slot is exempt: one
                # frame always goes, so the link cannot wedge on a frame
                # larger than the budget.
                if cursor != self._head and budget < len(slot.encoded):
                    break
                self._send_raw(slot.encoded)
                slot.last_sent_ns = now
                budget -= len(slot.encoded)
            cursor = (cursor + 1) & 0xFF

    def _check_window_stall(self) -> None:
        """Re-handshake if the window has stopped advancing.

        A frame the firmware never accepts blocks its receive sequence, and
        with it the whole link — the queue behind it grows while the clock's
        display stops updating. Nothing on this side can tell *why* it was
        rejected, so the recovery is the protocol's own reset: drop the stuck
        window (those frames are already lost), re-initialise, and let the
        pending queue go out on a clean sequence space.
        """
        if self._head == self._tail:
            self._head_advanced_ns = time.monotonic_ns()
            return
        stalled = (time.monotonic_ns() - self._head_advanced_ns) / 1e9
        if stalled < self._WINDOW_STALL_S:
            return
        log.warning("egress window stalled for %.1fs at sequence %d "
                    "(last ACK %d, %d in flight); re-initialising the link",
                    stalled, self._head, self._last_ack,
                    (self._tail - self._head) & 0xFF)
        self._reset_window()

    def _reset_window(self) -> None:
        """Fail the in-flight frames and start the handshake over."""
        for slot in self._slots:
            if slot.future is not None and not slot.future.done():
                slot.future.set_exception(RuntimeError(
                    "link re-initialised: frame was not accepted"))
            slot.payload = b""
            slot.encoded = b""
            slot.last_sent_ns = 0
            slot.future = None
            slot.submitted_ns = 0
            slot.label = "control"
        self._head = 0
        self._tail = 0
        self._egress_initialised = False
        self._countdown = 0          # straight to INITIALISE, not a PING drain
        self._head_advanced_ns = time.monotonic_ns()
        self._resets += 1

    def _service_handshake(self) -> None:
        if self._countdown > 0:
            self._send_raw(framing.build_ping())
        else:
            self._send_raw(framing.build_initialise(self._head))

    # ---------- writing ----------

    def _send_raw(self, frame: bytes) -> None:
        ser = self._serial
        if ser is None:
            return
        with self._wire_lock:
            try:
                ser.write(frame)
            except serial.SerialException:
                log.exception("write failure")
                self._running = False

    def stats(self) -> dict[str, object]:
        """Queue depth and submit-to-ACK latency, for the daemon's `stats`.

        A lagging link does not raise: frames still arrive, just later, so
        nothing shows up in the logs. `pending` is the number that does show
        it — if it grows, callers are outpacing the link and every frame's
        latency grows with it.
        """
        with self._pending_lock:
            pending = len(self._pending)
        # Which frames the *window* is holding: a window that stops advancing
        # is what a wedged link looks like from outside, and the classes in it
        # say whether it is the stream, the display or a control op. It is
        # always the oldest frames (the window is a FIFO), so this is the
        # queue's head.
        in_window: dict[str, int] = {}
        cursor = self._head
        while cursor != self._tail:
            slot = self._slots[cursor]
            in_window[slot.label] = in_window.get(slot.label, 0) + 1
            cursor = (cursor + 1) & 0xFF
        return {
            "pending": pending,
            "window": (self._tail - self._head) & 0xFF,
            "window_size": WINDOW_SIZE,
            "window_labels": in_window,
            "submitted": self._submitted,
            "completed": self._completed,
            "dropped": self._dropped,
            "acks": self._acks,
            "resets": self._resets,
            "last_ack": self._last_ack,
            "skipped_bytes": self._skipped_bytes,
            "by_label": _count_labels(self._pending),
            "latency_ms": _percentiles(self._latencies_ms),
            "latency_by_label": {label: _percentiles(series)
                                 for label, series in sorted(self._latencies_by_label.items())},
        }

    def _has_pending(self) -> bool:
        with self._pending_lock:
            return bool(self._pending)


def _percentiles(samples: "deque[float] | list[float]") -> dict[str, float]:
    """p50/p90/p99/max of a latency series, rounded for `stats`."""
    ordered = sorted(samples)
    if not ordered:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0}

    def at(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))], 2)

    return {"p50": at(0.5), "p90": at(0.9), "p99": at(0.99),
            "max": round(ordered[-1], 2)}


def _count_labels(entries: "list[_Queued]") -> dict[str, int]:
    """How much of the queue is audio, display, control — the lag breakdown."""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.label] = counts.get(entry.label, 0) + 1
    return counts


def _in_window(seq: int, head: int, tail: int) -> bool:
    """True iff seq is within the half-open window [head, tail) modulo 256."""
    return ((seq - head) & 0xFF) < ((tail - head) & 0xFF)
