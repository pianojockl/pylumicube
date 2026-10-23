"""Module namespace + compat-shim tests (no hardware).

Covers the daemon-side grouping of fields into modules
(`pylumicube.modules`), the read-only telemetry module shim, the local
``pi`` pseudo-module, and the lazy wiring in `LumiCubeCompat`.
"""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from pylumicube.constants import (
    FIELD_TYPE_BOOLEAN,
    FIELD_TYPE_UINT,
    TYPE_PUBLISHED_FIELDS,
)
from pylumicube.metadata import FieldSpec
from pylumicube.modules import FieldModule, build_namespace
from pylumicube.compat.runtime import (
    LumiCubeCompat,
    PiModule,
    StubModule,
    TelemetryModuleShim,
    _LazyModule,
    build_globals,
)
from pylumicube.telemetry import TelemetryStore
from pylumicube.uavcan import Broadcast


class FakeTransport:
    def __init__(self) -> None:
        self.handlers: list = []
        self.requests: list = []

    def add_broadcast_handler(self, handler) -> None:
        self.handlers.append(handler)

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     timeout: float = 5.0, priority: int = 20) -> Future:
        self.requests.append((dest_id, type_id, payload))
        future: Future = Future()
        future.set_result(b"")
        return future

    def emit(self, source_id: int, payload: bytes) -> None:
        transfer = Broadcast(source_id=source_id, type_id=TYPE_PUBLISHED_FIELDS,
                             transfer_id=0, priority=20, payload=payload)
        for handler in list(self.handlers):
            handler(transfer)


# The schema the live `button_and_light_sensor` board reports (2026-10-23).
SENSOR_SCHEMA = {
    0: FieldSpec(0, 1, FIELD_TYPE_BOOLEAN, 1, "top_pressed", module="buttons"),
    1: FieldSpec(1, 1, FIELD_TYPE_UINT, 4, "top_pressed_count", module="buttons"),
    6: FieldSpec(6, 1, FIELD_TYPE_UINT, 4, "ambient_light", module="light_sensor"),
    12: FieldSpec(12, 1, FIELD_TYPE_BOOLEAN, 1, "within_proximity",
                  module="light_sensor"),
    60014: FieldSpec(60014, 1, FIELD_TYPE_UINT, 4, "rdp_level", system=True),
}

CUBE_SCHEMA = {
    256: FieldSpec(256, 1, FIELD_TYPE_UINT, 1, "brightness", module="display"),
    7412: FieldSpec(7412, 1, FIELD_TYPE_BOOLEAN, 1, "button_pressed",
                    module="buttons"),
}


def _store_with_sensor() -> tuple[TelemetryStore, FakeTransport]:
    transport = FakeTransport()
    store = TelemetryStore(transport)
    store.set_schema(124, SENSOR_SCHEMA)
    store.start()          # frames are decoded once armed, as the cube does
    return store, transport


def _publish_ambient(transport: FakeTransport, value: int) -> None:
    payload = bytes([0x11, 0x06, 0x01, 0x01]) + value.to_bytes(4, "little")
    transport.emit(124, payload)


# ----- namespace building -------------------------------------------------


def test_namespace_groups_fields_by_module_metadata() -> None:
    store, _transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "button_and_light_sensor"}, store)
    assert sorted(ns) == ["button_and_light_sensor", "buttons", "light_sensor"]
    assert ns["light_sensor"].field_names == ["ambient_light", "within_proximity"]
    assert ns["buttons"].field_names == ["top_pressed", "top_pressed_count"]


def test_namespace_falls_back_to_node_name_without_module_metadata() -> None:
    store, _transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "button_and_light_sensor"}, store)
    assert "rdp_level" in ns["button_and_light_sensor"].field_names


def test_cube_buttons_module_is_renamed_to_system_button() -> None:
    store, _transport = _store_with_sensor()
    ns = build_namespace({125: CUBE_SCHEMA}, {125: "cube"}, store)
    assert "system_button" in ns
    assert "buttons" not in ns
    assert ns["system_button"].field_names == ["button_pressed"]


