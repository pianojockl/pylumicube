"""Client bindings: a `LumiCube`-shaped proxy over the control socket.

Scripts (and the compat shim) talk to a *daemon* through an object that
looks like the `LumiCube` they already use — `.display`, `.screen`,
`.speaker`, `.modules`, `.light_sensor`, `.buttons`, `.telemetry` — so the
drivers, the namespace and the scripts themselves are unchanged. The
drivers were built around injected callables (`write_named`, `read_named`,
`write_run`) and `Display` around a transport, which is what makes this a
binding rather than a rewrite.

`connect_or_open()` is the entry point scripts should use: connect to the
daemon when one is listening, otherwise open the serial port directly, so a
fresh install with no daemon behaves exactly as it did before.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from typing import Any

from .constants import SERIAL_DEVICE
from .control import (ControlClient, ControlError, _b64encode, decode_schemas,
                      resolve_socket_path, socket_is_live)
from .metadata import FieldSpec
from .modules import FieldModule, build_namespace

log = logging.getLogger(__name__)


class RemoteTelemetry:
    """`TelemetryStore`-shaped view of the daemon's cache.

    Values arrive as `values` events; reads are dict lookups, so the
    namespace, `FieldModule.wait_for` and the drivers all work unchanged.
    """

    def __init__(self, client: "CubeClient") -> None:
        self._client = client
        self._lock = threading.Lock()
        self._updated = threading.Condition(self._lock)
        self._schemas: dict[int, Mapping[int, FieldSpec]] = {}
        self._values: dict[int, dict[int, int]] = {}
        self._last_update: dict[int, float] = {}
        self._subscribed: set[int] = set()
        self._frames = 0
        self._started = time.time()
        self._handlers: list[Callable[[int, Mapping[int, int]], None]] = []

    # ---------------- store API ----------------

    def set_schema(self, node_id: int, specs: Mapping[int, FieldSpec]) -> None:
        with self._lock:
            self._schemas[node_id] = dict(specs)

    @property
    def schemas(self) -> dict[int, Mapping[int, FieldSpec]]:
        with self._lock:
            return dict(self._schemas)

    def subscribe(self, node_ids: Iterable[int] | None = None, *,
                  timeout: float = 2.0) -> list[int]:
        """Ask the daemon to subscribe (and to push us those nodes)."""
        payload = sorted({int(node) for node in node_ids}) if node_ids else []
        reply = self._client.control.request("subscribe", nodes=payload,
                                             timeout=timeout + 2.0)
        subscribed = [int(node) for node in reply.get("subscribed") or []]
        with self._lock:
            self._subscribed.update(subscribed)
        return subscribed

    @property
    def subscribed_nodes(self) -> set[int]:
        with self._lock:
            return set(self._subscribed)

    def reset_subscriptions(self) -> None:
        """Forget what we asked for — the daemon lost it when it restarted."""
        with self._lock:
            self._subscribed.clear()

    def start(self) -> None:
        """No-op: the daemon's own heartbeat keeps the subscription alive."""

    def stop(self) -> None:
        """No-op: the daemon owns the subscription."""

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
        """Block until `key` has been published, or return `default`."""
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
            return {"frames": self._frames, "nodes": len(self._values),
                    "subscribed": sorted(self._subscribed),
                    "uptime": round(time.time() - self._started, 1),
                    "remote": True}

    # ---------------- event sink ----------------

    def on_values(self, callback: Callable[[int, Mapping[int, int]], None]) -> None:
        """Add a callback for pushed frames (the microphone captures this way)."""
        with self._lock:
            self._handlers.append(callback)

    def remove_values_handler(self,
                              callback: Callable[[int, Mapping[int, int]], None]) -> None:
        with self._lock:
            try:
                self._handlers.remove(callback)
            except ValueError:
                pass

    def _on_values(self, node_id: int, values: Mapping[int, int]) -> None:
        with self._updated:
            node_values = self._values.setdefault(node_id, {})
            node_values.update(values)
            self._last_update[node_id] = time.monotonic()
            self._frames += 1
            self._updated.notify_all()
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(node_id, values)
            except Exception:  # noqa: BLE001 — a handler must not kill the reader
                log.exception("telemetry value handler raised")


