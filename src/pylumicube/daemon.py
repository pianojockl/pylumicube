"""The cube daemon: one owner of the serial port, many clients.

`lumicube-daemon` opens the LumiCube, discovers every schema, subscribes to
the boards that do not compete with streaming writes, and serves the control
protocol (`pylumicube.control`) on a Unix socket. The clock, the compat
runner, ad-hoc scripts and — through the MQTT bridge — Home Assistant all
connect to it instead of fighting over `/dev/ttyAMA0`.

Keeping the daemon and the clock in separate processes is deliberate: the
clock can restart without dropping the cube link, and Home Assistant keeps
working while it does.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .constants import SERIAL_DEVICE
from .control import ControlServer, resolve_socket_path
from .node import LumiCube
from .telemetry import DEFAULT_CADENCE

log = logging.getLogger(__name__)

#: Button modules and the actions their `*_pressed_count` fields represent.
BUTTON_SOURCES = (("buttons", ("top", "middle", "bottom")),
                  ("system_button", ("button",)))

#: How often the CPU temperature is published (HA likes a slow sensor).
TEMPERATURE_INTERVAL = 30.0


class Daemon:
    """Own the cube, serve the control protocol, optionally bridge to MQTT."""

    def __init__(self, port: str = SERIAL_DEVICE, *, socket_path: str | None = None,
                 log_ops: bool = False, subscribe_except: tuple[str, ...] = ("cube",),
                 telemetry_cadence: float = DEFAULT_CADENCE,
                 mqtt: Any = None, temperature_interval: float = TEMPERATURE_INTERVAL,
                 clear_on_stop: bool = False,
                 allow_cube_telemetry: bool = False) -> None:
        self.port = port
        self.socket_path = resolve_socket_path(socket_path)
        self.mqtt_config = mqtt
        self.temperature_interval = temperature_interval
        self.clear_on_stop = clear_on_stop
        self.allow_cube_telemetry = bool(allow_cube_telemetry)
        self._button_counts: dict[str, int] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.started_at = time.time()
        self._frames_seen = 0

        self.cube = LumiCube(port, telemetry=True,
                             telemetry_cadence=telemetry_cadence,
                             subscribe_except=subscribe_except,
                             on_update=self._on_telemetry)
        self.server = ControlServer(self.cube, self.socket_path, log_ops=log_ops,
                                    protected_nodes=("cube",),
                                    allow_protected=self.allow_cube_telemetry)
        self.bridge = None

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        self.cube.start()
        counts = self.cube.walk_all_schemas()
        log.info("schemas: %s", ", ".join(
            f"node {node}: {count}" for node, count in sorted(counts.items())))
        self.server.start()
        if self.mqtt_config:
            from .mqtt import MqttBridge
            self.bridge = MqttBridge(self, **self.mqtt_config)
            self.bridge.start()
        self._spawn(self._temperature_loop, "pylumicube-temperature")
        log.info("daemon ready: cube on %s, control socket %s, %d modules",
                 self.port, self.socket_path, len(self.cube.modules))

    def _spawn(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self.bridge is not None:
            self.bridge.stop()
        self.server.stop()
        if self.clear_on_stop:
            try:
                self.cube.display.set_all(0x000000)
                self.cube.display.flush(timeout=2.0)
            except Exception:  # noqa: BLE001 — shutdown must not raise
                log.debug("could not blank the display on shutdown", exc_info=True)
        self.cube.stop()

    def __enter__(self) -> "Daemon":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def run_forever(self, poll: float = 0.5) -> int:
        """Block until stopped (`request_stop`, SIGTERM or SIGINT)."""
        while not self._stop.wait(poll):
            pass
        return 0

    def request_stop(self) -> None:
        self._stop.set()

    # ---------------- telemetry ----------------

    def _on_telemetry(self, node_id: int, values: dict[int, int]) -> None:
        """Fan published values out to clients, and watch the buttons."""
        self._frames_seen += 1
        try:
            self.server.broadcast_values(node_id, values)
        except Exception:  # noqa: BLE001 — the link must never die for this
            log.exception("broadcasting telemetry failed")
        try:
            self._check_buttons()
        except Exception:  # noqa: BLE001
            log.exception("button check failed")

    def _check_buttons(self) -> None:
        """Detect presses from the monotonic counters and announce them.

        The counters only go up, so a press cannot be missed between two
        telemetry frames — unlike the `*_pressed` level flags, which are
        only high while the button is held.
        """
        if self.cube.telemetry is None:
            return                          # still starting up
        for module_name, actions in BUTTON_SOURCES:
            module = self.cube.modules.get(module_name)
            if module is None:
                continue
            for action in actions:
                field = f"{action}_pressed_count"
                if field not in module.refs:
                    continue
                count = module.get(field)
                if count is None:
                    continue
                key = f"{module_name}.{action}"
                previous = self._button_counts.get(key)
                self._button_counts[key] = count
                if previous is not None and count > previous:
                    self._on_button(module_name, action, count)

    def _on_button(self, module_name: str, action: str, count: int) -> None:
        log.info("button %s.%s pressed (%d total)", module_name, action, count)
        self.server.broadcast({"event": "button", "module": module_name,
                               "button": action, "count": count})
        if self.bridge is not None:
            self.bridge.button_pressed(module_name, action)

    def _temperature_loop(self) -> None:
        """Publish the CPU temperature for MQTT clients, slowly."""
        while not self._stop.wait(self.temperature_interval):
            if self.bridge is None:
                continue
            value = read_cpu_temperature()
            if value is not None:
                try:
                    self.bridge.publish_temperature(value)
                except Exception:  # noqa: BLE001 — a broker hiccup is not fatal
                    log.debug("publishing the temperature failed", exc_info=True)


def read_cpu_temperature() -> float | None:
    """The Pi's CPU temperature in °C, or None when unavailable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as handle:
            return int(handle.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def build_parser() -> "argparse.ArgumentParser":
    import argparse

    parser = argparse.ArgumentParser(
        prog="lumicube-daemon",
        description="Own the LumiCube and serve the control protocol.")
    parser.add_argument("--port", default=os.environ.get("PYLUMICUBE_PORT", SERIAL_DEVICE))
    parser.add_argument("--socket", default=None,
                        help="control socket path (default $XDG_RUNTIME_DIR/pylumicube.sock)")
    parser.add_argument("--log-ops", action="store_true",
                        help="log every control operation")
    parser.add_argument("--clear-on-stop", action="store_true",
                        help="blank the LED matrix when shutting down")
    parser.add_argument("--allow-cube-telemetry", action="store_true",
                        help="let clients subscribe to the cube board (needed for "
                             "the microphone; it floods the link, so it is off by "
                             "default)")
    parser.add_argument("--mqtt-host", default=os.environ.get("PYLUMICUBE_MQTT_HOST"),
                        help="MQTT broker for the Home Assistant bridge")
    parser.add_argument("--mqtt-port", type=int,
                        default=int(os.environ.get("PYLUMICUBE_MQTT_PORT", "1883")))
    parser.add_argument("--mqtt-username",
                        default=os.environ.get("PYLUMICUBE_MQTT_USERNAME"))
    parser.add_argument("--mqtt-password",
                        default=os.environ.get("PYLUMICUBE_MQTT_PASSWORD"))
    parser.add_argument("--mqtt-prefix",
                        default=os.environ.get("PYLUMICUBE_MQTT_PREFIX", "lumicube"))
    parser.add_argument("--mqtt-discovery-prefix",
                        default=os.environ.get("PYLUMICUBE_MQTT_DISCOVERY_PREFIX",
                                               "homeassistant"))
    parser.add_argument("--no-mqtt-discovery", action="store_true",
                        help="publish state but no Home Assistant discovery")
    parser.add_argument("--mqtt-state-interval", type=float, default=1.0,
                        help="seconds between state publishes (default 1)")
    parser.add_argument("--no-mqtt", action="store_true",
                        help="run without the MQTT bridge")
    parser.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    import signal

    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    mqtt = None
    if args.mqtt_host and not args.no_mqtt:
        mqtt = {"host": args.mqtt_host, "port": args.mqtt_port,
                "username": args.mqtt_username, "password": args.mqtt_password,
                "prefix": args.mqtt_prefix, "discovery": not args.no_mqtt_discovery,
                "discovery_prefix": args.mqtt_discovery_prefix,
                "state_interval": args.mqtt_state_interval}
    elif not args.no_mqtt:
        log.info("no --mqtt-host given; running without the MQTT bridge")

    daemon = Daemon(args.port, socket_path=args.socket, log_ops=args.log_ops,
                    mqtt=mqtt, clear_on_stop=args.clear_on_stop,
                    allow_cube_telemetry=args.allow_cube_telemetry)

    def _stop(_signum: int, _frame: Any) -> None:
        log.info("stopping")
        daemon.request_stop()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    try:
        daemon.start()
    except Exception:
        log.exception("the daemon could not start")
        daemon.stop()
        return 1
    try:
        return daemon.run_forever()
    finally:
        daemon.stop()


if __name__ == "__main__":
    raise SystemExit(main())
