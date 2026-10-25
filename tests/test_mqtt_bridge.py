"""MQTT bridge: command mapping, state publishing and discovery payloads.

No broker: a fake paho client records what would go on the wire, and a fake
cube records what the commands did. The bridge imports paho lazily, so these
run without it installed.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from pylumicube.mqtt import (
    SCROLL_FRAME_TIMEOUT,
    MqttBridge,
    parse_colour,
    parse_payload,
)


class FakeSpeaker:
    def __init__(self) -> None:
        self.volume = 100
        self.tones: list[tuple] = []
        self.played: list[str] = []
        self.stops = 0

    def tone(self, frequency, duration, amplitude=0.25, function="sine") -> None:
        self.tones.append((frequency, duration, amplitude, function))

    def play(self, path: str) -> None:
        self.played.append(path)

    def stop(self) -> None:
        self.stops += 1


class FakeScreen:
    def __init__(self) -> None:
        self.texts: list[tuple] = []
        self.images: list[str] = []
        self.clears: list[int] = []

    def write_text(self, x, y, text, size=1, colour=0xFFFFFF) -> None:
        self.texts.append((x, y, text, size, colour))

    def draw_image(self, path: str) -> None:
        self.images.append(path)

    def clear(self, colour: int = 0) -> None:
        self.clears.append(colour)


class FakeDisplay:
    def __init__(self) -> None:
        self.fills: list[int] = []
        self.brightness: list[int] = []
        self.scrolls: list[tuple] = []

    def fill(self, colour: int) -> None:
        self.fills.append(colour)

    def set_brightness(self, value: int) -> None:
        self.brightness.append(value)

    def scroll_text(self, text, colour=0xFFFFFF, background=0x000000, speed=1.0,
                    timeout=None) -> None:
        self.scrolls.append((text, colour, background, speed, timeout))


class FakeSensor:
    def get(self, field, default=None):
        return {"ambient_light": 123, "red": 10, "green": 20, "blue": 30,
                "within_proximity": 0}.get(field, default)


class FakeCube:
    def __init__(self) -> None:
        self.speaker = FakeSpeaker()
        self.screen = FakeScreen()
        self.display = FakeDisplay()
        self.said: list[tuple] = []
        self.modules = {"light_sensor": FakeSensor()}

    def say(self, text, *, voice=None, speed=None, volume=None) -> None:
        self.said.append((text, voice, volume))


class FakeClient:
    """Stands in for a paho client: records publishes and subscriptions."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object, bool]] = []
        self.subscribed: list[str] = []

    def publish(self, topic, payload=None, retain=False, **kwargs) -> None:
        self.published.append((topic, payload, retain))

    def subscribe(self, topic) -> None:
        self.subscribed.append(topic)

    def topics(self) -> dict[str, object]:
        return {topic: payload for topic, payload, _retain in self.published}


@pytest.fixture()
def bridge():
    class FakeDaemon:
        def __init__(self) -> None:
            self.cube = FakeCube()

    instance = MqttBridge(FakeDaemon(), host="broker", prefix="lumicube")
    instance._client = FakeClient()          # noqa: SLF001 - no broker here
    instance._connected.set()                # noqa: SLF001
    return instance


# ----- payload parsing ----------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("#ff8000", 0xFF8000), ("ff8000", 0xFF8000), ("0x00ff00", 0x00FF00),
    ("255", 0x0000FF), ("0", 0),
])
def test_parse_colour(text, expected) -> None:
    assert parse_colour(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("hello", "hello"), ("440", 440), ("0.5", 0.5),
    ('{"text": "hi"}', {"text": "hi"}), ("true", "true"),
])
def test_parse_payload(text, expected) -> None:
    assert parse_payload(text) == expected


# ----- commands -----------------------------------------------------------


def test_say_accepts_plain_text_and_json(bridge) -> None:
    bridge._handle_command("speaker/say", "The washing machine has finished")
    bridge._handle_command("speaker/say", '{"text": "Dinner is ready", "volume": 60}')
    assert bridge.cube.said == [("The washing machine has finished", None, None),
                                ("Dinner is ready", None, 60)]


def test_tone_accepts_a_number_or_an_object(bridge) -> None:
    bridge._handle_command("speaker/tone", "440")
    bridge._handle_command("speaker/tone",
                           '{"frequency": 880, "duration": 0.1, "waveform": "square"}')
    assert bridge.cube.speaker.tones == [(440.0, 0.5, 0.25, "sine"),
                                         (880.0, 0.1, 0.25, "square")]