def test_cube_and_sensor_button_modules_do_not_collide() -> None:
    store, _transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA, 125: CUBE_SCHEMA},
                         {124: "button_and_light_sensor", 125: "cube"}, store)
    # The sensor board keeps `buttons`; the cube's is `system_button`.
    assert ns["buttons"].refs["top_pressed"].node_id == 124
    assert ns["system_button"].refs["button_pressed"].node_id == 125


def test_duplicate_field_name_keeps_the_first_node() -> None:
    store, _transport = _store_with_sensor()
    other = {0: FieldSpec(0, 1, FIELD_TYPE_BOOLEAN, 1, "top_pressed", module="buttons")}
    ns = build_namespace({124: SENSOR_SCHEMA, 123: other},
                         {123: "other", 124: "button_and_light_sensor"}, store)
    assert ns["buttons"].refs["top_pressed"].node_id == 123


def test_field_refs_carry_node_and_key() -> None:
    store, _transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "sensor"}, store)
    ref = ns["light_sensor"].ref("ambient_light")
    assert (ref.node_id, ref.key, ref.name) == (124, 6, "ambient_light")


# ----- FieldModule --------------------------------------------------------


def test_field_module_reads_from_the_telemetry_cache() -> None:
    store, transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "sensor"}, store)
    module = ns["light_sensor"]
    assert module.get("ambient_light") is None
    assert not module.has_data()
    _publish_ambient(transport, 1712)
    assert module.get("ambient_light") == 1712
    assert module.has_data()
    assert module.snapshot() == {"ambient_light": 1712}
    assert module.ambient_light == 1712


def test_field_module_unknown_field_raises_attribute_error() -> None:
    store, _transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "sensor"}, store)
    with pytest.raises(AttributeError):
        ns["light_sensor"].no_such_field
    with pytest.raises(AttributeError):
        ns["light_sensor"].ref("no_such_field")


def test_field_module_wait_for() -> None:
    store, transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "sensor"}, store)
    _publish_ambient(transport, 42)
    assert ns["light_sensor"].wait_for("ambient_light", timeout=1.0) == 42


# ----- TelemetryModuleShim ------------------------------------------------


def _shim() -> tuple[TelemetryModuleShim, FakeTransport]:
    store, transport = _store_with_sensor()
    ns = build_namespace({124: SENSOR_SCHEMA}, {124: "sensor"}, store)
    return TelemetryModuleShim(ns["light_sensor"]), transport


def test_shim_raises_until_the_node_publishes_anything() -> None:
    """Upstream scripts wait for the sensor with a try/except loop."""
    shim, transport = _shim()
    with pytest.raises(AttributeError):
        shim.ambient_light
    _publish_ambient(transport, 100)
    assert shim.ambient_light == 100


def test_shim_unknown_field_raises_attribute_error() -> None:
    shim, _transport = _shim()
    with pytest.raises(AttributeError):
        shim.temperature


def test_shim_field_missing_from_a_live_node_returns_none() -> None:
    """Once the node is live, an unpublished field is None (not an error)."""
    shim, transport = _shim()
    _publish_ambient(transport, 100)
    assert shim.within_proximity is None


def test_shim_fields_are_read_only() -> None:
    shim, transport = _shim()
    _publish_ambient(transport, 100)
    with pytest.raises(AttributeError):
        shim.ambient_light = 5


def test_shim_exposes_field_names_and_wait_for() -> None:
    shim, transport = _shim()
    assert shim.field_names == ["ambient_light", "within_proximity"]
    _publish_ambient(transport, 7)
    assert shim.wait_for("ambient_light", timeout=1.0) == 7


# ----- PiModule -----------------------------------------------------------


def test_pi_cpu_temp_reads_millidegrees(tmp_path, monkeypatch) -> None:
    thermal = tmp_path / "temp"
    thermal.write_text("54321\n")
    monkeypatch.setattr(PiModule, "_THERMAL", str(thermal))
    assert PiModule().cpu_temp() == pytest.approx(54.321)


