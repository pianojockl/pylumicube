"""Transport-level request bookkeeping (no hardware).

The expiry sweep is this implementation's `AbortExpiredRequestsTask`: a lost
response must not hold a slot open, and the caller should hear about it
promptly instead of waiting out its own timeout. It used to be one thread per
request — i.e. one thread per control operation — which is what these tests
pin shut.
"""

from __future__ import annotations

import time
from concurrent.futures import Future

import pytest

from pylumicube import uavcan as u
from pylumicube.constants import DEFAULT_PRIORITY
from pylumicube.transport import Transport


class FakeLink:
    """Records frames, acknowledges immediately, and keeps the handler."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.handler = None

    def set_message_handler(self, handler) -> None:
        self.handler = handler

    def submit(self, body, *, label: str = "control", droppable: bool = False) -> Future:
        self.frames.append(body)
        future: Future = Future()
        future.set_result(None)          # the link ACKs at once
        return future


def _transport() -> tuple[Transport, FakeLink]:
    link = FakeLink()
    return Transport(link, source_id=1), link


def test_abort_expired_fails_only_the_expired_requests() -> None:
    transport, _link = _transport()
    stale = transport.send_request(2, 216, b"x", timeout=0.05)
    fresh = transport.send_request(2, 216, b"y", timeout=30.0)
    time.sleep(0.1)                       # let the stale one pass its deadline

    assert transport.abort_expired() == 1
    with pytest.raises(TimeoutError):
        stale.result(timeout=0)
    assert not fresh.done(), "a live request was aborted"
    assert transport.abort_expired() == 0


def test_a_response_resolves_before_it_expires() -> None:
    transport, link = _transport()
    future = transport.send_request(2, 216, b"x", timeout=30.0)
    body = u.pack_message(0, u.encode_response(2, 1, 216, DEFAULT_PRIORITY), b"ok")
    assert link.handler is not None
    link.handler(body)
    assert future.result(timeout=0) == b"ok"
    assert transport.abort_expired() == 0, "a resolved request was still tracked"


def test_the_sweeper_aborts_without_any_caller_waiting() -> None:
    transport, _link = _transport()
    future = transport.send_request(2, 216, b"x", timeout=0.05)
    with pytest.raises(TimeoutError):
        future.result(timeout=5.0)        # the sweep, not a caller, expires it
    sweeper = transport._sweeper          # noqa: SLF001
    assert sweeper is not None and sweeper.daemon


def test_many_requests_share_one_sweeper_thread() -> None:
    """The regression this replaces: one expiry thread per request."""
    transport, _link = _transport()
    for _ in range(5):
        transport.send_request(2, 216, b"x", timeout=30.0)
    sweeper = transport._sweeper          # noqa: SLF001
    transport.send_request(2, 216, b"y", timeout=30.0)
    assert transport._sweeper is sweeper, "a thread per request again"  # noqa: SLF001


def test_a_fire_and_forget_frame_starts_no_sweeper() -> None:
    """Streaming callers never wait, so they should not spawn a thread."""
    transport, _link = _transport()
    for _ in range(10):
        transport.send_request(2, 216, b"audio", timeout=1.0,
                               expect_response=False, label="audio",
                               droppable=True)
    assert transport._sweeper is None     # noqa: SLF001