def test_commands_never_subscribe_to_our_own_state_topics(bridge) -> None:
    """The loop that made HA's volume slider bounce 0/5/10.

    The bridge publishes state on `lumicube/speaker/volume` and
    `lumicube/display/brightness`; if it also *listened* there it would hear
    its own state as a command, and Home Assistant's number entity would
    answer it — around and around.
    """
    topics = bridge._command_topics()          # noqa: SLF001
    assert "lumicube/speaker/volume/set" in topics
    assert "lumicube/display/brightness/set" in topics
    assert "lumicube/speaker/volume" not in topics
    assert "lumicube/display/brightness" not in topics
    assert not any(topic.endswith("#") for topic in topics)


def test_an_echo_of_our_own_state_is_ignored(bridge) -> None:
    bridge._handle_command("speaker/volume/set", "70")      # a real command
    assert bridge.cube.speaker.volume == 70
    bridge.cube.speaker.volume = 70
    bridge._last_state["lumicube/speaker/volume"] = 70      # noqa: SLF001
    bridge.cube.speaker.volume = 99                         # would-be change
    bridge._handle_command("speaker/volume", "70")          # our own state back
    assert bridge.cube.speaker.volume == 99, "the echo was applied as a command"


def test_display_scroll_takes_text_and_options(bridge) -> None:
    """The 16x8 matrix fits ~3 characters, so a message has to travel."""
    bridge._handle_command("display/scroll",
                           json.dumps({"text": "Waschmaschine fertig!",
                                       "colour": "#ff8800", "speed": 1.5}))
    assert bridge._scroll_thread is not None, "the scroll did not run"
    bridge._scroll_thread.join(timeout=5)
    assert bridge.cube.display.scrolls == [
        ("Waschmaschine fertig!", 0xFF8800, 0, 1.5, SCROLL_FRAME_TIMEOUT)]

    bridge._handle_command("display/scroll", "plain text")
    bridge._scroll_thread.join(timeout=5)
    assert bridge.cube.display.scrolls[-1][0] == "plain text"

    bridge._handle_command("display/scroll", "")
    assert len(bridge.cube.display.scrolls) == 2, "empty text scrolled"


def test_a_scroll_waits_for_the_speaker(bridge) -> None:
    """Scroll and speech must not overlap: on the shared link they starve.

    Measured on the cube: run together, a 21-character message was still
    scrolling 90 s later while the announcement dropped every chunk.
    """
    lock = threading.RLock()
    bridge.daemon.server = SimpleNamespace(audio_lock=lock)
    lock.acquire()                        # pretend a `say` is in progress
    try:
        bridge._handle_command("display/scroll", "hello")
        bridge._scroll_thread.join(timeout=0.3)
        assert bridge.cube.display.scrolls == [], "scrolled over the speaker"
    finally:
        lock.release()
    bridge._scroll_thread.join(timeout=5)
    assert bridge.cube.display.scrolls[-1][0] == "hello"


def test_say_takes_a_language(bridge) -> None:
    """`language` is the friendlier spelling of espeak's `voice`."""
    bridge._handle_command("speaker/say",
                           json.dumps({"text": "Die Wäsche ist fertig",
                                       "language": "de"}))
    assert bridge.cube.said == [("Die Wäsche ist fertig", "de", None)]

    bridge._handle_command("speaker/say", json.dumps({"text": "hi", "voice": "en-gb"}))
    assert bridge.cube.said[-1] == ("hi", "en-gb", None)


def test_play_stop_and_volume(bridge) -> None:
    bridge._handle_command("speaker/play", "/tmp/clip.wav")
    bridge._handle_command("speaker/stop", "1")
    bridge._handle_command("speaker/volume", "70")
    assert bridge.cube.speaker.played == ["/tmp/clip.wav"]
    assert bridge.cube.speaker.stops == 1
    assert bridge.cube.speaker.volume == 70
    # The new volume is echoed back (retained) so HA shows what it set.
    assert bridge._client.topics()["lumicube/speaker/volume"] == 70


