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
from collections.abc import Iterable, Mapping
from concurrent.futures import Future

from . import discovery as _discovery
from . import flat_dictionary
from . import uavcan as u
from .allocator import Allocator
from .constants import (
    BAUD,
    DAEMON_NODE_ID,
    SERIAL_DEVICE,
    TYPE_ALLOCATION,
    TYPE_GET_NODE_INFO,
    TYPE_GET_PREFERRED_NAME,
    TYPE_SET_FIELDS,
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
        self._screen = None
        self._speaker = None


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
        # The namespace is derived from the schemas, so it must be rebuilt
        # after a new one lands (e.g. walking the cube board after enabling
        # telemetry for the sensor board only).
        self._namespace = None
        log.info("node %d (%s): %d field block(s) discovered in %.1fs",
                 node_id, name or "?", len(specs), time.monotonic() - started)
        return specs

    def walk_all_schemas(self, *, budget: float | None = None) -> dict[int, int]:
        """Walk every discovered node's schema without subscribing to it.

        Useful when a driver needs another node's field keys but its
        telemetry stream would compete for the link — e.g. audio playback
        wants the cube board's `speaker` keys while subscribing only to the
        sensor board. Returns ``{node_id: field_count}``.
        """
        counts: dict[int, int] = {}
        for node_id in sorted(self._allocated):
            counts[node_id] = len(self.discover_schema(node_id, budget=budget))
        return counts

    @property
    def schemas(self) -> dict[int, dict[int, FieldSpec]]:
        """Discovered per-node schemas: ``{node_id: {floor: FieldSpec}}``."""
        return {node_id: dict(specs) for node_id, specs in self._schemas.items()}

    def _repair_schema(self, node_id: int, key: int) -> bool:
        """Probe one key and add it to a node's schema (telemetry callback).

        Called when a `PUBLISHED_FIELDS` frame references a key the walk did
        not discover. With the leading skip understood as an absolute key
        (see `pylumicube.discovery`) this is a single query, no probing.
        """
        specs = self._schemas.get(node_id)
        if specs is None or self._transport is None:
            return False
        result = _discovery.probe_key(self._transport, node_id, key,
                                      timeout=self._schema_timeout)
        if result is None:
            return False
        meta, field_key = result
        spec = _discovery.spec_from_metadata(field_key, meta)
        if not spec.covers(key):
            # Trust the probe: it answered for exactly this key.
            spec = _discovery.spec_from_metadata(key, meta)
        specs[spec.floor] = spec
        self._namespace = None
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

    # ---------------- raw field writes ----------------

    def write_fields(self, node_id: int, values: Mapping[int, int | bytes], *,
                     timeout: float = 2.0, await_response: bool = True,
                     wait: bool = True) -> "Future | None":
        """Write raw field values to a node (one SET_FIELDS request).

        Keys are absolute; values are encoded per the node's discovered
        schema.

        `await_response=False` waits only for the link-level ACK, for
        peripherals that service writes without answering (the speaker).
        `wait=False` returns immediately with the link future instead of
        blocking on it — the building block for streaming: a single write
        costs ~50-100 ms of *latency* on this hardware, but pipelined writes
        sustain several hundred frames per second.
        """
        if self._transport is None:
            raise RuntimeError("cube not started")
        specs = self._schemas.get(node_id)
        if specs is None:
            raise RuntimeError(f"no schema for node {node_id}; "
                               f"enable telemetry or call discover_schema() first")
        payload = flat_dictionary.serialise(values, specs)
        future = self._transport.send_request(
            dest_id=node_id, type_id=TYPE_SET_FIELDS, payload=payload,
            timeout=timeout, expect_response=await_response)
        if not wait:
            return future
        future.result(timeout=timeout)
        return None

    def write_named(self, module_name: str, values: Mapping[str, int | bytes], *,
                    timeout: float = 2.0, await_response: bool = True,
                    wait: bool = True) -> "Future | None":
        """Write fields by module + field name.

        Names come from the module namespace. A homogeneous run (the
        screen's `pixel_data_stream` or `text`) is addressed with a ``+n``
        offset from its first field, e.g. ``"pixel_data_stream+3"`` — those
        individual keys are not separate namespace entries.
        """
        module = self.module(module_name)
        refs = module.refs
        if not refs:
            raise RuntimeError(f"module {module_name!r} has no fields")
        node_id = next(iter(refs.values())).node_id
        keys: dict[int, int | bytes] = {}
        for name, value in values.items():
            base, _, offset_text = name.partition("+")
            ref = module.ref(base)
            offset = int(offset_text) if offset_text else 0
            if offset and offset >= ref.spec.span:
                raise ValueError(f"{module_name}.{base} has span {ref.spec.span}; "
                                 f"offset {offset} is out of range")
            keys[ref.key + offset] = value
        return self.write_fields(node_id, keys, timeout=timeout,
                                 await_response=await_response, wait=wait)

    def write_run(self, module_name: str, field_name: str,
                  values: Iterable[int], *, timeout: float = 2.0,
                  await_response: bool = False,
                  wait: bool = True) -> "Future | None":
        """Fast path for streaming: write a contiguous run starting at a field.

        `values[i]` goes to `field + i`, which is how the speaker's 112-sample
        blocks and the screen's 80-pixel blocks are addressed. Avoids the
        per-value name resolution and dict building of `write_named`, which
        matters at hundreds of frames per second.
        """
        if self._transport is None:
            raise RuntimeError("cube not started")
        ref = self.module(module_name).ref(field_name)
        payload = flat_dictionary.serialise_run(ref.key, values, ref.spec)
        future = self._transport.send_request(
            dest_id=ref.node_id, type_id=TYPE_SET_FIELDS, payload=payload,
            timeout=timeout, expect_response=await_response)
        if not wait:
            return future
        future.result(timeout=timeout)
        return None

    @property
    def screen(self):
        """The secondary LCD driver (320x240). Requires telemetry."""
        if self._screen is None:
            from .screen import Screen
            self._screen = Screen(self.write_named)
        return self._screen

    @property
    def speaker(self):
        """The speaker driver (32 kHz PCM streaming). Requires telemetry."""
        if self._speaker is None:
            from .speaker import Speaker
            self._speaker = Speaker(
                self.write_named,
                read=lambda name: self.read_named("speaker", name),
                run=lambda samples, **kwargs: self.write_run("speaker", "data",
                                                             samples, **kwargs),
            )
        return self._speaker

    def read_named(self, module_name: str, field_name: str) -> int | None:
        """Read a module field from the telemetry cache (None if unavailable).

        There is no field-read service, so this only sees values the node
        has published — which is enough for the speaker's
        `free_space_in_bytes` flow control when telemetry is enabled for the
        cube board.
        """
        if self._telemetry is None:
            return None
        try:
            ref = self.module(module_name).ref(field_name)
        except (KeyError, AttributeError, RuntimeError):
            return None
        value = self._telemetry.get(ref.node_id, ref.key)
        return value if isinstance(value, int) else None

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

    def _discover_names(self, attempts: int = 3) -> None:
        """Resolve every discovered node's preferred name.

        The link drops replies (the daemon logs retransmissions constantly),
        so a single lost GET_PREFERRED_NAME used to leave a node nameless —
        and since the module namespace is keyed by name, that silently
        removed whole modules. Retry until every node answers or we run out
        of attempts.
        """
        assert self._transport is not None
        for attempt in range(attempts):
            pending = [node_id for node_id in self._allocated
                       if node_id not in self._names]
            if not pending:
                return
            for node_id in pending:
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
                    log.warning("could not get preferred name for node %d "
                                "(attempt %d/%d): %s", node_id, attempt + 1,
                                attempts, exc)
            if attempt + 1 < attempts:
                time.sleep(0.2)

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
