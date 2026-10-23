"""Control protocol: one process owns the cube, clients talk to it.

The LumiCube's serial port has a single owner, but several things want to
use it — the clock, a script, Home Assistant. This module is the split that
upstream had too (`/tmp/foundry_daemon.sock`, thin module proxies): a
*daemon* holds the link and serves operations on a Unix socket, and clients
get bindings that look like the `LumiCube` API they already use.

Wire format: one JSON object per line, UTF-8, newline-terminated. A request
carries an `id` and an `op`; the reply echoes the `id`. Unsolicited events
carry `event` and no `id`.

    {"id": 1, "op": "read", "module": "light_sensor", "field": "ambient_light"}
    {"id": 1, "ok": true, "value": 1232}
    {"event": "values", "node": 124, "values": {"2049": 1232}}

Writes carry the same `await_response` / `wait` flags as
`LumiCube.write_fields`, so a streaming caller (the speaker, a scroll
animation) is not forced to wait for a ServiceResponse just because it
crossed a socket.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import socket
import threading
import time
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

#: Socket file name, inside `$XDG_RUNTIME_DIR` (or /tmp when unset).
DEFAULT_SOCKET_NAME = "pylumicube.sock"

#: Environment variable that overrides where clients look for the daemon.
ENV_SOCKET = "PYLUMICUBE_SOCKET"

#: Longest line we will read from a client (a speaker frame is ~300 bytes).
MAX_LINE = 4 * 1024 * 1024

#: Protocol version, bumped when an op's shape changes incompatibly.
PROTOCOL_VERSION = 1


def default_socket_path() -> str:
    """Where the daemon listens by default."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not os.path.isdir(runtime):
        runtime = "/tmp"
    return os.path.join(runtime, DEFAULT_SOCKET_NAME)


def resolve_socket_path(path: str | None = None) -> str:
    """Explicit path, else `$PYLUMICUBE_SOCKET`, else the default."""
    if path:
        return os.path.expanduser(path)
    return os.path.expanduser(os.environ.get(ENV_SOCKET) or default_socket_path())


def socket_is_live(path: str | None = None, *, timeout: float = 0.5) -> bool:
    """True when something is listening on the control socket."""
    target = resolve_socket_path(path)
    if not os.path.exists(target):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(target)
    except OSError:
        return False
    finally:
        probe.close()
    return True


