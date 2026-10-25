"""UAVCAN transport sitting on top of the link layer.

Provides:
  - `send_request(dest_id, type_id, payload)` -> Future[bytes] that resolves
    when the matching response is received (or times out).
  - `send_broadcast(type_id, payload, source_id=...)` for fire-and-forget.
  - subscriber callbacks for received broadcasts and requests.

The link layer guarantees frame delivery and ordering; this layer just
manages transferIds and matches requests to responses.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

from . import uavcan as u
from .constants import DEFAULT_PRIORITY, MAX_UAVCAN_PAYLOAD
from .link import SerialLink

log = logging.getLogger(__name__)

#: How often expired requests are swept. Upstream sweeps from a heartbeat
#: every few seconds; a quarter second is cheap and lets a caller fail fast
#: with a clear reason instead of waiting out its own timeout.
_EXPIRY_SWEEP_S = 0.25


BroadcastHandler = Callable[[u.Broadcast], None]
RequestHandler = Callable[[u.ServiceRequest], None]


class Transport:
    def __init__(self, link: SerialLink, source_id: int) -> None:
        self._link = link
        self._source_id = source_id
        self._lock = threading.Lock()
        # transferId pool keyed by (dest_id, type_id) -> next ID (0..31, monotonic mod 32)
        self._service_transfer_ids: dict[tuple[int, int], int] = {}
        # broadcast transferId per (source, typeId) — only relevant when *we* broadcast
        self._broadcast_transfer_ids: dict[tuple[int, int], int] = {}
        # outstanding requests: (source_of_response, type_id, transfer_id)
        # -> (Future, deadline_ns). Upstream keeps the same table with a 5 s
        # expiry and sweeps it from its heartbeat (`AbortExpiredRequestsTask`)
        # so a lost response cannot pin a slot forever.
        self._inflight: dict[tuple[int, int, int], tuple[Future, int]] = {}
        self._sweeper: threading.Thread | None = None
        self._broadcast_handlers: list[BroadcastHandler] = []
        self._request_handlers: list[RequestHandler] = []

        link.set_message_handler(self._on_message)

    # ---------------- public API ----------------

    @property
    def source_id(self) -> int:
        return self._source_id

    def set_source_id(self, source_id: int) -> None:
        self._source_id = source_id

    def add_broadcast_handler(self, handler: BroadcastHandler) -> None:
        self._broadcast_handlers.append(handler)

    def add_request_handler(self, handler: RequestHandler) -> None:
        self._request_handlers.append(handler)

    def send_broadcast(self, type_id: int, payload: bytes,
                       source_id: int | None = None,
                       priority: int = DEFAULT_PRIORITY) -> Future:
        if len(payload) > MAX_UAVCAN_PAYLOAD:
            raise ValueError("payload too large")
        src = source_id if source_id is not None else self._source_id
        if src == 0:
            message_id = u.encode_anonymous_broadcast(type_id, priority)
        else:
            message_id = u.encode_broadcast(src, type_id, priority)
        with self._lock:
            key = (src, type_id)
            transfer_id = self._broadcast_transfer_ids.get(key, 0)
            self._broadcast_transfer_ids[key] = (transfer_id + 1) & 0x1F
        body = u.pack_message(transfer_id, message_id, payload)
        return self._link.submit(body)

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     priority: int = DEFAULT_PRIORITY,
                     timeout: float = 5.0,
                     expect_response: bool = True, *,
                     label: str = "control", droppable: bool = False) -> Future:
        """Send a service request.

        With `expect_response=True` (the default) the returned future
        resolves when the matching ServiceResponse arrives, and times out
        otherwise. Some peripherals — the speaker's audio stream is the one
        we know — service the write but never answer, so streaming callers
        pass `expect_response=False`: the future then resolves when the
        *link* acknowledges the frame, which is the strongest delivery
        guarantee available without a service response.
        """
        if len(payload) > MAX_UAVCAN_PAYLOAD:
            raise ValueError("payload too large")
        message_id = u.encode_request(self._source_id, dest_id, type_id, priority)
        with self._lock:
            key = (dest_id, type_id)
            transfer_id = self._service_transfer_ids.get(key, 0)
            self._service_transfer_ids[key] = (transfer_id + 1) & 0x1F
            if expect_response:
                response_fut: Future = Future()
                inflight_key = (dest_id, type_id, transfer_id)
                self._inflight[inflight_key] = (
                    response_fut, time.monotonic_ns() + int(timeout * 1e9))
        body = u.pack_message(transfer_id, message_id, payload)
        link_fut = self._link.submit(body, label=label, droppable=droppable)

        if not expect_response:
            return link_fut

        def on_link_complete(f: Future) -> None:
            try:
                f.result()
            except Exception as exc:
                with self._lock:
                    self._inflight.pop(inflight_key, None)
                if not response_fut.done():
                    response_fut.set_exception(exc)
        link_fut.add_done_callback(on_link_complete)

        self._start_sweeper()
        return response_fut

    def _start_sweeper(self) -> None:
        """Start the expiry sweep once, on the first request that waits.

        A thread per request (what this used to do) is one thread per control
        operation for no benefit: upstream sweeps a single table from its
        heartbeat, so this does the same. Started lazily so a transport that
        only streams fire-and-forget frames never runs one at all.
        """
        if self._sweeper is not None:
            return
        with self._lock:
            if self._sweeper is not None:
                return
            thread = threading.Thread(target=self._sweep_loop,
                                      name="lumicube-expiry", daemon=True)
            self._sweeper = thread
            thread.start()

    def _sweep_loop(self) -> None:
        while True:
            time.sleep(_EXPIRY_SWEEP_S)
            try:
                self.abort_expired()
            except Exception:  # noqa: BLE001 — a sweep must never die
                log.exception("expiry sweep failed")

    def abort_expired(self, *, now_ns: int | None = None) -> int:
        """Fail requests whose response never came, and free their slots.

        Returns how many were aborted. Upstream's `AbortExpiredRequestsTask`
        is the same idea: a lost response should not hold anything open, and
        the caller should hear about it promptly rather than waiting out its
        own timeout.
        """
        now = time.monotonic_ns() if now_ns is None else now_ns
        expired: list[tuple[tuple[int, int, int], Future]] = []
        with self._lock:
            for key, (future, deadline) in list(self._inflight.items()):
                if now >= deadline:
                    self._inflight.pop(key, None)
                    expired.append((key, future))
        for (dest_id, type_id, transfer_id), future in expired:
            if not future.done():
                future.set_exception(TimeoutError(
                    f"no response for type_id={type_id} dest={dest_id} "
                    f"tid={transfer_id} (aborted as expired)"))
        return len(expired)

    def send_response(self, dest_id: int, type_id: int, transfer_id: int,
                      payload: bytes, priority: int = DEFAULT_PRIORITY) -> Future:
        message_id = u.encode_response(self._source_id, dest_id, type_id, priority)
        body = u.pack_message(transfer_id, message_id, payload)
        return self._link.submit(body)

    # ---------------- ingress ----------------

    def _on_message(self, uavcan_payload: bytes) -> None:
        transfer = u.parse_message(uavcan_payload)
        if transfer is None:
            return
        if isinstance(transfer, u.Broadcast):
            for handler in list(self._broadcast_handlers):
                try:
                    handler(transfer)
                except Exception:
                    log.exception("broadcast handler raised")
        elif isinstance(transfer, u.ServiceRequest):
            for handler in list(self._request_handlers):
                try:
                    handler(transfer)
                except Exception:
                    log.exception("request handler raised")
        elif isinstance(transfer, u.ServiceResponse):
            key = (transfer.source_id, transfer.type_id, transfer.transfer_id)
            with self._lock:
                entry = self._inflight.pop(key, None)
            fut = entry[0] if entry is not None else None
            if fut is not None and not fut.done():
                fut.set_result(transfer.payload)
            else:
                log.debug("unmatched response: %r", transfer)
