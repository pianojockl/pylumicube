"""Retransmission policy of the serial link (no hardware).

ACKs are cumulative and in-order: the firmware only advances its receive
sequence past frames it has *accepted*, so a frame sitting behind an
unacknowledged head can never be acknowledged first. Re-sending every slot
whose timer expired therefore costs byte budget and buys nothing — measured on
the cube at 15 % of all audio frames duplicated. Only the head is retried.
"""

from __future__ import annotations

import time

import pylumicube.link as link_module
from tests.test_link_handshake import FakeSerial, fake_link  # noqa: F401 — fixture

FRAME_BYTES = 40          # a small payload; three of them fit one pass's budget


def _idle_link(fake: FakeSerial) -> link_module.SerialLink:
    """A link that is 'initialised' but whose worker thread is not running."""
    link = link_module.SerialLink("dummy")
    link._serial = fake                       # noqa: SLF001 — bypass start()
    link._running = True                      # noqa: SLF001
    link._egress_initialised = True           # noqa: SLF001
    return link


def _fill_window(link: link_module.SerialLink, count: int) -> int:
    for index in range(count):
        link.submit(bytes([index]) * FRAME_BYTES)
    link._service_egress()                    # noqa: SLF001
    return (link._tail - link._head) & 0xFF   # noqa: SLF001


def _frames(written: bytes) -> int:
    """How many COBS frames (0x00-delimited) are in `written`."""
    return written.count(b"\x00")


def test_only_the_head_is_retransmitted(fake_link) -> None:   # noqa: F811
    """Three slots go out; when all three timers expire, one frame is re-sent."""
    fake = fake_link
    link = _idle_link(fake)
    assert _fill_window(link, 3) == 3
    assert _frames(fake.drain_tx()) == 3

    # Nothing is due yet, so a second pass writes nothing at all.
    link._service_egress()                    # noqa: SLF001
    assert fake.drain_tx() == b""

    # Age every slot past the retry threshold.
    stale = time.monotonic_ns() - int(link.retransmit_after * 1e9) - 1
    for cursor in range(link._head, link._tail):      # noqa: SLF001
        link._slots[cursor].last_sent_ns = stale      # noqa: SLF001

    link._service_egress()                    # noqa: SLF001
    assert _frames(fake.drain_tx()) == 1, "only the head should be re-sent"
    assert link.stats()["resends"] == 1


def test_a_stale_ack_retransmits_the_head_immediately(fake_link) -> None:  # noqa: F811
    """The firmware says "I want the one before" — re-send the head at once.

    This is the recovery path that replaces a retry timer: no duplicates at all
    while nothing is lost, and one round trip when something is.
    """
    fake = fake_link
    link = _idle_link(fake)
    assert _fill_window(link, 3) == 3
    fake.drain_tx()
    stale_seq = (link._head - 1) & 0xFF                # noqa: SLF001
    link._slots[link._head].last_sent_ns = (           # noqa: SLF001
        time.monotonic_ns() - 10_000_000)
    link._handle_ack(stale_seq)                        # noqa: SLF001
    assert _frames(fake.drain_tx()) == 1
    assert link.stats()["ack_stale"] == 1
    # ...and an immediate second one is ignored: echoing would storm.
    link._handle_ack(stale_seq)                        # noqa: SLF001
    assert fake.drain_tx() == b""


def test_fast_retransmit_never_touches_a_frame_behind_the_head(fake_link) -> None:  # noqa: F811
    fake = fake_link
    link = _idle_link(fake)
    assert _fill_window(link, 3) == 3
    fake.drain_tx()
    for cursor in range(link._head, link._tail):       # noqa: SLF001
        link._slots[cursor].last_sent_ns = (           # noqa: SLF001
            time.monotonic_ns() - 10_000_000)
    link._handle_ack((link._head - 1) & 0xFF)          # noqa: SLF001
    assert _frames(fake.drain_tx()) == 1

