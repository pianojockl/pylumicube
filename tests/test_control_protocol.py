"""Control protocol and client bindings (no hardware, no broker).

A fake cube stands in for the LumiCube: the server's ops are driven through a
real `ControlClient` over a real Unix socket, so the wire format, the pushed
values and the error codes are all exercised end to end.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from pylumicube.client import CubeClient, connect_or_open, daemon_available
from pylumicube.control import (ControlClient, ControlError, ControlServer,
                                decode_schemas, encode_schemas, socket_is_live)
from pylumicube.metadata import FieldSpec


# ----- fakes --------------------------------------------------------------


class FakeTelemetry:
    def __init__(self) -> None:
        self.values: dict[int, dict[int, int]] = {124: {2049: 1200, 2048: 640}}
        self.subscribe_calls: list[list[int] | None] = []
        self.subscribed: list[int] = []

    def subscribe(self, node_ids=None, *, timeout: float = 2.0) -> list[int]:
        self.subscribe_calls.append(list(node_ids) if node_ids else None)
        self.subscribed = list(node_ids or [124])
        return self.subscribed

    def snapshot(self, node_id: int) -> dict[int, int]:
        return dict(self.values.get(node_id, {}))

    def stats(self) -> dict[str, object]:
        return {"frames": 7}


class FakeModule:
    def __init__(self, name: str, fields: dict[str, int],
                 values: dict[str, int] | None = None) -> None:
        self.name = name
        self.refs = {field: SimpleNamespace(node_id=124, key=key)
                     for field, key in fields.items()}
        self._values = dict(values or {})

    def get(self, field: str, default: int | None = None) -> int | None:
        return self._values.get(field, default)

    def snapshot(self) -> dict[str, int]:
        return dict(self._values)


class FakeSpeaker:
    def __init__(self) -> None:
        self.volume = 100
        self.tones: list[tuple] = []
        self.played: list[str] = []
        self.stops = 0

    def tone(self, frequency: float, duration: float, amplitude: float = 0.25,
             function: str = "sine") -> None:
        self.tones.append((frequency, duration, amplitude, function))

    def play(self, path: str) -> None:
        self.played.append(path)

    def stop(self) -> None:
        self.stops += 1

    def note_volume(self, value: int) -> None:
        self.volume = int(value)


class FakeTransport:
    def __init__(self) -> None:
        self.frames: list[tuple[int, int, bytes, bool]] = []

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     priority: int = 20, timeout: float = 5.0,
                     expect_response: bool = True) -> Future:
        self.frames.append((dest_id, type_id, payload, expect_response))
        future: Future = Future()
        future.set_result(b"")
        return future


class FakeCube:
    """Just enough of the LumiCube API for the server's ops."""

    def __init__(self) -> None:
        self.telemetry = FakeTelemetry()
        self.transport = FakeTransport()
        self.speaker = FakeSpeaker()
        self.said: list[str] = []
        self.writes: list[tuple] = []
        self.runs: list[tuple] = []
        self.walks = 0
        self.say_delay = 0.0
        self._modules = {
            "light_sensor": FakeModule("light_sensor",
                                       {"ambient_light": 2049, "red": 2048},
                                       {"ambient_light": 1200, "red": 640}),
            "speaker": FakeModule("speaker", {"volume": 128, "data": 129}),
        }

    # LumiCube surface the ops use
    def say(self, text: str, *, voice=None, speed=None, volume=None) -> None:
        if self.say_delay:
            time.sleep(self.say_delay)
        self.said.append(text)

    @property
    def node_names(self) -> dict[int, str]:
        return {124: "button_and_light_sensor", 125: "cube"}

    def node_id_for_name(self, name: str) -> int | None:
        return {"cube": 125}.get(name)

    @property
    def schemas(self) -> dict[int, dict[int, FieldSpec]]:
        return {124: {2049: FieldSpec(floor=2049, span=1, type=2, size=4,
                                      name="ambient_light", module="light_sensor")}}

    @property
    def modules(self) -> dict[str, FakeModule]:
        return self._modules

    def module(self, name: str) -> FakeModule:
        try:
            return self._modules[name]
        except KeyError:
            raise KeyError(f"no module {name!r}") from None

    def write_named(self, module: str, values, *, timeout: float = 2.0,
                    await_response: bool = True, wait: bool = True) -> None:
        self.writes.append((module, dict(values), await_response, wait))

    def write_run(self, module: str, field: str, samples: bytes, *,
                  timeout: float = 2.0, await_response: bool = True,
                  wait: bool = True) -> None:
        self.runs.append((module, field, bytes(samples)))

    def walk_all_schemas(self, *, budget=None) -> dict[int, int]:
        self.walks += 1
        return {124: 1}


@pytest.fixture()
def server(tmp_path):
    """A running ControlServer on a temporary socket, plus its fake cube."""
    cube = FakeCube()
    control = ControlServer(cube, str(tmp_path / "control.sock"))
    control.start()
    try:
        yield control, cube
    finally:
        control.stop()


# ----- protocol basics ----------------------------------------------------


