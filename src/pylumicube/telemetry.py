"""Telemetry: SUBSCRIBE_DEFAULT_FIELDS + PUBLISHED_FIELDS (PROTOCOL §4.6).

Nodes do not answer GET_FIELDS requests — reads are *push* based. The
daemon asks a node to publish a default set of its fields:

    SUBSCRIBE_DEFAULT_FIELDS (typeId 200)  payload = [duration_s, bandwidth]

and the node then broadcasts `PUBLISHED_FIELDS` (typeId 20000) snapshots
until the subscription expires, so the request is renewed on a heartbeat
(the Java daemon renews every `cadence` and asks for `cadence * 2`
seconds, so a renewal can be missed without a gap).

Each broadcast is a FlatDictionary-encoded `{key: value}` snapshot. It can
only be decoded against the publishing node's discovered schema (see
`pylumicube.discovery`), because the encoding carries no per-value width.

Failure policy: a frame that cannot be decoded is counted and logged once
per node, and the previously known values are kept. A long-running script
must not die because a firmware revision published a field we have no
metadata for.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor

from . import flat_dictionary
from . import uavcan as u
from .constants import TYPE_PUBLISHED_FIELDS, TYPE_SUBSCRIBE_DEFAULT_FIELDS
from .metadata import FieldSpec
from .transport import Transport

log = logging.getLogger(__name__)

#: Seconds between subscription renewals. Each renewal asks for twice this.
DEFAULT_CADENCE = 15.0
#: Bytes per second a node may spend on telemetry; 255 is the firmware max
#: and what the Java daemon asks for.
DEFAULT_BANDWIDTH = 255
#: Longest subscription the 1-byte duration field can express.
MAX_DURATION = 255


class TelemetryStore:
    """Keeps the latest published field values for each node."""

    def __init__(self, transport: Transport, *,
                 cadence: float = DEFAULT_CADENCE,
                 bandwidth: int = DEFAULT_BANDWIDTH,
                 on_update: Callable[[int, dict[int, int]], None] | None = None,
                 repair_schema: Callable[[int, int], bool] | None = None,
                 max_repairs_per_node: int = 32) -> None:
        self._transport = transport
        self._cadence = cadence
        self._bandwidth = bandwidth
        self._on_update = on_update
        self._repair_schema = repair_schema
        self._max_repairs = max_repairs_per_node

        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._schemas: dict[int, Mapping[int, FieldSpec]] = {}
        self._values: dict[int, dict[int, int]] = {}
        self._last_update: dict[int, float] = {}
        self._subscribed: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._decode_errors: dict[int, int] = {}
        self._repairs: dict[int, int] = {}
        self._repairing: set[tuple[int, int]] = set()
        self._repaired: set[tuple[int, int]] = set()
        self._repair_executor: ThreadPoolExecutor | None = None
        self._frames = 0
        self._registered = False
        self._update_handlers: list[Callable[[int, dict[int, int]], None]] = []

    # ---------------- registration ----------------

    def set_schema(self, node_id: int, specs: Mapping[int, FieldSpec]) -> None:
        """Register a node's field schema (needed to decode its frames)."""
        with self._lock:
            self._schemas[node_id] = dict(specs)

    def add_update_handler(self, handler: Callable[[int, dict[int, int]], None]) -> None:
        """Add a callback for decoded frames (the microphone captures this way).

        Unlike the constructor's `on_update` (one owner, the daemon), any
        number of handlers can be added and removed at runtime.
        """
        with self._lock:
            self._update_handlers.append(handler)

    def remove_update_handler(self, handler: Callable[[int, dict[int, int]], None]) -> None:
        with self._lock:
            try:
                self._update_handlers.remove(handler)
            except ValueError:
                pass

    @property
    def schemas(self) -> dict[int, Mapping[int, FieldSpec]]:
        with self._lock:
            return dict(self._schemas)

    # ---------------- subscription ----------------

    def subscribe(self, node_ids: Iterable[int] | None = None, *,
                  timeout: float = 2.0) -> list[int]:
        """(Re)send SUBSCRIBE_DEFAULT_FIELDS. Returns the nodes subscribed to.

        Errors are logged, not raised: a node that is mid-reboot should not
        take the caller down, and the renewal heartbeat will retry.
        """
        with self._lock:
            targets = sorted(self._schemas if node_ids is None else set(node_ids))
        duration = max(1, min(MAX_DURATION, int(self._cadence * 2)))
        payload = bytes([duration, self._bandwidth & 0xFF])
        ok: list[int] = []
        for node_id in targets:
            try:
                future = self._transport.send_request(
                    dest_id=node_id,
                    type_id=TYPE_SUBSCRIBE_DEFAULT_FIELDS,
                    payload=payload,
                    timeout=timeout,
                )
                future.result(timeout=timeout + 1.0)
                ok.append(node_id)
                log.debug("subscribed to node %d telemetry for %ds", node_id, duration)
            except Exception as exc:  # noqa: BLE001 — best effort, retried on renewal
                log.warning("telemetry subscription to node %d failed: %s", node_id, exc)
        with self._lock:
            self._subscribed.update(ok)
        return ok

    @property
    def subscribed_nodes(self) -> set[int]:
        with self._lock:
            return set(self._subscribed)

    def unsubscribe(self, node_ids: Iterable[int] | None = None, *,
                    timeout: float = 2.0) -> list[int]:
        """Stop renewing a subscription, so it lapses on its own.

        The firmware's subscription carries a duration (twice the cadence),
        so this does not take effect instantly — it stops the *renewal*,
        which is what a caller wanting the link back needs. (Asking the node
        to stop early was tried and is not worth the request: it adds
        traffic to a link that is usually the reason you are unsubscribing.)
        """
        with self._lock:
            targets = (sorted(self._subscribed) if node_ids is None
                       else [int(node) for node in node_ids])
            for node_id in targets:
                self._subscribed.discard(node_id)
        return targets

    def start(self) -> None:
        """Arm the store: decode frames from here on, and renew the subscription.

        Frames are not decoded before this: during `LumiCube.enable_telemetry`
        the schemas are still being walked, and decoding a frame against a
        half-built namespace would call the update callback before the
        modules exist (the daemon's button watcher found that the hard way).
        """
        if not self._registered:
            self._transport.add_broadcast_handler(self._on_broadcast)
            self._registered = True
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._renew_loop, daemon=True,
                                        name="lumicube-telemetry")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            executor = self._repair_executor
            self._repair_executor = None
        if executor is not None:
            executor.shutdown(wait=False)

    def _renew_loop(self) -> None:
        while not self._stop.wait(self._cadence):
            with self._lock:
                targets = sorted(self._subscribed)
            if targets:
                self.subscribe(targets)

    # ---------------- ingress ----------------

    def _on_broadcast(self, transfer: u.Broadcast) -> None:
        if transfer.type_id != TYPE_PUBLISHED_FIELDS or self._stop.is_set():
            return
        node_id = transfer.source_id
        with self._lock:
            specs = self._schemas.get(node_id)
        if specs is None:
            log.debug("PUBLISHED_FIELDS from unknown node %d (%d bytes); no schema",
                      node_id, len(transfer.payload))
            return
        values = self._decode(node_id, transfer.payload, specs)
        if values is None:
            return
        with self._updated:
            self._frames += 1
            self._values.setdefault(node_id, {}).update(values)
            self._last_update[node_id] = time.monotonic()
            self._updated.notify_all()
        if self._on_update is not None:
            try:
                self._on_update(node_id, values)
            except Exception:  # noqa: BLE001 — a bad callback must not kill the link
                log.exception("telemetry on_update callback raised")
        if self._update_handlers:
            with self._lock:
                handlers = list(self._update_handlers)
            for handler in handlers:
                try:
                    handler(node_id, values)
                except Exception:  # noqa: BLE001 — same reasoning
                    log.exception("telemetry update handler raised")

    def _decode(self, node_id: int, payload: bytes,
                specs: Mapping[int, FieldSpec]) -> dict[int, int | float] | None:
        """Decode one frame; an unknown key schedules a schema repair.

        No retry: this runs on the link's I/O thread, so it must not block
        (a repair probe is a request/response round-trip). The repaired
        schema benefits the *next* frame, ~30 ms later.
        """
        try:
            return flat_dictionary.deserialise(payload, specs)
        except flat_dictionary.UndecodablePayload as exc:
            if exc.key is not None:
                self._schedule_repair(node_id, exc.key)
            self._count_decode_error(node_id, exc)
            return None
        except Exception as exc:  # noqa: BLE001 — see module docstring
            self._count_decode_error(node_id, exc)
            return None

    def _schedule_repair(self, node_id: int, key: int) -> bool:
        """Queue a targeted re-probe of `key` for a node's schema.

        The walk can transiently miss a block (the firmware has been seen
        to answer a probe with the *next* block depending on what was
        queried before), so a frame that arrives with an unknown key gets
        one re-probe instead of being dropped forever. Runs off the I/O
        thread; `max_repairs_per_node` bounds the probe traffic.
        """
        if self._repair_schema is None:
            return False
        with self._lock:
            if self._repairs.get(node_id, 0) >= self._max_repairs:
                return False
            if (node_id, key) in self._repairing or (node_id, key) in self._repaired:
                return False
            self._repairing.add((node_id, key))
            self._repairs[node_id] = self._repairs.get(node_id, 0) + 1
            executor = self._repair_executor
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix="lumicube-repair")
                self._repair_executor = executor
        executor.submit(self._run_repair, node_id, key)
        return True

    def _run_repair(self, node_id: int, key: int) -> None:
        try:
            if self._repair_schema is not None and self._repair_schema(node_id, key):
                log.info("telemetry: extended node %d schema with key %d after an "
                         "undecodable frame", node_id, key)
                with self._lock:
                    # Do not probe this key again: if the schema still does
                    # not cover it, probing cannot fix that.
                    self._repaired.add((node_id, key))
        except Exception:  # noqa: BLE001 — never let repair break ingest
            log.exception("telemetry schema repair for node %d key %d raised",
                          node_id, key)
        finally:
            with self._lock:
                self._repairing.discard((node_id, key))


    def _count_decode_error(self, node_id: int, exc: Exception) -> None:
        with self._lock:
            count = self._decode_errors.get(node_id, 0) + 1
            self._decode_errors[node_id] = count
        if count == 1:
            log.warning("cannot decode PUBLISHED_FIELDS from node %d "
                        "(schema mismatch?): %s", node_id, exc)
        else:
            log.debug("PUBLISHED_FIELDS decode failure #%d from node %d",
                      count, node_id)


    # ---------------- reads ----------------

    def get(self, node_id: int, key: int, default: int | None = None) -> int | None:
        with self._lock:
            values = self._values.get(node_id)
            if values is None:
                return default
            return values.get(key, default)

    def snapshot(self, node_id: int) -> dict[int, int]:
        with self._lock:
            return dict(self._values.get(node_id, {}))

    def has_data(self, node_id: int) -> bool:
        with self._lock:
            return bool(self._values.get(node_id))

    def last_update(self, node_id: int) -> float | None:
        with self._lock:
            return self._last_update.get(node_id)

    def wait_for(self, node_id: int, key: int, timeout: float | None = None,
                 default: int | None = None) -> int | None:
        """Block until `key` has been published at least once.

        Returns the value, or `default` on timeout. Raises TimeoutError
        when no default is given.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._updated:
            while True:
                values = self._values.get(node_id)
                if values is not None and key in values:
                    return values[key]
                if deadline is None:
                    self._updated.wait(1.0)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if default is not None:
                        return default
                    raise TimeoutError(
                        f"no telemetry for node {node_id} key {key} within {timeout}s")
                self._updated.wait(remaining)

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "frames": self._frames,
                "nodes": sorted(self._values),
                "subscribed": sorted(self._subscribed),
                "decode_errors": dict(self._decode_errors),
                "schema_repairs": dict(self._repairs),
                "last_update": dict(self._last_update),
            }


