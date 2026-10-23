"""High-level orchestration: open the link, run the handshake, allocate
node IDs, and surface a `Display` (and future) module accessor.

This is the entry point most users want:

    with LumiCube('/dev/ttyAMA0') as cube:
        cube.display.set_leds({0: 0xFF0000})
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterable

from . import discovery as _discovery
from . import uavcan as u
from .allocator import Allocator
from .constants import (
    BAUD,
    DAEMON_NODE_ID,
    SERIAL_DEVICE,
    TYPE_ALLOCATION,
    TYPE_GET_NODE_INFO,
    TYPE_GET_PREFERRED_NAME,
)
from .display import Display
from .link import SerialLink
from .metadata import FieldSpec, display_specs
from .modules import CUBE_NODE_NAMES, FieldModule, build_namespace
from .telemetry import DEFAULT_CADENCE, TelemetryStore
from .transport import Transport

log = logging.getLogger(__name__)


class LumiCube:
    """Owns the serial link, transport, and allocator. Exposes module helpers."""

    def __init__(self, port: str = SERIAL_DEVICE, *, baudrate: int = BAUD,
                 source_id: int = DAEMON_NODE_ID,
                 handshake_timeout: float = 5.0,
                 discovery_timeout: float = 3.0,
                 telemetry: bool = False,
                 telemetry_cadence: float = DEFAULT_CADENCE,
                 schema_timeout: float = 1.5,
                 schema_budget: float = 15.0,
                 schema_nodes: Iterable[int] | None = None,
                 schema_node_names: Iterable[str] | None = None) -> None:
        self._port = port
        self._baudrate = baudrate
        self._source_id = source_id
        self._handshake_timeout = handshake_timeout
        self._discovery_timeout = discovery_timeout
        self._telemetry_enabled = telemetry
        self._telemetry_cadence = telemetry_cadence
        self._schema_timeout = schema_timeout
        self._schema_budget = schema_budget
        self._schema_nodes = list(schema_nodes) if schema_nodes is not None else None
        self._schema_node_names = (list(schema_node_names)
                                   if schema_node_names is not None else None)

        self._link: SerialLink | None = None
        self._transport: Transport | None = None
        self._allocator: Allocator | None = None
        self._discovered_event = threading.Event()
        self._allocated: dict[int, uuid.UUID | None] = {}  # node_id -> uuid (None = passively observed)
        self._names: dict[int, str] = {}              # node_id -> preferred name
        self._display_node_id: int | None = None
        # Cached Display instance — created on first `.display` access.
        # Held for the lifetime of the LumiCube so its async worker
        # thread and pending-write state persist across calls.
        self._display_instance: Display | None = None
        # Telemetry: per-node field schemas (from ENUMERATE_FIELDS), the
        # value cache fed by PUBLISHED_FIELDS broadcasts, and the module
        # namespace built from them.
        self._schemas: dict[int, dict[int, FieldSpec]] = {}
        self._telemetry: TelemetryStore | None = None
        self._namespace: dict[str, FieldModule] | None = None


    # ---------------- lifecycle ----------------

    def __enter__(self) -> "LumiCube":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> None:
        log.info("opening %s at %d baud", self._port, self._baudrate)
        self._link = SerialLink(self._port, self._baudrate)
        self._link.start(handshake_timeout=self._handshake_timeout)
        self._transport = Transport(self._link, source_id=self._source_id)
        self._allocator = Allocator(self._transport, on_allocated=self._on_allocated)
        # The daemon claims its own ID (matches Java behaviour).
        self._allocator.reserve(self._source_id)
        # Also passively learn IDs from any non-anonymous broadcast traffic
        # — useful when the cube boards retained their IDs from a prior daemon
        # session and aren't sending fresh allocation requests.
        self._transport.add_broadcast_handler(self._on_broadcast)
        self._wait_for_nodes()
        self._discover_names()
        self._display_node_id = self._pick_display_node()
        if self._telemetry_enabled:
            self.enable_telemetry()

    def stop(self) -> None:
        # Stop the telemetry heartbeat before closing the wire, so the
        # renewal thread can't race with link teardown.
        if self._telemetry is not None:
            try:
                self._telemetry.stop()
            except Exception:  # noqa: BLE001
                log.exception("error stopping telemetry")
            self._telemetry = None
        # Drain pending async display writes before closing the wire so
        # the last frame the script asked for actually lands.
        if self._display_instance is not None:
            try:
                self._display_instance.close()
            except Exception:  # noqa: BLE001
                log.exception("error closing display")
            self._display_instance = None
        if self._link is not None:
            self._link.stop()
            self._link = None
        self._transport = None
        self._allocator = None

    # ---------------- public accessors ----------------

    @property
    def link(self) -> SerialLink:
        if self._link is None:
            raise RuntimeError("link not started")
        return self._link

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            raise RuntimeError("transport not started")
        return self._transport

    @property
    def allocator(self) -> Allocator:
        if self._allocator is None:
            raise RuntimeError("allocator not started")
        return self._allocator

    @property
    def display(self) -> Display:
        if self._display_node_id is None:
            raise RuntimeError(
                "display module not located; "
                f"allocated nodes: {self._allocated}, names: {self._names}"
            )
        assert self._transport is not None
        if self._display_instance is None:
            self._display_instance = Display(self._transport, self._display_node_id)
        return self._display_instance

    # ---------------- telemetry ----------------

    def enable_telemetry(self, node_ids: Iterable[int] | None = None,
                         node_names: Iterable[str] | None = None, *,
                         subscribe: bool = True,
                         budget: float | None = None) -> TelemetryStore:
        """Discover field schemas, subscribe to telemetry, build the namespace.

        Idempotent — repeated calls return the existing store. `node_ids` /
        `node_names` restrict which nodes get walked and subscribed to;
        both default to the constructor's `schema_nodes` /
        `schema_node_names`, and to every discovered node when those are
        unset too. `budget` overrides `schema_budget` for this call.
        """
        if self._telemetry is not None:
            return self._telemetry
        store = TelemetryStore(self.transport, cadence=self._telemetry_cadence,
                               repair_schema=self._repair_schema)
        targets = self._resolve_nodes(node_ids, node_names)
        if not targets:
            log.warning("enable_telemetry: no nodes discovered to subscribe to")
        for node_id in targets:
            specs = self.discover_schema(node_id, budget=budget)
            if specs:
                store.set_schema(node_id, specs)
        self._telemetry = store
        self._namespace = None
        if subscribe:
            store.subscribe()
        store.start()
        return store

    def _resolve_nodes(self, node_ids: Iterable[int] | None,
                       node_names: Iterable[str] | None) -> list[int]:
        if node_ids is None and node_names is None:
            node_ids, node_names = self._schema_nodes, self._schema_node_names
        if node_ids is not None:
            return sorted({int(node_id) for node_id in node_ids})
        if node_names is not None:
            wanted = [name.lower() for name in node_names]
            return sorted(node_id for node_id, name in self._names.items()
                          if any(w in name.lower() for w in wanted))
        return sorted(self._allocated)

    def discover_schema(self, node_id: int, *,
                        budget: float | None = None) -> dict[int, FieldSpec]:
        """Walk node `node_id`'s ENUMERATE_FIELDS schema (cached per node).

        For the `cube` base board the hardcoded display schema is merged
        in, because the firmware's "next field at floor >= k" hints step
        over the display block's non-LED fields (see PROTOCOL.md §5.1).

        `budget` caps the wall-clock time spent walking (default:
        `schema_budget`). A truncated walk is safe: telemetry frames that
        reference a block the walk missed trigger a targeted re-probe (see
        `TelemetryStore`), so a small budget mostly trades a few dropped
        frames for a much faster start.
        """
        cached = self._schemas.get(node_id)
        if cached is not None:
            return cached
        name = self._names.get(node_id, "")
        is_cube = name.lower() in CUBE_NODE_NAMES
        started = time.monotonic()
        try:
            specs = _discovery.enumerate_fields(
                self.transport, node_id,
                extra_probe_keys=_discovery.CUBE_WELL_KNOWN_KEYS if is_cube else (),
                timeout=self._schema_timeout,
                max_seconds=self._schema_budget if budget is None else budget,
            )
        except Exception:  # noqa: BLE001 — a partial schema is still useful
            log.exception("schema discovery failed for node %d", node_id)
            specs = {}
        if is_cube:
            merged = display_specs()
            merged.update(specs)
            specs = merged
        self._schemas[node_id] = specs
        log.info("node %d (%s): %d field block(s) discovered in %.1fs",
                 node_id, name or "?", len(specs), time.monotonic() - started)
        return specs

    @property
    def schemas(self) -> dict[int, dict[int, FieldSpec]]:
        """Discovered per-node schemas: ``{node_id: {floor: FieldSpec}}``."""
        return {node_id: dict(specs) for node_id, specs in self._schemas.items()}

    def _repair_schema(self, node_id: int, key: int) -> bool:
        """Probe one key and add it to a node's schema (telemetry callback).

        Called when a `PUBLISHED_FIELDS` frame references a key the walk
        did not discover — the firmware has been observed to answer a walk
        probe with the *next* block, so a block can be transiently missed.
        """
        specs = self._schemas.get(node_id)
        if specs is None or self._transport is None:
            return False
        result = _discovery.probe_key(self._transport, node_id, key,
                                      timeout=self._schema_timeout)
        if result is None:
            return False
        meta, rel = result
        floor = _discovery._resolve_floor(self._transport, node_id, key, rel,
                                         _discovery._signature(meta),
                                         self._schema_timeout)
        spec = _discovery.spec_from_metadata(floor, meta)
        if not spec.covers(key):
            # The firmware's gap arithmetic is not always consistent with
            # the keys it actually publishes (observed for the system
            # blocks at 60000+/2183+), so trust the probe: it answered for
            # exactly this key.
            spec = _discovery.spec_from_metadata(key, meta)
        specs[spec.floor] = spec
        store = self._telemetry
        if store is not None:
            store.set_schema(node_id, specs)
        return True

    @property
    def telemetry(self) -> TelemetryStore | None:
        return self._telemetry

    @property
    def node_names(self) -> dict[int, str]:
        return dict(self._names)

    def node_id_for_name(self, name: str) -> int | None:
        """Resolve a preferred name (case-insensitive substring) to a node id."""
        needle = name.lower()
        for node_id, node_name in sorted(self._names.items()):
            if needle in node_name.lower():
                return node_id
        return None

    @property
    def modules(self) -> dict[str, FieldModule]:
        """Module namespace built from the discovered schemas.

        Requires telemetry: the module objects read the telemetry cache.
        """
        if self._namespace is None:
            if self._telemetry is None:
                raise RuntimeError(
                    "telemetry not enabled — construct with LumiCube(telemetry=True) "
                    "or call enable_telemetry()")
            self._namespace = build_namespace(self._schemas, self._names, self._telemetry)
        return self._namespace

    def module(self, name: str) -> FieldModule:
        try:
            return self.modules[name]
        except KeyError:
            raise KeyError(
                f"no module {name!r}; known modules: {sorted(self.modules)}") from None

    @property
    def light_sensor(self) -> FieldModule | None:
        """The `light_sensor` module (ambient_light, red/green/blue, gestures)."""
        return self.modules.get("light_sensor") if self._telemetry is not None else None

    @property
    def buttons(self) -> FieldModule | None:
        """The `buttons` module (top/middle/bottom pressed + counters)."""
        return self.modules.get("buttons") if self._telemetry is not None else None

    # ---------------- discovery ----------------

    def _on_allocated(self, node_id: int, identity: uuid.UUID) -> None:
        self._allocated[node_id] = identity
        self._discovered_event.set()

    def _on_broadcast(self, transfer: u.Broadcast) -> None:
        """Record any non-anonymous source ID we see — these are nodes that
        already have IDs (e.g. allocated by a previous daemon session)."""
        if transfer.type_id == TYPE_ALLOCATION:
            return  # handled by Allocator
        if transfer.source_id == 0 or transfer.source_id == self._source_id:
            return
        if transfer.source_id not in self._allocated:
            log.info("passively discovered node %d (typeId=%d)",
                     transfer.source_id, transfer.type_id)
            self._allocated[transfer.source_id] = None
            self._discovered_event.set()

    def _wait_for_nodes(self) -> None:
        """Wait until at least one node is discovered (via allocation OR
        passive broadcast observation), then settle for any stragglers."""
        first = self._discovered_event.wait(timeout=self._discovery_timeout)
        if not first and not self._allocated:
            log.warning("no nodes discovered; nothing to talk to")
            return
        time.sleep(0.75)  # grace period for additional discoveries
        log.info("discovered %d node(s): %s", len(self._allocated), self._allocated)

    def _discover_names(self) -> None:
        assert self._transport is not None
        for node_id in list(self._allocated):
            try:
                future = self._transport.send_request(
                    dest_id=node_id,
                    type_id=TYPE_GET_PREFERRED_NAME,
                    payload=b"",
                    timeout=2.0,
                )
                response = future.result(timeout=2.5)
                name = response.decode("utf-8", errors="replace").strip("\x00").strip()
                self._names[node_id] = name
                log.info("node %d preferred_name=%r", node_id, name)
            except Exception as exc:
                log.warning("could not get preferred name for node %d: %s", node_id, exc)

    def _pick_display_node(self) -> int | None:
        # The cube base board's preferred name is `cube` (or
        # `com.abstractfoundry.cube`); it owns the `display` module along with
        # `buttons`, `screen`, `speaker`. The sibling sensor board's name is
        # `button_and_light_sensor` and owns no LEDs.
        priority = ("display", "cube")
        for needle in priority:
            for node_id, name in self._names.items():
                lname = name.lower()
                if needle in lname and "sensor" not in lname:
                    return node_id
        # Last-resort fallback: any node we have a name for, otherwise first allocated.
        if self._names:
            return next(iter(self._names))
        if self._allocated:
            return next(iter(self._allocated))
        return None
