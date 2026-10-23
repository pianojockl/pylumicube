"""Speaker driver tests (no hardware).

`RecordingWriter` captures the field writes, so the tests can decode the
PCM chunks back into samples and check the streaming recipe (112-sample
chunks at the firmware's 32 kHz, signed 16-bit, one key per sample).
"""

from __future__ import annotations

import array
import io
import math
import shutil
import time
import wave

import pytest

from pylumicube.speaker import (
    CHUNK_SAMPLES,
    MAX_VOLUME,
    SAMPLE_RATE,
    SINE,
    SQUARE,
    NOISE,
    Speaker,
    pcm16,
    resample,
    wav_to_pcm,
)


class RecordingWriter:
    def __init__(self, delay: float = 0.0) -> None:
        self.writes: list[tuple[str, dict, float | None]] = []
        self.await_response: list[bool] = []
        self.waits: list[bool] = []
        self.delay = delay

    def __call__(self, module: str, values: dict, timeout: float | None = None,
                 await_response: bool = True, wait: bool = True):
        if self.delay:
            time.sleep(self.delay)
        self.writes.append((module, dict(values), timeout))
        self.await_response.append(await_response)
        self.waits.append(wait)
        if wait:
            return None
        future: Future = Future()
        future.set_result(None)
        return future

    # ----- helpers -----

    def chunks(self) -> list[list[int]]:
        """Decode the PCM writes back into per-chunk sample lists."""
        out = []
        for module, values, _timeout in self.writes:
            if module != "speaker" or not any(k.startswith("data+") for k in values):
                continue
            out.append([values[f"data+{i}"] for i in range(len(values) - 0)])
        return out


def _speaker(**kwargs) -> tuple[Speaker, RecordingWriter]:
    writer = RecordingWriter()
    kwargs.setdefault("pace", False)
    return Speaker(writer, **kwargs), writer


# ----- PCM helpers --------------------------------------------------------


def test_pcm16_packs_and_clamps() -> None:
    packed = pcm16([0.0, 1.0, -1.0, 2.0, -2.0])
    samples = array.array("h")
    samples.frombytes(packed)
    assert list(samples) == [0, 32767, -32767, 32767, -32767]


def test_resample_is_identity_at_the_same_rate() -> None:
    pcm = pcm16([0.5, -0.5])
    assert resample(pcm, SAMPLE_RATE, SAMPLE_RATE) is pcm


def test_resample_changes_the_sample_count() -> None:
    pcm = pcm16([0.0] * 22050)              # one second at 22.05 kHz
    out = resample(pcm, 22050, 32000)
    assert len(out) // 2 == 32000


def test_resample_interpolates_a_ramp() -> None:
    ramp = array.array("h", [0, 1000, 2000, 3000])
    out = array.array("h")
    out.frombytes(resample(ramp.tobytes(), 2, 4))
    assert list(out) == [0, 500, 1000, 1500, 2000, 2500, 3000, 3000]


def test_wav_to_pcm_handles_stereo_and_rate() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        stereo = array.array("h", [1000, 3000] * 16000)     # one second, L/R
        handle.writeframes(stereo.tobytes())
    pcm = wav_to_pcm(buffer.getvalue(), SAMPLE_RATE)
    samples = array.array("h")
    samples.frombytes(pcm)
    assert len(samples) == SAMPLE_RATE                      # resampled to 1s
    assert set(samples) == {2000}                           # channels averaged


def test_wav_to_pcm_rejects_8_bit() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(b"\x80" * 100)
    with pytest.raises(ValueError):
        wav_to_pcm(buffer.getvalue())


# ----- streaming ----------------------------------------------------------


def test_write_pcm_sends_112_sample_chunks() -> None:
    speaker, writer = _speaker()
    speaker.write_pcm(pcm16([0.0] * (CHUNK_SAMPLES * 2)))
    chunks = writer.chunks()
    assert len(chunks) == 2
    assert all(len(chunk) == CHUNK_SAMPLES for chunk in chunks)
    assert all(module == "speaker" for module, _values, _timeout in writer.writes)