def test_ping_round_trip(server) -> None:
    control, _cube = server
    with ControlClient(control.path) as client:
        assert client.request("ping")["version"] == 1


def test_hello_describes_the_cube(server) -> None:
    control, _cube = server
    with CubeClient(control.path) as client:
        assert client.info["cube_node_id"] == 125
        assert client.node_names == {124: "button_and_light_sensor", 125: "cube"}
        assert sorted(client.info["modules"]) == ["light_sensor", "speaker"]
        # The schemas survive the JSON round trip and build a namespace...
        assert "ambient_light" in client.modules["light_sensor"].refs
        # ...and a value can be read straight from the daemon's cache.
        assert client.read_named("light_sensor", "ambient_light") == 1200


def test_connected_goes_false_when_the_daemon_disappears(server) -> None:
    """`connected` must not keep claiming life after the peer has gone."""
    control, _cube = server
    client = ControlClient(control.path).connect()
    try:
        assert client.connected
        control.stop()
        deadline = time.time() + 5.0
        while client.connected and time.time() < deadline:
            time.sleep(0.05)
        assert not client.connected, "a dead socket still reported as connected"
    finally:
        client.close()


def test_client_reconnects_after_the_daemon_restarts(server) -> None:
    """A daemon restart must not leave a long-running client a zombie.

    The clock lives across daemon restarts: without reconnection every
    request fails with `Broken pipe` and the display silently stops updating.
    """
    control, cube = server
    with CubeClient(control.path) as client:
        assert client.read_named("light_sensor", "ambient_light") == 1200
        assert client.enable_telemetry([124]).subscribed_nodes == {124}

        control.stop()                       # the daemon goes away...
        with pytest.raises(ControlError):
            client.read_named("light_sensor", "ambient_light")

        restarted = ControlServer(cube, control.path)     # ...and comes back
        restarted.start()
        try:
            client.reconnect()
            assert client.info["cube_node_id"] == 125
            assert "ambient_light" in client.modules["light_sensor"].refs
            assert client.read_named("light_sensor", "ambient_light") == 1200
            assert client.telemetry.subscribed_nodes == {124}, \
                "the subscription was not re-issued"
        finally:
            restarted.stop()


def test_unknown_op_and_module_have_codes(server) -> None:
    control, _cube = server
    with ControlClient(control.path) as client:
        with pytest.raises(ControlError) as op_error:
            client.request("nonsense")
        assert op_error.value.code == "no_op"
        with pytest.raises(ControlError) as module_error:
            client.request("read", module="nope", field="x")
        assert module_error.value.code == "no_module"
        with pytest.raises(ControlError) as field_error:
            client.request("read", module="light_sensor", field="nope")
        assert field_error.value.code == "no_field"


def test_malformed_line_does_not_kill_the_connection(server) -> None:
    control, _cube = server
    import socket as _socket

    with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as raw:
        raw.connect(control.path)
        raw.sendall(b"this is not json\n")
        assert b"bad_request" in raw.recv(4096)
        raw.sendall(b'{"id": 9, "op": "ping"}\n')
        assert b'"ok": true' in raw.recv(4096)


# ----- reads, writes and frames -------------------------------------------


def test_read_returns_the_cached_value(server) -> None:
    control, _cube = server
    with ControlClient(control.path) as client:
        assert client.request("read", module="light_sensor",
                              field="ambient_light")["value"] == 1200
        assert client.request("read_all",
                              module="light_sensor")["values"]["red"] == 640


def test_write_op_forwards_flags(server) -> None:
    control, cube = server
    with CubeClient(control.path) as client:
        client.write_named("speaker", {"volume": 80})
        pending = client.write_named("screen", {"rectangle_x": 1},
                                     await_response=False, wait=False)
        pending.result(timeout=2.0)          # the daemon has dispatched it
    assert cube.writes[0] == ("speaker", {"volume": 80}, True, True)
    assert cube.writes[1] == ("screen", {"rectangle_x": 1}, False, False)
    # ...and the daemon mirrors a client's volume so its own view stays true.
    assert cube.speaker.volume == 80


def test_run_op_carries_samples(server) -> None:
    control, cube = server
    with CubeClient(control.path) as client:
        client.write_run("speaker", "data", b"\x01\x02\x03\x04")
    assert cube.runs == [("speaker", "data", b"\x01\x02\x03\x04")]


def test_frame_op_forwards_raw_frames(server) -> None:
    control, cube = server
    with CubeClient(control.path) as client:
        future = client._transport.send_request(  # noqa: SLF001
            125, 21, b"\xaa\xbb", expect_response=False)
        assert future.result(timeout=2.0) == b""
    assert cube.transport.frames == [(125, 21, b"\xaa\xbb", False)]


def test_walk_op_refreshes_the_client_schemas(server) -> None:
    control, cube = server
    with CubeClient(control.path) as client:
        counts = client.walk_all_schemas()
    assert counts == {124: 1}
    assert cube.walks == 1


# ----- telemetry push -----------------------------------------------------


