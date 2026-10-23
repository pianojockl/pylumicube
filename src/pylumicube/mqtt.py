"""MQTT bridge: Home Assistant (and anything else) talks to the cube.

Commands arrive as plain payloads where that makes sense, so an automation
can publish the sentence it wants spoken:

    lumicube/speaker/say        "The washing machine has finished"
    lumicube/speaker/tone       "440" or {"frequency": 440, "duration": 0.5}
    lumicube/speaker/play       a WAV path on the cube
    lumicube/speaker/stop       anything
    lumicube/speaker/volume/set 0..200
    lumicube/screen/text        "hello" or {"text": ..., "size": 2, "colour": ...}
    lumicube/screen/image       an image path on the cube
    lumicube/screen/clear       a colour (hex like "#000000" or a number)
    lumicube/display/fill       a colour
    lumicube/display/brightness/set  0..255

Note the `/set` suffix on the two settable values: their *state* is published
on the bare topic (`lumicube/speaker/volume`), and a bridge that subscribed
to both would hear its own state as a command — which is exactly the loop
that had Home Assistant's volume slider bouncing between 0, 5 and 10.

State goes the other way (retained), with MQTT Discovery payloads so Home
Assistant creates the entities by itself — sensors for the light sensor and
CPU temperature, a number for the volume, a text box that speaks, and event
entities for the cube's buttons.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


def parse_colour(payload: str) -> int:
    """Colours on the command topics: "#rrggbb", "rrggbb" or an int."""
    text = str(payload).strip().strip('"')
    if text.startswith("#"):
        text = text[1:]
    if text.lower().startswith("0x"):
        return int(text, 16) & 0xFFFFFF
    if len(text) == 6 and all(char in "0123456789abcdefABCDEF" for char in text):
        return int(text, 16)
    return int(text) & 0xFFFFFF


def parse_payload(payload: str) -> Any:
    """A JSON object, a bare number, or the raw string."""
    text = payload.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            log.warning("ignoring malformed JSON command: %r", text)
            return {}
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


class MqttBridge:
    """Bridges the daemon to an MQTT broker (and so to Home Assistant)."""

    def __init__(self, daemon: Any, *, host: str = "localhost", port: int = 1883,
                 username: str | None = None, password: str | None = None,
                 prefix: str = "lumicube", discovery: bool = True,
                 discovery_prefix: str = "homeassistant",
                 client_id: str = "pylumicube", state_interval: float = 1.0,
                 keepalive: int = 60) -> None:
        self.daemon = daemon
        self.cube = daemon.cube
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.prefix = prefix.strip("/")
        self.discovery = bool(discovery)
        self.discovery_prefix = discovery_prefix.strip("/")
        self.client_id = client_id
        self.state_interval = float(state_interval)
        self.keepalive = int(keepalive)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._client: Any = None
        self._connected = threading.Event()
        self._last_state: dict[str, Any] = {}
        self._last_publish: dict[str, float] = {}

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        from paho.mqtt import client as mqtt_client   # noqa: PLC0415 — optional dep

        if hasattr(mqtt_client, "CallbackAPIVersion"):     # paho-mqtt >= 2.0
            client = mqtt_client.Client(
                mqtt_client.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        else:                                              # paho-mqtt 1.x
            client = mqtt_client.Client(client_id=self.client_id)
        if self.username:
            client.username_pw_set(self.username, self.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        self._client = client
        # The last will tells Home Assistant when the cube goes away.
        client.will_set(self._topic("status"), "offline", retain=True)
        client.connect_async(self.host, self.port, self.keepalive)
        client.loop_start()
        log.info("MQTT bridge connecting to %s:%s as %s (prefix %s)",
                 self.host, self.port, self.client_id, self.prefix)
        thread = threading.Thread(target=self._state_loop, daemon=True,
                                  name="pylumicube-mqtt-state")
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                client.publish(self._topic("status"), "offline", retain=True)
                time.sleep(0.05)
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001 — shutdown must not raise
                log.debug("MQTT shutdown failed", exc_info=True)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()

    def _topic(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}"

    # ---------------- paho callbacks ----------------

    def _command_topics(self) -> tuple[str, ...]:
        """The topics we listen on — commands only, never our own state.

        Subscribing to `speaker/#` used to include `speaker/volume`, the
        topic we *publish* the volume on, so the bridge heard its own state
        and treated it as a command. Home Assistant's number entity then
        answered that, and the pair sat in a loop: the broker logged 10,470
        state changes on `number.lumicube_speaker_volume` in a few minutes,
        with the volume cycling 0/5/10. Commands now live under `/set`, which
        is the convention Home Assistant's MQTT entities expect anyway.
        """
        return tuple(self._topic(suffix) for suffix in (
            "speaker/say", "speaker/tone", "speaker/play", "speaker/stop",
            "speaker/volume/set", "screen/text", "screen/image", "screen/clear",
            "display/fill", "display/brightness/set",
        ))

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any,
                    properties: Any = None) -> None:
        code = getattr(reason_code, "value", reason_code)
        if code not in (0, None):
            log.error("MQTT connection refused: %s", reason_code)
            return
        self._connected.set()
        log.info("MQTT connected to %s:%s", self.host, self.port)
        client.publish(self._topic("status"), "online", retain=True)
        for topic in self._command_topics():
            client.subscribe(topic)
        self._publish_discovery(client)
        self._publish_all_state(client)

    def _on_disconnect(self, *args: Any) -> None:
        self._connected.clear()
        log.warning("MQTT disconnected")

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        topic = getattr(message, "topic", "")
        payload = getattr(message, "payload", b"")
        try:
            text = payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — defensive: never kill the loop
            return
        suffix = (topic[len(self.prefix) + 1:] if topic.startswith(self.prefix)
                  else topic)
        try:
            self._handle_command(suffix, text)
        except Exception:  # noqa: BLE001 — one bad command must not stop the bridge
            log.exception("MQTT command %r failed", suffix)

    # ---------------- commands (HA -> cube) ----------------

    def _handle_command(self, suffix: str, payload: str) -> None:
        value = parse_payload(payload)
        cube = self.cube

        if suffix in ("speaker/say", "say"):
            text = value.get("text") if isinstance(value, dict) else str(value)
            if not text:
                return
            options = value if isinstance(value, dict) else {}
            log.info("MQTT: say %r", text)
            cube.say(str(text), voice=options.get("voice"),
                     volume=options.get("volume"))
            return
        if suffix in ("speaker/tone", "tone"):
            options = value if isinstance(value, dict) else {"frequency": value}
            log.info("MQTT: tone %s", options)
            cube.speaker.tone(
                float(options.get("frequency", 440)),
                float(options.get("duration", 0.5)),
                amplitude=float(options.get("amplitude", 0.25)),
                function=str(options.get("waveform") or options.get("function")
                             or "sine"))
            return

        if suffix in ("speaker/play", "play"):
            path = value.get("path") if isinstance(value, dict) else str(value)
            if path:
                log.info("MQTT: play %r", path)
                cube.speaker.play(str(path))
            return

        if suffix in ("speaker/stop", "stop"):
            cube.speaker.stop()
            return

        if suffix in ("speaker/volume/set", "speaker/volume"):
            volume = int(value)
            if volume == self._last_state.get(self._topic("speaker/volume")):
                return                      # our own state echoing back
            log.info("MQTT: volume %d", volume)
            cube.speaker.volume = volume
            self._publish_state(self._topic("speaker/volume"), cube.speaker.volume)
            return

        if suffix == "screen/text":
            options = value if isinstance(value, dict) else {"text": str(value)}
            text = str(options.get("text", ""))
            if not text:
                return
            log.info("MQTT: screen text %r", text)
            cube.screen.write_text(int(options.get("x", 10)),
                                   int(options.get("y", 10)), text,
                                   int(options.get("size", 2)),
                                   parse_colour(str(options.get("colour", "#ffffff"))))
            return

        if suffix == "screen/image":
            path = value.get("path") if isinstance(value, dict) else str(value)
            if path:
                log.info("MQTT: screen image %r", path)
                cube.screen.draw_image(str(path))
            return

        if suffix == "screen/clear":
            cube.screen.clear(parse_colour(str(payload or "#000000")))
            return

        if suffix == "display/fill":
            cube.display.fill(parse_colour(str(payload)))
            return

        if suffix in ("display/brightness/set", "display/brightness"):
            level = int(value)
            if level == self._last_state.get(self._topic("display/brightness")):
                return                      # our own state echoing back
            log.info("MQTT: display brightness %d", level)
            cube.display.set_brightness(max(0, min(255, level)))
            self._publish_state(self._topic("display/brightness"), level)
            return

        log.debug("ignoring unknown MQTT command %r", suffix)

    # ---------------- state (cube -> HA) ----------------

    def button_pressed(self, module_name: str, action: str) -> None:
        """Announce a press on an event topic (deliberately not retained)."""
        self._publish(self._topic(f"buttons/{action}/event"), "press", retain=False)

    def publish_temperature(self, value: float) -> None:
        self._publish_state(self._topic("pi/cpu_temp"), round(float(value), 1))

    def _state_loop(self) -> None:
        """Publish the slow-changing state on a timer."""
        while not self._stop.wait(self.state_interval):
            if not self._connected.is_set():
                continue
            try:
                self._publish_all_state(self._client)
            except Exception:  # noqa: BLE001 — telemetry may not be up yet
                log.debug("publishing state failed", exc_info=True)

    def _publish_all_state(self, client: Any) -> None:
        sensor = self.cube.modules.get("light_sensor")
        if sensor is not None:
            for field in ("ambient_light", "red", "green", "blue",
                          "within_proximity"):
                value = sensor.get(field)
                if value is not None:
                    self._publish_state(self._topic(f"light_sensor/{field}"),
                                        int(value))
        volume = getattr(self.cube.speaker, "volume", None)
        if volume is not None:
            self._publish_state(self._topic("speaker/volume"), int(volume))

    def _publish_state(self, topic: str, value: Any, *, force: bool = False) -> None:
        """Publish a retained value, skipping an unchanged one."""
        if not force and self._last_state.get(topic) == value:
            return
        self._last_state[topic] = value
        self._last_publish[topic] = time.monotonic()
        self._publish(topic, value, retain=True)

    def _publish(self, topic: str, payload: Any, *, retain: bool = False) -> None:
        client = self._client
        if client is None:
            return
        client.publish(topic, payload, retain=retain)

    # ---------------- discovery ----------------

    def _device(self) -> dict[str, Any]:
        return {"identifiers": [self.client_id], "name": "LumiCube",
                "manufacturer": "Abstract Foundry", "model": "LumiCube",
                "configuration_url": f"http://{self.host}"}

    def _availability(self) -> dict[str, Any]:
        return {"availability_topic": self._topic("status"),
                "payload_available": "online", "payload_not_available": "offline"}

    def _discover(self, client: Any, component: str, object_id: str,
                  payload: dict[str, Any]) -> None:
        topic = (f"{self.discovery_prefix}/{component}/{self.client_id}/"
                 f"{object_id}/config")
        body = {"unique_id": f"{self.client_id}_{object_id}",
                "device": self._device(), **self._availability(), **payload}
        client.publish(topic, json.dumps(body), retain=True)

    def _publish_discovery(self, client: Any) -> None:
        """Announce the entities Home Assistant should create."""
        if not self.discovery:
            return
        sensors = (
            ("ambient_light", "Ambient light", None, None),
            ("red", "Light sensor red", None, None),
            ("green", "Light sensor green", None, None),
            ("blue", "Light sensor blue", None, None),
            ("within_proximity", "Something within proximity", None, None),
        )
        for field, name, device_class, unit in sensors:
            payload: dict[str, Any] = {
                "name": name,
                "state_topic": self._topic(f"light_sensor/{field}"),
            }
            if device_class:
                payload["device_class"] = device_class
            if unit:
                payload["unit_of_measurement"] = unit
            self._discover(client, "sensor", f"light_sensor_{field}", payload)

        self._discover(client, "sensor", "cpu_temp", {
            "name": "Cube CPU temperature",
            "state_topic": self._topic("pi/cpu_temp"),
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
        })
        self._discover(client, "number", "volume", {
            "name": "Speaker volume",
            "state_topic": self._topic("speaker/volume"),
            "command_topic": self._topic("speaker/volume/set"),
            "min": 0, "max": 200, "step": 5, "mode": "slider",
            "icon": "mdi:volume-high",
        })
        self._discover(client, "number", "brightness", {
            "name": "Display brightness",
            "state_topic": self._topic("display/brightness"),
            "command_topic": self._topic("display/brightness/set"),
            "min": 0, "max": 255, "step": 5, "mode": "slider",
            "icon": "mdi:brightness-6",
        })
        self._discover(client, "text", "speak", {
            "name": "Speak",
            "command_topic": self._topic("speaker/say"),
            "icon": "mdi:bullhorn",
        })
        for action, name in (("top", "Top button"), ("middle", "Middle button"),
                             ("bottom", "Bottom button"), ("button", "Base button")):
            self._discover(client, "event", f"button_{action}", {
                "name": name,
                "state_topic": self._topic(f"buttons/{action}/event"),
                "event_types": ["press"],
                "device_class": "button",
            })