class ControlError(RuntimeError):
    """An operation the daemon refused, with a machine-readable code."""

    def __init__(self, message: str, *, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


def encode_schemas(schemas: Mapping[int, Mapping[int, Any]]) -> dict[str, dict[str, Any]]:
    """FieldSpec tables -> JSON (keys and node ids become strings)."""
    out: dict[str, dict[str, Any]] = {}
    for node_id, specs in schemas.items():
        entries: dict[str, Any] = {}
        for key, spec in specs.items():
            entries[str(key)] = {
                "floor": spec.floor,
                "span": spec.span,
                "type": int(spec.type),
                "size": int(spec.size),
                "name": spec.name,
                "module": spec.module,
                "units": spec.units,
                "min": spec.min_value,
                "max": spec.max_value,
            }
        out[str(node_id)] = entries
    return out


def decode_schemas(payload: Mapping[str, Mapping[str, Any]]) -> dict[int, dict[int, Any]]:
    """JSON -> FieldSpec tables, mirroring `encode_schemas`."""
    from .metadata import FieldSpec

    out: dict[int, dict[int, Any]] = {}
    for node_text, entries in payload.items():
        specs: dict[int, FieldSpec] = {}
        for key_text, entry in entries.items():
            specs[int(key_text)] = FieldSpec(
                floor=int(entry["floor"]),
                span=int(entry["span"]),
                type=int(entry["type"]),
                size=int(entry["size"]),
                name=entry.get("name", ""),
                module=entry.get("module", ""),
                units=entry.get("units", ""),
                min_value=entry.get("min"),
                max_value=entry.get("max"),
            )
        out[int(node_text)] = specs
    return out


def _b64encode(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class _Connection:
    """One client, with a lock so events never interleave with replies."""

    def __init__(self, sock: socket.socket, address: str = "") -> None:
        self.sock = sock
        self.address = address
        self.write_lock = threading.Lock()
        self.alive = True
        self.subscriptions: set[int] = set()
        self.ops = 0

    def send(self, message: Mapping[str, Any]) -> None:
        line = json.dumps(message).encode("utf-8") + b"\n"
        with self.write_lock:
            if not self.alive:
                return
            try:
                self.sock.sendall(line)
            except OSError:
                self.alive = False

    def close(self) -> None:
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ControlServer:
    """Serve cube operations on a Unix socket.

    Threaded: one thread per client, plus whatever threads the cube itself
    runs. `cube` is anything with the `LumiCube` API — the real thing, or a
    client proxy (which lets a daemon chain to another daemon).
    """

    def __init__(self, cube: Any, path: str | None = None, *,
                 log_ops: bool = False,
                 protected_nodes: Iterable[str] = (),
                 allow_protected: bool = False) -> None:
        self.cube = cube
        self.path = resolve_socket_path(path)
        self.log_ops = log_ops
        #: Node names whose telemetry is refused unless `allow_protected`:
        #: the cube board publishes everything at full rate (its LED block
        #: included), which drowns the link — measured at 30 service
        #: timeouts in 30 seconds, with display writes failing.
        self.protected_nodes = {name.lower() for name in protected_nodes}
        self.allow_protected = bool(allow_protected)
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[_Connection] = set()
        #: Serialises writes that must not interleave (a scroll animation and
        #: a frame update from another client).
        self.write_lock = threading.RLock()
        #: Held for a whole utterance, so two clients cannot talk over each
        #: other — and deliberately *not* the write lock, so a three-second
        #: announcement does not stall the clock's display updates.
        self.audio_lock = threading.RLock()
        self._audio_stopped = threading.Event()
        self._last_audio_op = 0.0
        self._started = time.time()
        self._ops_served = 0
        #: Telemetry events waiting for the sender thread (bounded: dropping
        #: frames is better than stalling the link that feeds them).
        self._events: queue.Queue = queue.Queue(maxsize=256)
        self._sender: threading.Thread | None = None
        self._dropped_events = 0

    def start(self) -> None:
        if self._listener is not None:
            return
        self._unlink_stale()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        os.chmod(self.path, 0o600)      # owner only; MQTT is the network face
        listener.listen(8)
        listener.settimeout(0.5)
        self._listener = listener
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="pylumicube-control")
        self._thread.start()
        self._sender = threading.Thread(target=self._sender_loop, daemon=True,
                                        name="pylumicube-events")
        self._sender.start()
        log.info("control socket listening on %s", self.path)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._events.put_nowait(None)       # wake the sender
        except queue.Full:
            pass
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sender is not None:
            self._sender.join(timeout=2.0)
            self._sender = None
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:  # pragma: no cover - race with another process
            log.debug("could not unlink %s", self.path, exc_info=True)

    def __enter__(self) -> "ControlServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _unlink_stale(self) -> None:
        """Remove a leftover socket file, refusing if someone is listening."""
        if not os.path.exists(self.path):
            return
        if socket_is_live(self.path):
            raise ControlError(f"another daemon is already listening on {self.path}",
                               code="in_use")
        log.warning("removing stale control socket %s", self.path)
        os.unlink(self.path)

    # ---------------- connections ----------------

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            connection = _Connection(sock)
            with self._lock:
                self._connections.add(connection)
            threading.Thread(target=self._serve, args=(connection,),
                             daemon=True, name="pylumicube-client").start()

    def _serve(self, connection: _Connection) -> None:
        buffer = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = connection.sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if not self._handle(connection, line):
                        return
        finally:
            with self._lock:
                self._connections.discard(connection)
            connection.close()

    def _handle(self, connection: _Connection, line: bytes) -> bool:
        """Dispatch one request line. Returns False to close the connection."""
        try:
            request = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            connection.send({"ok": False, "error": "malformed JSON",
                             "code": "bad_request"})
            return True
        if not isinstance(request, dict):
            connection.send({"ok": False, "error": "request must be an object",
                             "code": "bad_request"})
            return True

        op = request.get("op")
        request_id = request.get("id")
        if op == "quit":
            return False
        try:
            reply = self._dispatch(connection, str(op), request)
        except ControlError as exc:
            reply = {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:  # noqa: BLE001 — one bad op must not kill the daemon
            log.exception("control op %r failed", op)
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                     "code": "internal"}
        connection.ops += 1
        with self._lock:
            self._ops_served += 1
        if self.log_ops:
            log.info("op %s -> %s", op,
                     "ok" if reply.get("ok") else reply.get("error"))
        if request_id is not None:
            reply["id"] = request_id
        connection.send(reply)
        return True

    # ---------------- events ----------------

    def broadcast(self, message: Mapping[str, Any]) -> None:
        """Queue an event for every connected client."""
        self._enqueue(message)

    def broadcast_values(self, node_id: int, values: Mapping[int, int]) -> None:
        """Queue freshly published field values for the subscribers.

        Queued, never sent here: this runs on the link's I/O thread, and
        serialising a frame for every client (a cube-board frame carries a
        couple of hundred values) is easily slow enough to stall the wire —
        which then makes service requests time out. A dedicated sender
        thread does the work, and the queue is bounded so a client that
        cannot keep up loses frames instead of slowing the cube down.
        """
        if not values:
            return
        self._enqueue({"event": "values", "node": node_id,
                       "values": {str(key): int(value)
                                  for key, value in values.items()}})

    def _enqueue(self, message: Mapping[str, Any]) -> None:
        try:
            self._events.put_nowait(dict(message))
        except queue.Full:
            self._dropped_events += 1
            if self._dropped_events % 100 == 1:
                log.warning("dropping telemetry events: %d clients are behind",
                            self._dropped_events)

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._events.get(timeout=0.5)
            except queue.Empty:
                continue
            if message is None:
                break
            node_id = message.get("node")
            with self._lock:
                targets = [c for c in self._connections
                           if node_id is None or not c.subscriptions
                           or node_id in c.subscriptions]
            for connection in targets:
                connection.send(message)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._connections)

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    # ---------------- operations ----------------

    def _dispatch(self, connection: _Connection, op: str,
                  request: Mapping[str, Any]) -> dict[str, Any]:
        """Run one operation. Raises ControlError for anything refused."""
        cube = self.cube

        if op == "ping":
            return {"ok": True, "version": PROTOCOL_VERSION}

        if op == "hello":
            return {"ok": True, "version": PROTOCOL_VERSION, "socket": self.path,
                    "cube_node_id": cube.node_id_for_name("cube"),
                    "nodes": {str(k): v for k, v in cube.node_names.items()},
                    "schemas": encode_schemas(cube.schemas),
                    "modules": sorted(cube.modules)}

        if op == "schemas":
            return {"ok": True, "schemas": encode_schemas(cube.schemas)}

        if op == "walk":
            counts = cube.walk_all_schemas()
            return {"ok": True, "counts": {str(k): v for k, v in counts.items()},
                    "schemas": encode_schemas(cube.schemas)}

        if op == "subscribe":
            nodes = self._node_list(request.get("nodes"))
            refused = self._protected_in(nodes)
            if refused and not self.allow_protected:
                raise ControlError(
                    "refusing to subscribe to "
                    f"{', '.join(refused)}: that board publishes every field at "
                    "full rate and drowns the link (display writes then time "
                    "out). Start the daemon with --allow-cube-telemetry to "
                    "permit it, and expect a degraded cube while it lasts.",
                    code="link_protected")
            with self.write_lock:
                subscribed = cube.telemetry.subscribe(nodes or None)
            connection.subscriptions.update(subscribed)
            return {"ok": True, "subscribed": sorted(subscribed),
                    "telemetry": cube.telemetry.stats()
                    if hasattr(cube.telemetry, "stats") else {}}

        if op == "unsubscribe":
            nodes = self._node_list(request.get("nodes"))
            with self.write_lock:
                stopped = cube.telemetry.unsubscribe(nodes or None)
            connection.subscriptions.difference_update(stopped)
            return {"ok": True, "unsubscribed": sorted(stopped)}

        if op == "read":
            module = self._module(request)
            field = str(request.get("field", ""))
            if field not in module.refs:
                raise ControlError(f"{module.name!r} has no field {field!r}",
                                   code="no_field")
            return {"ok": True, "value": module.get(field)}

        if op == "read_all":
            module = self._module(request)
            return {"ok": True, "values": module.snapshot()}

        if op == "write":
            module = str(request.get("module", ""))
            values = request.get("values") or {}
            if not isinstance(values, Mapping):
                raise ControlError("values must be an object", code="bad_request")
            with self.write_lock:
                cube.write_named(module, values,
                                 timeout=float(request.get("timeout", 2.0)),
                                 await_response=bool(request.get("await_response", True)),
                                 wait=bool(request.get("wait", True)))
            if module == "speaker" and "volume" in values:
                # Keep the daemon's own view in step, so the volume it
                # publishes to Home Assistant is the one a client just set.
                try:
                    cube.speaker.note_volume(int(values["volume"]))
                except Exception:  # noqa: BLE001 — cosmetic only
                    log.debug("could not mirror the speaker volume", exc_info=True)
            return {"ok": True}

        if op == "run":
            module = str(request.get("module", ""))
            field = str(request.get("field", ""))
            samples = _b64decode(str(request.get("samples", "")))
            if module == "speaker":
                # A stop from Home Assistant drops the rest of a stream
                # whoever started it; a gap means this is a new one.
                now = time.monotonic()
                if now - self._last_audio_op > 0.5:
                    self._audio_stopped.clear()
                self._last_audio_op = now
                if self._audio_stopped.is_set():
                    return {"ok": True, "dropped": True}
                with self.audio_lock:
                    cube.write_run(module, field, samples,
                                   timeout=float(request.get("timeout", 2.0)),
                                   await_response=bool(request.get("await_response", True)),
                                   wait=bool(request.get("wait", True)))
                return {"ok": True}
            with self.write_lock:
                cube.write_run(module, field, samples,
                               timeout=float(request.get("timeout", 2.0)),
                               await_response=bool(request.get("await_response", True)),
                               wait=bool(request.get("wait", True)))
            return {"ok": True}

        if op == "frame":
            with self.write_lock:
                future = cube.transport.send_request(
                    dest_id=int(request["dest_id"]),
                    type_id=int(request["type_id"]),
                    payload=_b64decode(str(request.get("payload", ""))),
                    timeout=float(request.get("timeout", 2.0)),
                    expect_response=bool(request.get("expect_response", True)),
                )
                if request.get("wait", True) and future is not None:
                    future.result(timeout=float(request.get("timeout", 2.0)) + 1.0)
            return {"ok": True}

        if op == "snapshot":
            node = int(request["node"])
            return {"ok": True, "values": {str(k): v
                                           for k, v in cube.telemetry.snapshot(node).items()}}

        if op == "say":
            text = str(request.get("text", ""))
            if not text:
                raise ControlError("text is required", code="bad_request")
            self._audio_stopped.clear()
            with self.audio_lock:
                cube.say(text, voice=request.get("voice"),
                         speed=request.get("speed"),
                         volume=request.get("volume"))
            return {"ok": True}

        if op == "tone":
            self._audio_stopped.clear()
            with self.audio_lock:
                cube.speaker.tone(float(request.get("frequency", 440.0)),
                                  float(request.get("duration", 0.5)),
                                  amplitude=float(request.get("amplitude", 0.25)),
                                  function=str(request.get("function", "sine")))
            return {"ok": True}

        if op == "play":
            path = str(request.get("path", ""))
            if not path:
                raise ControlError("path is required", code="bad_request")
            self._audio_stopped.clear()
            with self.audio_lock:
                cube.speaker.play(path)
            return {"ok": True}

        if op == "stop_audio":
            self._audio_stopped.set()
            try:
                cube.speaker.stop()
            except Exception:  # noqa: BLE001 — best effort
                log.debug("stopping the speaker failed", exc_info=True)
            return {"ok": True}

        if op == "stats":
            telemetry = cube.telemetry
            return {"ok": True, "clients": self.client_count,
                    "ops": self._ops_served,
                    "uptime": round(time.time() - self._started, 1),
                    "telemetry": telemetry.stats() if hasattr(telemetry, "stats") else {},
                    "subscriptions": sorted(connection.subscriptions)}

        raise ControlError(f"unknown op {op!r}", code="no_op")

    def _module(self, request: Mapping[str, Any]):
        name = str(request.get("module", ""))
        try:
            return self.cube.module(name)
        except KeyError as exc:
            raise ControlError(str(exc), code="no_module") from None

    @staticmethod
    def _node_list(value: Any) -> list[int]:
        """Accept `[1, 2]`, `1` or `"1"` — `lumicube-do nodes=125` is handy."""
        if value is None or value == "":
            return []
        if isinstance(value, (int, str)):
            return [int(value)]
        return [int(node) for node in value]

    def _protected_in(self, node_ids: Iterable[int]) -> list[str]:
        """Which of these node ids are on the protected list (by name)."""
        if not self.protected_nodes:
            return []
        names = getattr(self.cube, "node_names", {}) or {}
        out = []
        for node_id in node_ids:
            name = str(names.get(node_id, "")).lower()
            if name in self.protected_nodes:
                out.append(name or str(node_id))
        return out