def test_write_pcm_pads_a_short_final_chunk() -> None:
    speaker, writer = _speaker()
    speaker.write_pcm(pcm16([1.0] * 10))
    chunks = writer.chunks()
    assert len(chunks) == 1
    assert chunks[0][:10] == [32767] * 10
    assert set(chunks[0][10:]) == {0}


def test_write_pcm_preserves_signed_samples() -> None:
    speaker, writer = _speaker()
    speaker.write_pcm(pcm16([0.5, -0.5]))
    chunk = writer.chunks()[0]
    assert chunk[0] > 0 > chunk[1]


def test_write_pcm_without_pacing_sends_everything_fast() -> None:
    speaker, writer = _speaker(pace=False)
    started = time.monotonic()
    speaker.write_pcm(pcm16([0.0] * (CHUNK_SAMPLES * 10)))
    assert len(writer.chunks()) == 10
    assert time.monotonic() - started < 0.5


def test_realtime_pacing_spreads_the_chunks() -> None:
    """One second of audio should take roughly a second to stream."""
    speaker, _writer = _speaker(pace=True)
    started = time.monotonic()
    speaker.write_pcm(pcm16([0.0] * SAMPLE_RATE))
    elapsed = time.monotonic() - started
    assert 0.5 < elapsed < 2.5


def test_slow_writes_are_dropped_not_queued() -> None:
    """A writer slower than real-time must not build an ever-growing lag."""
    writer = RecordingWriter(delay=0.02)         # 20ms per chunk > 3.5ms of audio
    speaker = Speaker(writer, pace=True, timeout=0.5)
    started = time.monotonic()
    speaker.write_pcm(pcm16([0.0] * SAMPLE_RATE))     # one second of audio
    elapsed = time.monotonic() - started
    assert speaker.dropped_chunks > 0
    assert elapsed < 3.0                              # it gave up on late chunks


def test_write_failure_is_counted_not_raised() -> None:
    def broken(module: str, values: dict, timeout: float | None = None,
               await_response: bool = True, wait: bool = True):
        raise TimeoutError("no ack")

    speaker = Speaker(broken, pace=False)
    speaker.write_pcm(pcm16([0.0] * CHUNK_SAMPLES))
    assert speaker.dropped_chunks == 1
    assert speaker.written_chunks == 0


def test_speaker_writes_do_not_wait_for_a_service_response() -> None:
    """The firmware services speaker writes without answering them."""
    speaker, writer = _speaker()
    speaker.write_pcm(pcm16([0.0] * CHUNK_SAMPLES))
    assert writer.await_response == [False]
    writer.await_response.clear()
    speaker.volume = 100
    assert writer.await_response == [False]


def test_chunks_are_pipelined() -> None:
    """Audio writes must not block on each ACK: latency is ~50-100 ms."""
    speaker, writer = _speaker()
    speaker.write_pcm(pcm16([0.0] * (CHUNK_SAMPLES * 8)))
    assert writer.waits == [False] * 8


def test_stop_ends_the_stream() -> None:
    speaker, writer = _speaker(pace=False)

    def stop_after_first(module: str, values: dict, timeout: float | None = None,
                         await_response: bool = True, wait: bool = True):
        writer.writes.append((module, dict(values), timeout))
        speaker.stop()
        return None

    speaker._write = stop_after_first      # noqa: SLF001 — test seam
    speaker.write_pcm(pcm16([0.0] * (CHUNK_SAMPLES * 5)))
    assert len(writer.chunks()) == 1


def test_flow_control_waits_for_free_space() -> None:
    speaker, _writer = _speaker()
    free = [0, 0, 512]
    speaker._read = lambda name: free.pop(0) if free else 512   # noqa: SLF001
    speaker._wait_for_space()                                   # noqa: SLF001
    assert not free                        # it consumed the blocked readings