def test_screen_commands(bridge) -> None:
    bridge._handle_command("screen/text", '{"text": "hello", "size": 3}')
    bridge._handle_command("screen/image", "/tmp/pic.jpg")
    bridge._handle_command("screen/clear", "#102030")
    assert bridge.cube.screen.texts == [(10, 10, "hello", 3, 0xFFFFFF)]
    assert bridge.cube.screen.images == ["/tmp/pic.jpg"]
    assert bridge.cube.screen.clears == [0x102030]


def test_display_commands(bridge) -> None:
    bridge._handle_command("display/fill", "#00ff00")
    bridge._handle_command("display/brightness", "128")
    assert bridge.cube.display.fills == [0x00FF00]
    assert bridge.cube.display.brightness == [128]
    assert bridge._client.topics()["lumicube/display/brightness"] == 128


def test_unknown_command_is_ignored(bridge) -> None:
    bridge._handle_command("nonsense/topic", "1")
    assert bridge.cube.said == []


# ----- state --------------------------------------------------------------


def test_state_is_published_and_deduplicated(bridge) -> None:
    bridge._publish_all_state(bridge._client)
    bridge._publish_all_state(bridge._client)          # nothing changed
    topics = bridge._client.topics()
    assert topics["lumicube/light_sensor/ambient_light"] == 123
    assert topics["lumicube/light_sensor/red"] == 10
    assert topics["lumicube/speaker/volume"] == 100
    publishes = [topic for topic, _payload, _retain in bridge._client.published]
    assert publishes.count("lumicube/light_sensor/ambient_light") == 1
    assert all(retain for _t, _p, retain in bridge._client.published)


def test_button_events_are_published_unretained(bridge) -> None:
    bridge.button_pressed("buttons", "top")
    assert bridge._client.published == [("lumicube/buttons/top/event", "press", False)]


def test_temperature_is_rounded(bridge) -> None:
    bridge.publish_temperature(46.234)
    assert bridge._client.topics()["lumicube/pi/cpu_temp"] == 46.2


# ----- discovery ----------------------------------------------------------


def test_discovery_covers_the_entities_ha_should_create(bridge) -> None:
    bridge._publish_discovery(bridge._client)
    published = {topic: payload for topic, payload, retain in bridge._client.published}
    assert all(retain for _t, _p, retain in bridge._client.published)

    configs = {topic: json.loads(payload) for topic, payload in published.items()
               if topic.endswith("/config")}
    assert "homeassistant/sensor/pylumicube/light_sensor_ambient_light/config" in configs
    assert "homeassistant/number/pylumicube/volume/config" in configs
    assert "homeassistant/text/pylumicube/speak/config" in configs
    assert "homeassistant/event/pylumicube/button_top/config" in configs

    for payload in configs.values():
        assert payload["unique_id"].startswith("pylumicube_")
        assert payload["device"]["identifiers"] == ["pylumicube"]
        assert payload["availability_topic"] == "lumicube/status"

    volume = configs["homeassistant/number/pylumicube/volume/config"]
    assert volume["command_topic"] == "lumicube/speaker/volume/set"
    assert volume["state_topic"] == "lumicube/speaker/volume"
    assert (volume["min"], volume["max"]) == (0, 200)
    speak = configs["homeassistant/text/pylumicube/speak/config"]
    assert speak["command_topic"] == "lumicube/speaker/say"
    button = configs["homeassistant/event/pylumicube/button_top/config"]
    assert button["state_topic"] == "lumicube/buttons/top/event"
    assert button["event_types"] == ["press"]
    temperature = configs["homeassistant/sensor/pylumicube/cpu_temp/config"]
    assert temperature["device_class"] == "temperature"


def test_discovery_can_be_switched_off() -> None:
    class FakeDaemon:
        cube = FakeCube()

    bridge = MqttBridge(FakeDaemon(), host="broker", discovery=False)
    bridge._client = FakeClient()             # noqa: SLF001
    bridge._publish_discovery(bridge._client)
    assert bridge._client.published == []


def test_message_handling_survives_a_bad_command(bridge) -> None:
    """A malformed colour must not stop the bridge."""
    class Message:
        topic = "lumicube/display/fill"
        payload = b"not-a-colour"

    bridge._on_message(bridge._client, None, Message())     # logs, does not raise
    class Good:
        topic = "lumicube/display/fill"
        payload = b"#123456"

    bridge._on_message(bridge._client, None, Good())
    assert bridge.cube.display.fills == [0x123456]