def test_values_are_pushed_to_subscribers(server) -> None:
    control, _cube = server
    with CubeClient(control.path) as client:
        telemetry = client.enable_telemetry(node_names=("button_and_light_sensor",))
        assert telemetry.subscribed_nodes == {124}
        control.broadcast_values(124, {2049: 1234})
        assert telemetry.wait_for(124, 2049, timeout=2.0) == 1234
        assert telemetry.snapshot(124)[2049] == 1234
        assert telemetry.last_update(124) is not None
        assert telemetry.stats()["frames"] >= 1


def test_unsubscribed_clients_still_receive_broadcasts(server) -> None:
    """A client that asked for nothing gets everything (the daemon decides)."""
    control, _cube = server
    with CubeClient(control.path) as client:
        control.broadcast_values(124, {2048: 111})
        assert client.telemetry.wait_for(124, 2048, timeout=2.0) == 111


def test_wait_for_times_out_with_a_default(server) -> None:
    control, _cube = server
    with CubeClient(control.path) as client:
        assert client.telemetry.wait_for(124, 9999, timeout=0.1, default=-1) == -1


# ----- audio ---------------------------------------------------------------


def test_say_tone_play_and_stop(server) -> None:
    control, cube = server
    with CubeClient(control.path) as client:
        client.say("hello", volume=80)
        client.tone(880, 0.2)
        client.play("/tmp/clip.wav")
        client.stop_audio()
    assert cube.said == ["hello"]
    assert cube.speaker.tones == [(880.0, 0.2, 0.25, "sine")]
    assert cube.speaker.played == ["/tmp/clip.wav"]
    assert cube.speaker.stops == 1


def test_audio_ops_serialise_but_display_writes_do_not_wait(server) -> None:
    """A three-second utterance must not block a display frame."""
    control, cube = server
    cube.say_delay = 0.4
    started = threading.Event()

    def slow_say() -> None:
        with CubeClient(control.path) as client:
            started.set()
            client.say("a long sentence")

    thread = threading.Thread(target=slow_say, daemon=True)
    thread.start()
    assert started.wait(timeout=2.0)
    time.sleep(0.1)                        # let the say op take the audio lock

    with CubeClient(control.path) as other:
        began = time.monotonic()
        other.write_named("screen", {"text_x": 1})
        display_elapsed = time.monotonic() - began
        began = time.monotonic()
        other.tone(440, 0.01)
        tone_elapsed = time.monotonic() - began
    thread.join(timeout=5.0)

    assert display_elapsed < 0.2, "the display write waited for the utterance"
    assert tone_elapsed >= 0.15, "two audio ops overlapped"


def test_stop_audio_drops_a_client_stream(server) -> None:
    control, cube = server
    with CubeClient(control.path) as client:
        client.write_run("speaker", "data", b"\x00\x01")     # a stream starts
        client.stop_audio()
        client.write_run("speaker", "data", b"\x02\x03")     # ...and is dropped
    assert len(cube.runs) == 1


# ----- sockets and the daemon fallback ------------------------------------


def test_socket_is_live_and_stale_sockets_are_replaced(tmp_path) -> None:
    path = str(tmp_path / "control.sock")
    assert not socket_is_live(path)
    with ControlServer(FakeCube(), path):
        assert socket_is_live(path)
    assert not socket_is_live(path)          # cleaned up on stop


def test_second_server_refuses_a_live_socket(tmp_path) -> None:
    path = str(tmp_path / "control.sock")
    with ControlServer(FakeCube(), path):
        with pytest.raises(ControlError) as error:
            ControlServer(FakeCube(), path).start()
        assert error.value.code == "in_use"


def test_stale_socket_file_is_unlinked(tmp_path) -> None:
    import socket as _socket

    path = tmp_path / "control.sock"
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()                             # a file with nobody listening
    control = ControlServer(FakeCube(), str(path))
    control.start()
    try:
        assert socket_is_live(str(path))
    finally:
        control.stop()


def test_daemon_available_follows_the_socket(tmp_path, monkeypatch) -> None:
    path = str(tmp_path / "control.sock")
    monkeypatch.setenv("PYLUMICUBE_SOCKET", path)
    assert not daemon_available()
    with ControlServer(FakeCube(), path):
        assert daemon_available()
        with connect_or_open() as cube:
            assert isinstance(cube, CubeClient)
    assert not daemon_available()


def test_connect_raises_a_clear_error_without_a_daemon(tmp_path) -> None:
    with pytest.raises(ControlError) as error:
        CubeClient(str(tmp_path / "nothing.sock"))
    assert error.value.code == "no_daemon"


def test_schema_encoding_round_trip() -> None:
    specs = {124: {2049: FieldSpec(floor=2049, span=4, type=2, size=4,
                                   name="ambient_light", module="light_sensor",
                                   units="lux", min_value=0, max_value=65535)}}
    decoded = decode_schemas(encode_schemas(specs))
    spec = decoded[124][2049]
    assert (spec.floor, spec.span, spec.type, spec.size) == (2049, 4, 2, 4)
    assert (spec.name, spec.module, spec.units) == ("ambient_light",
                                                    "light_sensor", "lux")
    assert (spec.min_value, spec.max_value) == (0, 65535)
