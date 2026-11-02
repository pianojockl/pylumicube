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
    #: Retransmit after this long without an ACK. Upstream's
    #: `CURSOR_RESET_DELAY_NANOSECONDS` is 12.5 ms; a link whose round trip is
    #: longer than that re-sends frames that were merely queued, so the shootout
    #: varies it (`set_retransmit`). With `_fast_retransmit` reacting to the
    #: firmware's own "I still want N" ACKs, this is only a backstop for a link
    #: that says nothing at all, so it is deliberately longer than any healthy
    #: round trip rather than shorter.
    _RETRANSMIT_AFTER_S = 0.25
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
    #: Bound on the deferred-ingress queue (see `set_ingress_mode`). Telemetry
    #: frames are fire-and-forget, so shedding the oldest is right when the
    #: decoder cannot keep up — the value is about to be superseded anyway.
    _HANDLER_BOUND = 256
    #: Consecutive serial read errors tolerated before giving up on the port. A
    #: single spurious wakeup is a tty artefact (see `_read_available`); a run
    #: of them means the device really is gone.
    _MAX_READ_ERRORS = 5
    #: A stale ACK re-sends the blocking head immediately, but not more often
    #: than this (a duplicate of ours also produces a stale ACK, and echoing it
    #: would turn recovery into a storm).
    _FAST_RETRANSMIT_GUARD_NS = 2_000_000

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
        #: Frames sent a second time (or more): the retransmit timer fired before
        #: the ACK came back. With an ACK latency of ~45 ms and a 12.5 ms timer
        #: most audio frames are re-sent three or four times, and that duplicate
        #: traffic is what starves the *fresh* frames — a queue slot only frees
        #: on ACK, but every pass's byte budget is spent on duplicates first.
        self._resends = 0
        #: ACKs that did not advance the window — the firmware saying "I still
        #: want an older frame", i.e. one was lost or rejected. The direct
        #: measure of wire loss, and each one is a hole in the audio until the
        #: retry timer refills it.
        self._ack_stale = 0
        #: The link's own transmit cadence: the gap between consecutive frame
        #: writes, and how many frames went out. If the *audio* is choppy while
        #: these gaps are short, the hiccup is downstream of the link.
        self._tx_gaps_ms: deque[float] = deque(maxlen=self._LATENCY_SAMPLES)
        #: How long each serial *write* took. A write that blocks means the
        #: kernel's tty buffer is full — i.e. the firmware has stopped reading
        #: the UART, which is how it back-pressures a producer that outruns its
        #: audio ring.
        self._write_ms: deque[float] = deque(maxlen=self._LATENCY_SAMPLES)
        self._last_tx_ns = 0
        self._tx_frames = 0
        self._last_ack = -1
        self._resets = 0
        self._head_advanced_ns = time.monotonic_ns()
        self._latencies_ms: deque[float] = deque(maxlen=self._LATENCY_SAMPLES)
        #: Per-class samples: a control request's ACK waits for the firmware to
        #: service it (50-105 ms), while a streaming frame's does not, so one
        #: aggregate figure would hide exactly the thing worth watching.
        self._latencies_by_label: dict[str, deque[float]] = {}
        self._pending_lock = threading.Lock()
        #: Guards the egress window (`_head`/`_tail`/`_slots`). Upstream splits
        #: ingress and egress into threads and hands frames across a disruptor;
        #: here both threads touch the window, so a short lock stands in for
        #: that queue. It is never held across a blocking serial write.
        self._window_lock = threading.Lock()
        #: Wakes the egress thread the moment a frame is submitted, so a lone
        #: frame does not wait out the idle poll (measured at 50-88 ms before).
        self._egress_event = threading.Event()
        self._ingress_thread: threading.Thread | None = None
        self._egress_thread: threading.Thread | None = None

        # Ingress state (mirrors IngressThread.java)
        self._ingress_initialised = False
        self._accept = 0
        #: Consecutive serial read errors (see `_read_available`).
        self._read_errors = 0
        #: Where received MESSAGE bodies are *decoded*. "inline" runs the
        #: handler in this worker thread — right between two egress passes, so
        #: a telemetry frame's decode time is added to the audio's send
        #: schedule. "deferred" hands it to a thread instead, which is what
        #: upstream does (its Inbox Disruptor), and what `stats` measures the
        #: latency of. See `set_ingress_mode`.
        self._ingress_mode = "inline"
        self._handler_queue: deque[tuple[bytes, int]] = deque()
        self._handler_cv = threading.Condition()
        self._handler_thread: threading.Thread | None = None
        self._handler_dropped = 0
        self._handler_latencies_ms: deque[float] = deque(maxlen=self._LATENCY_SAMPLES)
        #: Retransmit after this long without an ACK. Upstream's
        #: `CURSOR_RESET_DELAY_NANOSECONDS` is 12.5 ms; a link whose round
        #: trip is longer than that re-sends frames that were merely queued,
        #: so the shootout varies it (`set_retransmit`).
        self._retransmit_after_s = self._RETRANSMIT_AFTER_S

        # Handshake bookkeeping
        self._handshake_event = threading.Event()  # set when both directions are initialised

    # ------------------------------------------------------------------ public

    def start(self, *, handshake_timeout: float = 5.0) -> None:
        if self._ingress_thread is not None:
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
        # Upstream's shape: `IngressThread` reads, parses and ACKs; a separate
        # `EgressThread` walks the window and writes. One thread doing both is
        # what coupled a telemetry decode (or a blocking write) to the audio's
        # send schedule — measured here as 100 ms holes in the transmit stream.
        self._ingress_thread = threading.Thread(
            target=self._ingress_loop, name="lumicube-ingress-serial", daemon=True)
        self._egress_thread = threading.Thread(
            target=self._egress_loop, name="lumicube-egress", daemon=True)
        self._ingress_thread.start()
        self._egress_thread.start()

        if not self._handshake_event.wait(handshake_timeout):
            self.stop()
            raise HandshakeError(
                f"handshake did not complete within {handshake_timeout}s "
                f"(egress_init={self._egress_initialised}, ingress_init={self._ingress_initialised})"
            )

    def stop(self) -> None:
        self._running = False
        self._egress_event.set()               # wake the egress loop
        with self._handler_cv:
            self._handler_cv.notify_all()      # let the ingress thread exit
        for thread in (self._ingress_thread, self._egress_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        self._ingress_thread = None
        self._egress_thread = None
        handler_thread = self._handler_thread
        if handler_thread is not None:
            handler_thread.join(timeout=2.0)
            self._handler_thread = None
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
        self._egress_event.set()      # wake the egress thread now, not in 50 ms
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

    # ------------------------------------------------------------------ tuning

    @property
    def ingress_mode(self) -> str:
        """`"inline"` or `"deferred"` — where received frames are decoded."""
        return self._ingress_mode

    def set_ingress_mode(self, mode: str) -> str:
        """Decode received frames in the worker thread, or in their own one.

        Upstream hands each decoded frame to an Inbox Disruptor thread; this
        worker, by contrast, decodes telemetry inline, i.e. between two
        egress passes. With the sensor board publishing ~30 frames/s that is
        a few milliseconds per second of egress time, and any talker on the
        link (the light sensor, the buttons) shows up as jitter in the audio's
        send schedule — which matters because the firmware's buffer is ~47 ms.

        Frames are still ACKed immediately; only the decode moves, so link
        semantics do not change. Order is preserved (one FIFO, one thread).
        """
        if mode not in ("inline", "deferred"):
            raise ValueError(f"ingress mode must be 'inline' or 'deferred', "
                             f"not {mode!r}")
        with self._handler_cv:
            self._ingress_mode = mode
            if mode == "deferred" and self._handler_thread is None:
                thread = threading.Thread(target=self._handler_loop,
                                          name="lumicube-ingress", daemon=True)
                self._handler_thread = thread
                thread.start()
            self._handler_cv.notify_all()
        return mode

    @property
    def retransmit_after(self) -> float:
        """Seconds a sent frame waits for its ACK before being re-sent."""
        return self._retransmit_after_s

    def set_retransmit(self, seconds: float) -> float:
        """Change the retransmit threshold (seconds, clamped to 1 ms..5 s)."""
        self._retransmit_after_s = max(0.001, min(5.0, float(seconds)))
        return self._retransmit_after_s

    # ------------------------------------------------------- ingress decoding

    def _deliver(self, uavcan_payload: bytes) -> None:
        """Hand a received MESSAGE body to the handler, inline or deferred."""
        handler = self._message_handler
        if handler is None:
            return
        if self._ingress_mode != "deferred":
            self._call_handler(handler, uavcan_payload)
            return
        with self._handler_cv:
            while len(self._handler_queue) >= self._HANDLER_BOUND:
                self._handler_queue.popleft()
                self._handler_dropped += 1
            self._handler_queue.append((uavcan_payload, time.monotonic_ns()))
            self._handler_cv.notify()

    @staticmethod
    def _call_handler(handler: MessageHandler, uavcan_payload: bytes) -> None:
        try:
            handler(uavcan_payload)
        except Exception:  # noqa: BLE001 — one bad frame must not kill the link
            log.exception("message handler raised")

    def _handler_loop(self) -> None:
        """The deferred-ingress worker (drains, then exits once stopped)."""
        while True:
            with self._handler_cv:
                while not self._handler_queue and self._running:
                    self._handler_cv.wait(timeout=0.2)
                if not self._handler_queue:
                    if not self._running:
                        return
                    continue
                payload, enqueued_ns = self._handler_queue.popleft()
            handler = self._message_handler
            if handler is not None:
                self._call_handler(handler, payload)
            self._handler_latencies_ms.append(
                (time.monotonic_ns() - enqueued_ns) / 1e6)

    # ------------------------------------------------------------------ worker

    def _ingress_loop(self) -> None:
        """Read, parse, ACK and deliver — and nothing else.

        Upstream's `IngressThread`. Keeping egress out of this thread is the
        point: a telemetry burst or a slow `read()` must not delay a frame that
        is due on the wire, which is what the single-threaded version did.
        """
        try:
            while self._running:
                self._read_available()
        except Exception:  # noqa: BLE001 — a dead ingress thread must say so
            log.exception("serial ingress crashed")
            self._running = False

    def _egress_loop(self) -> None:
        """Walk the window and write frames, every 2.5 ms (upstream's cadence).

        Woken immediately by `submit()`, so a lone frame never waits out an
        idle poll — measured at 54-88 ms before this thread existed.
        """
        try:
            while self._running:
                busy = (not self._egress_initialised
                        or self._head != self._tail
                        or self._has_pending())
                self._egress_event.wait(
                    timeout=self._ACTIVE_PERIOD_S if busy else self._IDLE_PERIOD_S)
                self._egress_event.clear()
                if not self._running:
                    break
                self._service_egress()
        except Exception:  # noqa: BLE001 — a dead egress thread must say so
            log.exception("serial egress crashed")
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
        except serial.SerialException as error:
            # A spurious wakeup — `select()` says readable and `read()` returns
            # nothing — is a tty/PL011 artefact that was actually observed on
            # this cube while another process was opening the port, and one of
            # them must not kill a link that otherwise owns it. Upstream catches
            # the IOException and pauses (`Pause.onError()`); this does the
            # same, but gives up if the port really is gone.
            self._read_errors += 1
            if self._read_errors >= self._MAX_READ_ERRORS:
                log.exception("read failure (%d in a row); giving up on the link",
                              self._read_errors)
                self._running = False
                return
            log.warning("serial read failed (%d in a row): %s",
                        self._read_errors, error)
            time.sleep(0.05)
            return
        self._read_errors = 0
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
        if not _in_window(ack_seq, self._head, self._tail):
            # The firmware re-ACKed a sequence we have already left behind: it
            # is telling us it still expects an *older* frame, i.e. one was lost
            # on the wire (this line does log CRC errors — the Java daemon's log
            # showed several a second) or rejected by the firmware. Each of
            # those is a hole in the audio until we re-send, so it is worth
            # counting rather than inferring from a stalled window.
            self._ack_stale += 1
            self._fast_retransmit()
            return
        with self._window_lock:
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

    def _fast_retransmit(self) -> None:
        """Re-send the blocking head the moment the firmware says it is missing.

        A stale ACK means the firmware received something *after* our head, so
        the head itself did not arrive (a CRC error, or a frame it refused) and
        everything behind it is stuck. Waiting for the retry timer is what made
        this expensive both ways, measured on the cube:

        * a timer shorter than the ACK latency (12.5 ms against a p50 of 59 ms)
          re-sends the head four or five times, and that duplicate traffic is
          itself what slows the ACKs down — a congestion loop;
        * a timer longer than the latency (150-300 ms) turns every rejection
          into a hole the firmware's 63 ms ring cannot cover.

        Reacting to the ACK instead costs nothing when nothing is lost, and
        recovers in one ACK round trip when something is. The guard stops an
        echo: a duplicate of ours also produces a stale ACK, which must not
        trigger another duplicate.
        """
        slot = self._slots[self._head]
        if not slot.encoded or not slot.last_sent_ns:
            return
        now = time.monotonic_ns()
        if now - slot.last_sent_ns < self._FAST_RETRANSMIT_GUARD_NS:
            return
        frame = slot.encoded
        slot.last_sent_ns = now
        self._resends += 1
        # Written outside any lock: this runs in the *ingress* thread, and a
        # blocking write here must not hold up the window.
        self._send_raw(frame)

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
            # ACK first: the link layer's promise is delivery, and the decode
            # (which may be deferred to another thread) is the caller's problem.
            self._send_raw(framing.build_acknowledge(seq))
            self._deliver(uavcan_payload)
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

        # Pull pending submits into the window, and collect the frames that are
        # due — all under the window lock, but the *writes* happen outside it so
        # a blocking `write()` can never stall the ingress thread's ACK path.
        outgoing: list[bytes] = []
        with self._window_lock:
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

            now = time.monotonic_ns()
            retry_threshold = self._retransmit_after_s * 1e9
            budget = self._MAX_BATCH_BYTES
            cursor = self._head
            while cursor != self._tail:
                slot = self._slots[cursor]
                due = slot.encoded and (slot.last_sent_ns == 0
                                        or now - slot.last_sent_ns >= retry_threshold)
                # ACKs are cumulative and in-order: the firmware only advances
                # its receive sequence past the frames it has *accepted*, so a
                # frame behind a still-unacknowledged head can never be
                # acknowledged first. Re-sending it therefore buys nothing and
                # spends this pass's byte budget — measured at 15 % of all
                # frames duplicated when every slot had its own retry timer.
                # Only the head is retried (and `_fast_retransmit` reacts to the
                # firmware's own "I still want N" instead of waiting).
                if cursor != self._head and slot.last_sent_ns:
                    due = False
                if due:
                    # Decide *before* sending, as upstream does (it adds the
                    # length and breaks if that would breach the limit), so a
                    # pass never overshoots the budget. The head slot is exempt:
                    # one frame always goes, so the link cannot wedge on a frame
                    # larger than the budget.
                    if cursor != self._head and budget < len(slot.encoded):
                        break
                    if slot.last_sent_ns:
                        self._resends += 1    # the retry timer fired: a duplicate
                    slot.last_sent_ns = now
                    budget -= len(slot.encoded)
                    outgoing.append(slot.encoded)
                cursor = (cursor + 1) & 0xFF
        for frame in outgoing:
            self._send_raw(frame)

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
                began = time.monotonic_ns()
                ser.write(frame)
                stamp = time.monotonic_ns()
                self._write_ms.append((stamp - began) / 1e6)
                if self._last_tx_ns:
                    self._tx_gaps_ms.append((stamp - self._last_tx_ns) / 1e6)
                self._last_tx_ns = stamp
                self._tx_frames += 1
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
        with self._window_lock:
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
            "resends": self._resends,
            "dropped": self._dropped,
            "acks": self._acks,
            "ack_stale": self._ack_stale,
            "tx_frames": self._tx_frames,
            "tx_gap_ms": _percentiles(self._tx_gaps_ms),
            "write_ms": _percentiles(self._write_ms),
            "resets": self._resets,
            "last_ack": self._last_ack,
            "skipped_bytes": self._skipped_bytes,
            "retransmit_ms": round(self._retransmit_after_s * 1000.0, 2),
            "ingress": {
                "mode": self._ingress_mode,
                "backlog": len(self._handler_queue),
                "dropped": self._handler_dropped,
                "latency_ms": _percentiles(self._handler_latencies_ms),
            },
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