class ControlClient:
    """A line-JSON client for `ControlServer`.

    Requests are pipelined: `request_async` sends and returns a Future that
    the reader thread resolves when the reply arrives, so a streaming caller
    (the speaker's 286 chunks/s) can keep several in flight instead of
    paying a round trip per chunk. `on_event` is called from the reader
    thread, so it must not block for long.
    """

    def __init__(self, path: str | None = None, *, timeout: float = 5.0,
                 on_event: Any = None) -> None:
        self.path = resolve_socket_path(path)
        self.timeout = timeout
        self.info: dict[str, Any] = {}
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._on_event = on_event
        self._send_lock = threading.Lock()
        self._futures_lock = threading.Lock()
        self._futures: dict[int, Any] = {}
        self._next_id = 1
        self._closed = threading.Event()

    # ---------------- lifecycle ----------------

    def connect(self) -> "ControlClient":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError as exc:
            sock.close()
            raise ControlError(f"cannot connect to {self.path}: {exc}",
                               code="no_daemon") from None
        self._sock = sock
        self._reader = threading.Thread(target=self._reader_loop, daemon=True,
                                        name="pylumicube-control-reader")
        self._reader.start()
        self.info = self.request("hello")
        return self

    def close(self) -> None:
        self._closed.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._fail_all("control connection closed")

    def __enter__(self) -> "ControlClient":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed.is_set()

    # ---------------- requests ----------------

    def request_async(self, op: str, **fields: Any):
        """Send a request; the returned Future yields the raw reply dict."""
        from concurrent.futures import Future

        with self._send_lock:
            sock = self._sock
            if sock is None:
                raise ControlError("control connection is closed", code="closed")
            request_id = self._next_id
            self._next_id += 1
            future: Future = Future()
            with self._futures_lock:
                self._futures[request_id] = future
            line = json.dumps({"id": request_id, "op": op, **fields}).encode("utf-8")
            try:
                sock.sendall(line + b"\n")
            except OSError as exc:
                with self._futures_lock:
                    self._futures.pop(request_id, None)
                raise ControlError(f"send failed: {exc}", code="closed") from None
        return future

    def request(self, op: str, *, timeout: float | None = None, **fields: Any) -> dict:
        """Send a request and wait for it. Raises ControlError on failure."""
        future = self.request_async(op, **fields)
        reply = future.result(timeout=self.timeout if timeout is None else timeout)
        if not reply.get("ok"):
            raise ControlError(str(reply.get("error", "request failed")),
                               code=str(reply.get("code", "error")))
        return reply

    def _reader_loop(self) -> None:
        sock = self._sock
        buffer = b""
        try:
            while sock is not None:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        self._deliver(line)
        except OSError:
            pass
        finally:
            self._fail_all("control connection closed")

    def _deliver(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            log.warning("ignoring malformed control frame")
            return
        request_id = message.get("id")
        if request_id is not None:
            with self._futures_lock:
                future = self._futures.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if self._on_event is not None:
            try:
                self._on_event(message)
            except Exception:  # noqa: BLE001 — an event handler must not kill the reader
                log.exception("control event handler failed")

    def _fail_all(self, reason: str) -> None:
        with self._futures_lock:
            pending = list(self._futures.values())
            self._futures.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ControlError(reason, code="closed"))
