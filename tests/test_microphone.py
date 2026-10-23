"""Microphone capture (no hardware).

A fake telemetry store feeds frames the way the firmware would: one block of
`microphone.data` values per frame, 112 of them at a time.
"""

from __future__ import annotations

import threading
import time
import wave
from types import SimpleNamespace

import pytest

from pylumicube.metadata import FieldSpec
from pylumicube.microphone import SAMPLE_BYTES, Microphone

DATA_KEY = 1000
SPAN = 112


class FakeModule:
    name = "microphone"

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []
        self.refs = {
            "data": SimpleNamespace(node_id=125, key=DATA_KEY, spec=FieldSpec(
                floor=DATA_KEY, span=SPAN, type=2, size=2, name="data",
                module="microphone")),
            "enable": SimpleNamespace(node_id=125, key=999, spec=FieldSpec(
                floor=999, span=1, type=2, size=1, name="enable",
                module="microphone")),
        }

    def ref(self, name: str):
        return self.refs[name]


class FakeTelemetry:
    """Store-like: hands frames to whatever registered an update handler."""

    def __init__(self) -> None:
        self.handlers: list = []

    def add_update_handler(self, handler) -> None:
        self.handlers.append(handler)

    def remove_update_handler(self, handler) -> None:
        self.handlers.remove(handler)

    def emit(self, values: dict[int, int], node_id: int = 125) -> None:
        for handler in list(self.handlers):
            handler(node_id, values)


def _mic(**kwargs) -> tuple[Microphone, FakeModule, FakeTelemetry, list[str]]:
    module = FakeModule()
    telemetry = FakeTelemetry()
    subscribed: list[str] = []

    def write(name: str, values: dict) -> None:
        module.writes.append((name, dict(values)))

    mic = Microphone(module, telemetry=telemetry, write=write,
                     subscribe=lambda: subscribed.append("cube"), **kwargs)
    return mic, module, telemetry, subscribed


def _frame(value: int = 100) -> dict[int, int]:
    return {DATA_KEY + offset: value for offset in range(SPAN)}


# ----- enabling -----------------------------------------------------------


def test_enable_subscribes_and_writes_the_flag() -> None:
    mic, module, _telemetry, subscribed = _mic()
    mic.enable()
    assert subscribed == ["cube"]
    assert module.writes == [("microphone", {"enable": 1})]
    assert mic.enabled
    mic.enable()                       # idempotent
    assert len(module.writes) == 1


def test_disable_writes_the_flag_and_unregisters() -> None:
    mic, module, telemetry, _subscribed = _mic()
    mic.enable()
    mic.disable()
    assert module.writes[-1] == ("microphone", {"enable": 0})
    assert not mic.enabled
    telemetry.emit(_frame())
    assert mic.frames == 0             # no longer listening


def test_missing_writer_is_a_clear_error() -> None:
    mic = Microphone(FakeModule(), telemetry=FakeTelemetry())
    with pytest.raises(RuntimeError, match="no writer"):
        mic.enable(subscribe=False)


# ----- frame assembly -----------------------------------------------------


def test_frames_become_little_endian_pcm() -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    telemetry.emit(_frame(0x1234))
    chunk = mic.read_chunk(timeout=1.0)
    assert chunk == b"\x34\x12" * SPAN
    assert mic.frames == 1


def test_negative_samples_keep_their_bits() -> None:
    """s16le is the same bytes as unsigned two's complement."""
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    telemetry.emit(_frame(0xFFFF))     # -1 as a signed 16-bit sample
    assert mic.read_chunk(timeout=1.0) == b"\xff\xff" * SPAN


def test_frames_from_other_nodes_are_ignored() -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    telemetry.emit(_frame(), node_id=124)
    assert mic.frames == 0


def test_unrelated_fields_do_not_make_a_chunk() -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    telemetry.emit({999: 1})           # the `enable` field, not the block
    assert mic.read_chunk(timeout=0.05) is None
    assert mic.frames == 0


# ----- capture, record, stream -------------------------------------------


def _feeder(telemetry: FakeTelemetry, stop: threading.Event,
            period: float = 0.002) -> threading.Thread:
    """Emit frames the way the firmware does: 143 of them a second."""
    def run() -> None:
        while not stop.is_set():
            telemetry.emit(_frame(0x0100))
            time.sleep(period)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_capture_collects_frames_for_the_requested_time() -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    stop = threading.Event()
    feeder = _feeder(telemetry, stop)
    try:
        pcm = mic.capture(0.1, enable=False)
    finally:
        stop.set()
        feeder.join(timeout=1.0)
    assert len(pcm) == int(0.1 * mic.sample_rate) * SAMPLE_BYTES
    # 0.1 s at 16 kHz is ~14 frames of 112 samples; capture stops once it has
    # enough, rather than waiting for a fixed number of frames.
    assert 10 <= mic.frames <= 40


def test_capture_gives_up_when_frames_stop(tmp_path) -> None:
    mic, _module, _telemetry, _subscribed = _mic()
    mic.enable()
    started = time.monotonic()
    pcm = mic.capture(0.5, timeout=0.2, enable=False)
    elapsed = time.monotonic() - started
    assert pcm == b""
    assert elapsed < 1.0, "capture waited for audio that never came"


def test_record_writes_a_playable_wav(tmp_path) -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    stop = threading.Event()
    feeder = _feeder(telemetry, stop)
    path = tmp_path / "mic.wav"
    try:
        samples = mic.record(str(path), 0.05, enable=False)
    finally:
        stop.set()
        feeder.join(timeout=1.0)
    assert samples > 0
    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == SAMPLE_BYTES
        assert handle.getframerate() == mic.sample_rate
        assert handle.getnframes() == samples


def test_chunks_streams_frames() -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    stop = threading.Event()
    feeder = _feeder(telemetry, stop)
    try:
        received = list(mic.chunks(0.05, enable=False))
    finally:
        stop.set()
        feeder.join(timeout=1.0)
    assert received
    assert all(len(chunk) == SPAN * SAMPLE_BYTES for chunk in received)


def test_chunk_samples_comes_from_the_schema() -> None:
    mic, _module, _telemetry, _subscribed = _mic()
    assert mic.chunk_samples == SPAN


def test_stats_and_drain() -> None:
    mic, _module, telemetry, _subscribed = _mic()
    mic.enable()
    for _ in range(3):
        telemetry.emit(_frame())
    assert mic.stats["frames"] == 3
    assert mic.stats["buffered"] == 3
    assert mic.drain() == 3
    assert mic.stats["buffered"] == 0
