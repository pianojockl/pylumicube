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
import threading
import time
import wave
from concurrent.futures import Future

import pytest

from pylumicube.constants import DROPPED
from pylumicube.speaker import (
    CHUNK_SAMPLES,
    MAX_VOLUME,
    SAMPLE_RATE,
    SPEAKER_RATE,
    SINE,
    SQUARE,
    NOISE,
    Speaker,
    pcm16,
    resolve_voice,
    set_speak_language,
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


# ----- pacing profiles ----------------------------------------------------


def test_every_profile_is_named_and_resolvable() -> None:
    from pylumicube.speaker import PROFILES, profile_name, resolve_profile

    assert set(PROFILES) >= {"default", "fine", "deep", "flood", "soft-volume"}
    for name, profile in PROFILES.items():
        assert profile_name(profile) == name
        assert resolve_profile(name) is profile
    assert resolve_profile(None) is PROFILES["default"]


def test_an_unknown_profile_is_a_clear_error() -> None:
    from pylumicube.speaker import resolve_profile

    with pytest.raises(ValueError) as caught:
        resolve_profile("nonsense")
    assert "deep" in str(caught.value)


def test_a_stream_records_its_timing() -> None:
    """The report is what the shootout prints and `stats` exposes."""
    speaker, _writer = _speaker()
    speaker.write_pcm(pcm16([0.1] * (CHUNK_SAMPLES * 4)), profile="deep")
    report = speaker.last_stream
    assert report is not None
    assert report.profile == "deep"
    assert report.chunks == 4
    assert speaker.stream_count == 1
    payload = report.as_dict()
    assert payload["chunks"] == 4
    assert set(payload["gaps_ms"]) == {"p50", "p95", "p99", "max"}
    assert payload["audio_seconds"] == pytest.approx(
        4 * CHUNK_SAMPLES / SPEAKER_RATE, abs=0.001)
    assert payload["long_gaps"] == 0


def test_the_default_profile_is_the_shipping_behaviour() -> None:
    from pylumicube.speaker import PROFILES

    shipping = PROFILES["default"]
    assert shipping.lead is None and shipping.pace_group is None
    assert shipping.realtime is None and shipping.volume_mode == "firmware"
    #: It paces on what the link has *taken*, not on the clock: a clock pacer
    #: can only ever be late, and lateness is what empties the buffer.
    assert shipping.ahead == 8
    assert shipping.rate is None              # the daemon's rate, not the profile's


def test_speech_is_resampled_to_the_rates_the_profile_will_pace() -> None:
    """`say`/`play` must resample to the profile's rate, not the driver's.

    The two would otherwise disagree: a `rate16000` profile pacing a stream
    generated at 22050 Hz plays back slow.
    """
    from pylumicube.speaker import PROFILES

    assert PROFILES["default"].effective_rate(SPEAKER_RATE) == SPEAKER_RATE
    for name, profile in PROFILES.items():
        if profile.rate is not None:
            assert profile.effective_rate(SPEAKER_RATE) == profile.rate


def test_software_volume_scales_the_pcm_and_pins_the_firmware() -> None:
    """The A/B for the firmware's own 20/200 scaling.

    `soft-volume` hands the firmware full scale and pre-scales the samples
    (a tenth here, volume 20 of 200), so the loudness is identical but the
    firmware's integer scaling is out of the path.
    """
    speaker, writer = _speaker(volume=20)
    speaker.write_pcm(pcm16([0.5] * CHUNK_SAMPLES), profile="soft-volume")
    volumes = [values["volume"] for module, values, _t in writer.writes
               if module == "speaker" and "volume" in values]
    assert volumes == [200, 20]                 # pinned, then restored
    data = [values for _m, values, _t in writer.writes if "data+0" in values]
    assert data, "the stream wrote no samples"
    first = int(data[0]["data+0"])
    # 0.5 full-scale scaled by 20/200 -> ~1638, not ~16383.
    assert 1400 < first < 1900


def test_scale_pcm_halves_and_clips() -> None:
    from pylumicube.speaker import scale_pcm

    samples = array.array("h", [1000, -1000, 32767, -32768])
    out = array.array("h")
    out.frombytes(scale_pcm(samples.tobytes(), 0.5))
    assert list(out) == [500, -500, 16383, -16384]


# ----- ACK-paced streaming ("ahead" profiles) -----------------------------


class AckPacedWriter:
    """Resolves each write after `delay` seconds, so `ahead` has something to
    wait for. Tracks how many writes were outstanding at any moment."""

    def __init__(self, delay: float = 0.004) -> None:
        self.delay = delay
        self.outstanding = 0
        self.max_outstanding = 0
        self.writes = 0
        self._lock = threading.Lock()

    def __call__(self, samples, timeout: float | None = None,
                 await_response: bool = True, wait: bool = True,
                 label: str = "audio", droppable: bool = True):
        future: Future = Future()
        with self._lock:
            self.outstanding += 1
            self.writes += 1
            self.max_outstanding = max(self.max_outstanding, self.outstanding)

        def finish() -> None:
            with self._lock:
                self.outstanding -= 1
            if not future.done():
                future.set_result(None)

        timer = threading.Timer(self.delay, finish)
        timer.daemon = True
        timer.start()
        return future


def test_ahead_pacing_never_exceeds_the_outstanding_bound() -> None:
    """The pump keeps N chunks in the link, so the firmware's ring stays fed.

    Wall-clock pacing can only ever be late (there is no mechanism to send
    early), which is why the ring hovers at empty; pacing on what the link has
    *taken* keeps a reserve in flight instead.
    """
    writer = AckPacedWriter(delay=0.004)
    speaker = Speaker(write=None, run=writer, read=lambda *a, **k: None,
                      pace=False)
    from pylumicube.speaker import PacingProfile
    speaker.write_pcm(pcm16([0.2] * (CHUNK_SAMPLES * 20)),
                      profile=PacingProfile(ahead=4))
    assert writer.writes >= 20
    assert writer.max_outstanding <= 4, writer.max_outstanding
    report = speaker.last_stream
    assert report is not None and report.chunks == 20
    assert report.dropped == 0


def test_wall_clock_pacing_does_not_bound_outstanding_writes() -> None:
    """The control: without `ahead`, the pacer submits on schedule instead."""
    writer = AckPacedWriter(delay=0.004)
    speaker = Speaker(write=None, run=writer, read=lambda *a, **k: None,
                      pace=False)
    speaker.write_pcm(pcm16([0.2] * (CHUNK_SAMPLES * 20)))
    assert writer.max_outstanding > 4


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
    speaker.write_pcm(pcm16([0.0] * SPEAKER_RATE))
    elapsed = time.monotonic() - started
    assert 0.5 < elapsed < 2.5


def test_slow_writes_are_dropped_not_queued() -> None:
    """A *clock*-paced writer must not build an ever-growing lag.

    The shipping profile paces on what the link has taken (and waits instead of
    dropping), so this spells out the wall-clock profile it is about.
    """
    from pylumicube.speaker import PacingProfile

    writer = RecordingWriter(delay=0.02)         # 20ms per chunk > 5ms of audio
    speaker = Speaker(writer, pace=True, timeout=0.5)
    started = time.monotonic()
    speaker.write_pcm(pcm16([0.0] * SPEAKER_RATE),      # one second of audio
                      profile=PacingProfile())
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
    assert len(samples) == pytest.approx(SPEAKER_RATE * 0.1, abs=CHUNK_SAMPLES)
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
    expected = [math.sin(2 * math.pi * 100 * i / SPEAKER_RATE) * 32767
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
    assert total == pytest.approx(SPEAKER_RATE, rel=0.01)    # ~1s of speech


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
    assert SPEAKER_RATE <= total < SPEAKER_RATE + CHUNK_SAMPLES




# ----- language resolution -------------------------------------------------


def test_iso_639_3_codes_map_to_what_espeak_accepts(monkeypatch) -> None:
    """espeak rejects `deu` with "voice does not exist"; `de` is what it wants."""
    monkeypatch.setattr("pylumicube.speaker._voices", {"de", "en", "german", "en-gb"})
    assert resolve_voice(None) is None
    assert resolve_voice("de") == "de"
    assert resolve_voice("deu") == "de"
    assert resolve_voice("de-DE") == "de"
    assert resolve_voice("german") == "german"
    assert resolve_voice("klingon") is None


def test_a_bare_language_is_accepted_when_only_variants_are_listed(monkeypatch) -> None:
    """espeak lists `en-us`/`en-gb` but not `en`, yet `-v en` works and is its
    default — rejecting it made the default look unknown."""
    monkeypatch.setattr("pylumicube.speaker._voices", {"en-us", "en-gb", "de"})
    assert resolve_voice("en") == "en"
    assert resolve_voice("de") == "de"
    assert resolve_voice("zz") is None, "invented a language"


def test_an_unknown_default_language_leaves_the_old_one(monkeypatch) -> None:
    """A typo in a config file must not quietly change how things sound."""
    monkeypatch.setattr("pylumicube.speaker._voices", {"de"})
    monkeypatch.setattr("pylumicube.speaker.SPEAK_LANGUAGE", None)
    assert set_speak_language("deu") == "de"
    assert set_speak_language("klingon") == "de"
    assert set_speak_language(None) is None


# ----- link classification -------------------------------------------------


def test_a_frame_dropped_by_the_link_counts_as_lost_audio() -> None:
    """The link sheds the oldest droppable frame under load. For audio that is
    a hole, and it used to be invisible: the future was discarded unread, so
    `dropped_chunks` never showed it."""
    results: list = [DROPPED, None]

    def run(samples, **kwargs):
        future: Future = Future()
        future.set_result(results.pop(0) if results else None)
        return future

    speaker = Speaker(write=None, run=run, read=lambda *a, **k: None, pace=False)
    speaker.write_pcm(b"\x00\x00" * 224)          # two chunks, so one is examined
    assert speaker.dropped_chunks >= 1, "a link-dropped frame went uncounted"


def test_audio_frames_are_classified_so_speech_never_queues() -> None:
    """The link orders its queue by class: audio first, display last.

    Speech stutters exactly when its frames wait behind display traffic on a
    busy link, which is what this pins.
    """
    calls: list[dict] = []

    def run(samples, **kwargs):
        calls.append(kwargs)
        future: Future = Future()
        future.set_result(b"")
        return future

    speaker = Speaker(write=None, run=run, read=lambda *a, **k: None, pace=False)
    speaker.write_pcm(b"\x00\x00" * 224)          # two 112-sample chunks

    assert calls, "nothing was written at all"
    assert all(call.get("label") == "audio" for call in calls), calls
    assert all(call.get("droppable") is True for call in calls), calls