class RemoteTransport:
    """The slice of `Transport` that `Display` needs, forwarded to the daemon.

    `Display` batches field writes itself and hands raw frames to a
    transport, so forwarding `send_request` is enough to run it — and the
    flags travel with the request, which is what keeps a scroll animation's
    fire-and-forget frames fire-and-forget across the socket.
    """

    def __init__(self, client: "CubeClient") -> None:
        self._client = client

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     priority: int = 20, timeout: float = 5.0,
                     expect_response: bool = True) -> Future:
        future = self._client.control.request_async(
            "frame",
            dest_id=int(dest_id),
            type_id=int(type_id),
            payload=_b64encode(payload),
            expect_response=bool(expect_response),
            wait=bool(expect_response),
            timeout=float(timeout),
        )

        def _unwrap(reply_future: Future) -> bytes:
            reply = reply_future.result()
            if not reply.get("ok"):
                raise ControlError(str(reply.get("error", "frame failed")),
                                   code=str(reply.get("code", "error")))
            return b""

        return _forward(future, _unwrap)


def _forward(source: Future, transform: Any) -> Future:
    """Chain `transform` onto `source`, returning a new Future."""
    target: Future = Future()

    def _run() -> None:
        try:
            target.set_result(transform(source))
        except Exception as exc:  # noqa: BLE001 — propagate to the caller
            target.set_exception(exc)

    source.add_done_callback(lambda _future: _run())
    return target