# ----- volume, tones and speech -------------------------------------------


def test_volume_is_written_and_clamped() -> None:
    speaker, writer = _speaker()
    speaker.volume = 100
    assert writer.writes[-1][0] == "speaker"
    assert writer.writes[-1][1] == {"volume": 100}
    speaker.volume = 999
    assert speaker.volume == MAX_VOLUME
    speaker.volume = -5
    assert speaker.volume == 0


def test_tone_generates_the_right_duration_and_amplitude() -> None:
    speaker, writer = _speaker()
    speaker.tone(frequency=1000, duration=0.1, amplitude=0.5)
    samples = [sample for chunk in writer.chunks() for sample in chunk]
    assert len(samples) == pytest.approx(SAMPLE_RATE * 0.1, abs=CHUNK_SAMPLES)
    assert max(abs(sample) for sample in samples) <= 32767 * 0.5 + 1
    assert max(abs(sample) for sample in samples) > 32767 * 0.4


def test_tone_fades_in_and_out() -> None:
    speaker, writer = _speaker()
    speaker.tone(frequency=440, duration=0.05, amplitude=1.0)
    samples = [sample for chunk in writer.chunks() for sample in chunk]
    assert samples[0] == 0
    assert abs(samples[-1]) < 2000          # faded back down
    assert max(abs(sample) for sample in samples) > 20000


def test_tone_waveforms_differ() -> None:
    speaker, writer = _speaker()
    for waveform in (SINE, SQUARE, NOISE):
        writer.writes.clear()
        speaker.tone(frequency=100, duration=0.02, function=waveform)
        assert writer.chunks()
    with pytest.raises(ValueError):
        speaker.tone(function="sawtooth")


def test_tone_reaches_the_expected_frequency() -> None:
    """A 1 kHz sine should complete 10 cycles in 10 ms."""
    speaker, writer = _speaker()
    speaker.tone(frequency=1000, duration=0.01, amplitude=1.0, fade=0.0)
    samples = [sample for chunk in writer.chunks() for sample in chunk]
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if a <= 0 < b)
    assert crossings == pytest.approx(10, abs=1)


def test_sine_wave_shape_is_roughly_sinusoidal() -> None:
    speaker, writer = _speaker()
    speaker.tone(frequency=100, duration=0.02, amplitude=1.0, fade=0.0)
    samples = [sample for chunk in writer.chunks() for sample in chunk][:320]
    expected = [math.sin(2 * math.pi * 100 * i / SAMPLE_RATE) * 32767
                for i in range(len(samples))]
    error = max(abs(a - b) for a, b in zip(samples, expected))
    assert error < 50


def test_say_without_espeak_explains_what_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    speaker, _writer = _speaker()
    with pytest.raises(RuntimeError, match="espeak"):
        speaker.say("hello")


def test_say_streams_the_synthesised_wav(monkeypatch) -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(22050)
        handle.writeframes(array.array("h", [1000] * 22050).tobytes())
    wav_bytes = buffer.getvalue()

    class FakeResult:
        returncode = 0
        stdout = wav_bytes
        stderr = b""

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/espeak-ng")
    monkeypatch.setattr("pylumicube.speaker.subprocess.run",
                        lambda *args, **kwargs: FakeResult())
    speaker, writer = _speaker()
    speaker.say("hello")
    total = sum(len(chunk) for chunk in writer.chunks())
    assert total == pytest.approx(SAMPLE_RATE, rel=0.01)     # ~1s at 32 kHz


def test_play_reads_a_wav_file(tmp_path) -> None:
    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(array.array("h", [500] * SAMPLE_RATE).tobytes())
    speaker, writer = _speaker()
    speaker.play(str(path))
    total = sum(len(chunk) for chunk in writer.chunks())
    # The final chunk is zero-padded up to the firmware's chunk size.
    assert SAMPLE_RATE <= total < SAMPLE_RATE + CHUNK_SAMPLES