def test_pi_ram_and_disk_percent_are_sane(tmp_path) -> None:
    pi = PiModule()
    ram = pi.ram_percent_used()
    assert 0.0 <= ram <= 100.0
    disk = pi.disk_percent(str(tmp_path))
    assert 0.0 <= disk <= 100.0


def test_pi_cpu_percent_is_a_percentage() -> None:
    pi = PiModule()
    assert pi.cpu_percent() == 0.0          # first call has no baseline
    value = pi.cpu_percent()                # second call computes a delta
    assert 0.0 <= value <= 100.0


def test_pi_ip_address_is_a_string() -> None:
    assert isinstance(PiModule().ip_address(), str)


# ----- LumiCubeCompat laziness -------------------------------------------


class FakeCube:
    """Stands in for LumiCube: records whether telemetry got enabled."""

    def __init__(self) -> None:
        self.display = MagicMock()
        self.telemetry = None
        self.enable_calls = 0
        self.enable_kwargs: list[dict] = []
        store, _transport = _store_with_sensor()
        self._modules = build_namespace({124: SENSOR_SCHEMA}, {124: "sensor"}, store)

    def enable_telemetry(self, *args, **kwargs):
        self.enable_calls += 1
        self.enable_kwargs.append(kwargs)
        self.telemetry = MagicMock()
        return self.telemetry

    @property
    def modules(self):
        if self.telemetry is None:
            raise RuntimeError("telemetry not enabled")
        return self._modules


def test_compat_construction_does_not_enable_telemetry() -> None:
    cube = FakeCube()
    compat = LumiCubeCompat(cube)
    assert cube.enable_calls == 0
    assert isinstance(compat.screen, StubModule)
    assert isinstance(compat.speaker, StubModule)
    assert isinstance(compat.pi, PiModule)


def test_compat_enables_telemetry_on_first_sensor_access() -> None:
    cube = FakeCube()
    compat = LumiCubeCompat(cube)
    module = compat.light_sensor
    assert cube.enable_calls == 1
    assert isinstance(module, TelemetryModuleShim)
    # Second access reuses the resolved module.
    assert compat.light_sensor is module
    assert cube.enable_calls == 1


def test_build_globals_module_handles_are_lazy() -> None:
    cube = FakeCube()
    ns = build_globals(LumiCubeCompat(cube))
    assert cube.enable_calls == 0
    for name in ("buttons", "light_sensor", "env_sensor", "imu"):
        assert isinstance(ns[name], _LazyModule)
    # Touching one resolves it (and switches telemetry on). The sensor has
    # not published anything yet, so the read raises — which is exactly
    # what the upstream `try/except` wait loops expect.
    with pytest.raises(AttributeError):
        ns["light_sensor"].ambient_light
    assert cube.enable_calls == 1
    # `display`, `speaker`, `pi` are eager objects, not lazy handles.
    assert not isinstance(ns["display"], _LazyModule)
    assert isinstance(ns["pi"], PiModule)


def test_telemetry_module_subscription_excludes_the_cube_board() -> None:
    """Speaker throughput needs the cube board walked but not subscribed."""
    cube = FakeCube()
    compat = LumiCubeCompat(cube)
    with pytest.warns(RuntimeWarning):
        compat.speaker                      # absent on FakeCube -> stub
    assert cube.enable_kwargs[-1].get("subscribe_except") == ("cube",)


def test_lazy_module_forwards_reads_and_writes() -> None:
    cube = FakeCube()
    compat = LumiCubeCompat(cube)
    lazy = compat.lazy("display")
    assert "lazy" in repr(lazy)
    lazy.set_all(0)                       # forwarded to the DisplayShim
    assert "resolved" in repr(lazy)
    assert cube.enable_calls == 0         # display needs no telemetry


def test_driver_module_falls_back_to_a_stub_when_absent() -> None:
    """A cube without a `speaker` module degrades to the warn+no-op stub."""
    cube = FakeCube()                     # has no .speaker attribute
    compat = LumiCubeCompat(cube)
    with pytest.warns(RuntimeWarning):
        module = compat.speaker
    assert isinstance(module, StubModule)
    assert cube.enable_calls == 1         # the driver path needs telemetry



