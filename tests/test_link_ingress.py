"""Deferred ingress and live retransmit tuning for the serial link.

The link decodes received frames in its own worker thread by default, which
is *between* two egress passes: a telemetry frame's decode time is added to
the audio's send schedule. `set_ingress_mode("deferred")` moves the decode to
a thread of its own — upstream's Inbox Disruptor equivalent. These tests pin
the ordering, the draining and the counters without a serial port.
"""

from __future__ import annotations

import threading

from pylumicube.link import SerialLink


def _link() -> SerialLink:
    # No port is opened until `start()`, so a bare instance is enough here.
    link = SerialLink("/dev/null")
    link._running = True                       # noqa: SLF001 — worker "running"
    return link


def test_frames_are_decoded_inline_by_default() -> None:
    link = _link()
    seen: list[bytes] = []
    link.set_message_handler(seen.append)
    assert link.ingress_mode == "inline"
    link._deliver(b"one")                      # noqa: SLF001
    assert seen == [b"one"]


def test_deferred_ingress_delivers_in_order_on_its_own_thread() -> None:
    link = _link()
    done = threading.Event()
    seen: list[bytes] = []

    def handler(payload: bytes) -> None:
        seen.append(payload)
        if len(seen) == 3:
            done.set()

    link.set_message_handler(handler)
    link.set_ingress_mode("deferred")
    try:
        for payload in (b"one", b"two", b"three"):
            link._deliver(payload)             # noqa: SLF001
        assert done.wait(timeout=2.0)
        assert seen == [b"one", b"two", b"three"]
    finally:
        link.stop()                            # joins the ingress thread


def test_a_raising_handler_does_not_kill_the_ingress_thread() -> None:
    link = _link()
    done = threading.Event()
    seen: list[bytes] = []

    def handler(payload: bytes) -> None:
        seen.append(payload)
        if payload == b"bad":
            raise RuntimeError("this frame is nonsense")
        done.set()

    link.set_message_handler(handler)
    link.set_ingress_mode("deferred")
    try:
        link._deliver(b"bad")                  # noqa: SLF001
        link._deliver(b"good")                 # noqa: SLF001
        assert done.wait(timeout=2.0)
        assert seen == [b"bad", b"good"]
    finally:
        link.stop()


def test_the_ingress_queue_is_bounded() -> None:
    """A decoder that cannot keep up sheds the oldest frame, not all of them."""
    link = _link()
    link.set_message_handler(lambda _payload: None)
    link._HANDLER_BOUND = 4                    # noqa: SLF001
    link.set_ingress_mode("deferred")          # starts the thread
    with link._handler_cv:                     # noqa: SLF001 — hold the drain
        for index in range(10):
            link._deliver(str(index).encode())  # noqa: SLF001
        assert link._handler_dropped == 6       # noqa: SLF001
        assert len(link._handler_queue) == 4    # noqa: SLF001
    link.stop()


def test_retransmit_is_tunable_and_clamped() -> None:
    link = _link()
    assert link.retransmit_after == link._RETRANSMIT_AFTER_S   # noqa: SLF001
    assert link.set_retransmit(0.05) == 0.05
    assert link.retransmit_after == 0.05
    assert link.set_retransmit(0.0) == 0.001    # clamped, not zero
    assert link.set_retransmit(60) == 5.0


def test_stats_report_the_tuning() -> None:
    link = _link()
    link.set_retransmit(0.05)
    link.set_ingress_mode("deferred")
    try:
        stats = link.stats()
        assert stats["retransmit_ms"] == 50.0
        assert stats["ingress"]["mode"] == "deferred"
        assert {"p50", "p99", "max"} <= set(stats["ingress"]["latency_ms"])
    finally:
        link.stop()