class CubeClient:
    """A `LumiCube`-shaped proxy backed by the daemon's control socket."""

    def __init__(self, path: str | None = None, *, timeout: float = 5.0) -> None:
        self.control = ControlClient(path, timeout=timeout,
                                     on_event=self._on_event)
        self.control.connect()
        self.path = self.control.path
        self.info = self.control.info
        self._names = {int(node): name
                       for node, name in (self.info.get("nodes") or {}).items()}
        self._cube_node_id = self.info.get("cube_node_id")
        self._telemetry = RemoteTelemetry(self)
        for node_id, specs in decode_schemas(
                self.info.get("schemas") or {}).items():
            self._telemetry.set_schema(node_id, specs)
        self._namespace: dict[str, FieldModule] | None = None
        self._display = None
        self._screen = None
        self._speaker = None
        self._microphone = None
        self._transport = RemoteTransport(self)
        self._reconnect_lock = threading.Lock()
        log.info("connected to the daemon on %s (%d nodes, %d modules)",
                 self.path, len(self._names), len(self.info.get("modules") or []))

    # ---------------- lifecycle ----------------

    def start(self) -> "CubeClient":
        return self

    def stop(self) -> None:
        self.close()

    def close(self) -> None:
        self.control.close()

    def __enter__(self) -> "CubeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _on_event(self, message: Mapping[str, Any]) -> None:
        if message.get("event") == "values":
            node = message.get("node")
            values = message.get("values") or {}
            if node is None:
                return
            self._telemetry._on_values(                     # noqa: SLF001
                int(node), {int(k): int(v) for k, v in values.items()})

    def reconnect(self) -> None:
        """Re-establish the connection after the daemon went away.

        A daemon restart closes every client's socket, and a long-running
        client (the clock) should come back rather than become a zombie that
        fails every request. The drivers and the telemetry view hold this
        object, not the socket, so swapping the underlying `ControlClient`
        is enough — the schemas and subscriptions are refreshed here.
        """
        with self._reconnect_lock:
            subscriptions = sorted(self._telemetry.subscribed_nodes)
            try:
                self.control.close()
            except Exception:  # noqa: BLE001 — the socket is already gone
                pass
            control = ControlClient(self.path, timeout=self.control.timeout,
                                    on_event=self._on_event)
            control.connect()
            self.control = control
            self.info = control.info
            self._names = {int(node): name
                           for node, name in (self.info.get("nodes") or {}).items()}
            self._cube_node_id = self.info.get("cube_node_id")
            self._apply_schemas(self.info.get("schemas") or {})
            self._telemetry.reset_subscriptions()
            if subscriptions:
                self._telemetry.subscribe(subscriptions)
            log.info("reconnected to the daemon on %s (%d nodes)",
                     self.path, len(self._names))

    # ---------------- telemetry ----------------

    @property
    def telemetry(self) -> RemoteTelemetry:
        return self._telemetry

    def enable_telemetry(self, node_ids: Iterable[int] | None = None,
                         node_names: Iterable[str] | None = None, *,
                         subscribe: bool = True,
                         subscribe_except: Iterable[str] = (),
                         budget: float | None = None) -> RemoteTelemetry:
        """Subscribe on the daemon and start receiving pushed values.

        The daemon owns discovery and the telemetry heartbeat, so this only
        chooses which nodes we are told about.
        """
        if subscribe:
            self._telemetry.subscribe(self._resolve_nodes(node_ids, node_names,
                                                          subscribe_except))
        return self._telemetry

    def _resolve_nodes(self, node_ids: Iterable[int] | None,
                       node_names: Iterable[str] | None,
                       subscribe_except: Iterable[str] = ()) -> list[int]:
        if node_ids is not None:
            resolved = sorted({int(node) for node in node_ids})
        elif node_names is not None:
            wanted = [name.lower() for name in node_names]
            resolved = sorted(node for node, name in self._names.items()
                              if any(w in name.lower() for w in wanted))
        else:
            resolved = sorted(self._names)
        excluded = {name.lower() for name in subscribe_except}
        return [node for node in resolved
                if self._names.get(node, "").lower() not in excluded]

    def walk_all_schemas(self, *, budget: float | None = None) -> dict[int, int]:
        """Ask the daemon to (re)walk every schema, then refresh our copy."""
        reply = self.control.request("walk", timeout=60.0)
        counts = {int(node): int(count)
                  for node, count in (reply.get("counts") or {}).items()}
        self._apply_schemas(reply.get("schemas") or {})
        return counts

    def _apply_schemas(self, encoded: Mapping[str, Mapping[str, Any]]) -> None:
        if not encoded:
            return
        for node_id, specs in decode_schemas(encoded).items():
            self._telemetry.set_schema(node_id, specs)
        self._namespace = None

    @property
    def schemas(self) -> dict[int, Mapping[int, FieldSpec]]:
        return self._telemetry.schemas

    @property
    def node_names(self) -> dict[int, str]:
        return dict(self._names)

    def node_id_for_name(self, name: str) -> int | None:
        wanted = name.lower()
        for node_id, node_name in self._names.items():
            if node_name.lower() == wanted:
                return node_id
        for node_id, node_name in self._names.items():
            if wanted in node_name.lower():
                return node_id
        return None

    # ---------------- modules ----------------

    @property
    def modules(self) -> dict[str, FieldModule]:
        if self._namespace is None:
            self._namespace = build_namespace(self._telemetry.schemas,
                                              self._names, self._telemetry)
        return self._namespace

    def module(self, name: str) -> FieldModule:
        try:
            return self.modules[name]
        except KeyError:
            raise KeyError(
                f"no module {name!r}; known modules: {sorted(self.modules)}") from None

    @property
    def light_sensor(self) -> FieldModule | None:
        return self.modules.get("light_sensor")

    @property
    def buttons(self) -> FieldModule | None:
        return self.modules.get("buttons")

    # ---------------- writes ----------------

    def write_named(self, module_name: str, values: Mapping[str, int | bytes], *,
                    timeout: float = 2.0, await_response: bool = True,
                    wait: bool = True) -> Any:
        """Write fields by module + name (`"pixel_data_stream+3"` offsets work).

        `wait=False` returns the reply Future instead of blocking — the
        daemon still owns the actual write, so a caller streaming frames
        can keep several in flight.
        """
        future = self.control.request_async(
            "write", module=module_name, values=dict(values),
            timeout=float(timeout), await_response=bool(await_response),
            wait=bool(wait))
        if not wait:
            return future
        _check(future.result(timeout=timeout + 5.0))
        return None

    def write_run(self, module_name: str, field_name: str, samples: bytes, *,
                  timeout: float = 2.0, await_response: bool = True,
                  wait: bool = True) -> Any:
        """Write a homogeneous run (the speaker's `data` block)."""
        future = self.control.request_async(
            "run", module=module_name, field=field_name,
            samples=_b64encode(bytes(samples)), timeout=float(timeout),
            await_response=bool(await_response), wait=bool(wait))
        if not wait:
            return future
        _check(future.result(timeout=timeout + 5.0))
        return None

    def read_named(self, module_name: str, field_name: str) -> int | None:
        """Read a module field from the daemon's telemetry cache."""
        try:
            reply = self.control.request("read", module=module_name,
                                         field=field_name)
        except ControlError as exc:
            if exc.code == "no_field":
                return None
            raise
        return reply.get("value")

    # ---------------- audio (atomic on the daemon) ----------------

    def say(self, text: str, *, voice: str | None = None, speed: int | None = None,
            volume: int | None = None) -> None:
        """Ask the daemon to speak, holding the speaker for the utterance.

        Going through the daemon (rather than streaming chunks from here)
        is what stops an announcement from Home Assistant talking over the
        clock's hourly chime.
        """
        fields: dict[str, Any] = {"text": text}
        if voice is not None:
            fields["voice"] = voice
        if speed is not None:
            fields["speed"] = speed
        if volume is not None:
            fields["volume"] = volume
        self.control.request("say", timeout=120.0, **fields)

    def tone(self, frequency: float = 440.0, duration: float = 0.5, *,
             amplitude: float = 0.25, function: str = "sine") -> None:
        self.control.request("tone", timeout=duration + 10.0,
                             frequency=frequency, duration=duration,
                             amplitude=amplitude, function=function)

    def play(self, path: str) -> None:
        self.control.request("play", path=path, timeout=600.0)

    def stop_audio(self) -> None:
        """Stop whatever is being said (including another client's stream)."""
        self.control.request("stop_audio")

    def stats(self) -> dict[str, Any]:
        return self.control.request("stats")

    # ---------------- drivers ----------------

    @property
    def display(self):
        """The LED matrix driver, backed by the daemon's transport."""
        if self._display is None:
            from .display import Display
            node_id = self._cube_node_id
            if node_id is None:
                raise ControlError("the daemon did not report a cube node",
                                   code="no_cube")
            self._display = Display(self._transport, node_id=int(node_id))
        return self._display

    @property
    def screen(self):
        """The secondary LCD driver."""
        if self._screen is None:
            from .screen import Screen
            self._screen = Screen(self.write_named)
        return self._screen

    @property
    def speaker(self):
        """The speaker driver."""
        if self._speaker is None:
            from .speaker import Speaker
            self._speaker = Speaker(
                self.write_named,
                read=lambda name: self.read_named("speaker", name),
                run=lambda samples, **kwargs: self.write_run("speaker", "data",
                                                             samples, **kwargs),
            )
        return self._speaker

    @property
    def microphone(self):
        """The microphone driver (16 kHz s16le capture).

        The microphone lives on the cube board, so `enable()` asks the
        daemon to subscribe to it — which costs speaker throughput while it
        is on (see `pylumicube.microphone`).
        """
        if self._microphone is None:
            from .microphone import Microphone
            module = self.module("microphone")

            def subscribe() -> None:
                node_id = next(iter(module.refs.values())).node_id
                self.enable_telemetry(node_ids=[node_id])

            def unsubscribe() -> None:
                node_id = next(iter(module.refs.values())).node_id
                self.control.request("unsubscribe", nodes=[node_id], timeout=10.0)

            self._microphone = Microphone(module, telemetry=self._telemetry,
                                          write=self.write_named,
                                          subscribe=subscribe,
                                          unsubscribe=unsubscribe)
        return self._microphone


def _check(reply: Mapping[str, Any]) -> None:
    if not reply.get("ok"):
        raise ControlError(str(reply.get("error", "request failed")),
                           code=str(reply.get("code", "error")))


def connect(path: str | None = None, *, timeout: float = 5.0) -> CubeClient:
    """Connect to the daemon, raising ControlError if there is none."""
    return CubeClient(path, timeout=timeout)


def daemon_available(path: str | None = None) -> bool:
    """True when a daemon is listening on the control socket."""
    return socket_is_live(resolve_socket_path(path))


@contextlib.contextmanager
def connect_or_open(port: str = SERIAL_DEVICE, path: str | None = None,
                    *, prefer_daemon: bool = True, **lumi_kwargs: Any):
    """Yield the daemon's proxy if one is listening, else open the port.

    This is what scripts should use: with a daemon running they share the
    cube with everything else; without one they own the port exactly as
    before, so a fresh install needs no daemon.
    """
    from .node import LumiCube

    if prefer_daemon and daemon_available(path):
        client = CubeClient(path)
        try:
            yield client
        finally:
            client.close()
        return
    with LumiCube(port, **lumi_kwargs) as cube:
        yield cube
