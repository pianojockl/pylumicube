"""Speaker driver: stream PCM into the cube's speaker.

The cube's speaker is fed by writing samples into `speaker.data`, a run of
112 signed 16-bit fields (keys 128..239 on the cube board). The daemon
(`SpeakerThread.java`) captures its PulseAudio sink monitor with ``pamon
--format=s16le --channels=1 --rate=32000`` and pushes **three 112-sample
batches per read** — one batch is 224 bytes, i.e. 3.5 ms of audio at the
firmware's 32 kHz rate.

This implementation skips PulseAudio entirely: it generates or decodes PCM
itself and paces the writes against the wall clock, so ``tone()``, ``play()``
and ``say()`` work on a headless Pi with no sound server. If the cube's
telemetry is available, `free_space_in_bytes` is consulted as flow control;
otherwise pacing is purely time-based. Frames that would arrive late are
dropped rather than queued, because audio latency matters more than
completeness (the daemon does the same: "Audio samples skipped due to
backoff signal").

``say()`` needs a TTS engine — ``espeak-ng`` (or ``espeak``) on the Pi.
"""

from __future__ import annotations

import array
import logging
import math
import random
import shutil
import struct
import subprocess
import time
import wave
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future

log = logging.getLogger(__name__)

#: The firmware's sample rate, from `speaker.frequency`.
SAMPLE_RATE = 32000
#: Samples per batch: the `speaker.data` block's span.
CHUNK_SAMPLES = 112
#: Speaker volume field range (`speaker.volume`, 0..200 per api.txt).
MAX_VOLUME = 200
#: Default volume. The firmware powers up at full blast, which is far too
#: loud in a room; measured on this cube, speech is comfortable around 20.
DEFAULT_VOLUME = 40

#: Audio sent ahead of real time, in seconds. The firmware buffers only a few
#: chunks, so pacing exactly to real time means every scheduling hiccup
#: underruns it — measured on the cube, ~10% of chunks arrived late (p95
#: 5-14 ms, spikes to 50 ms) with no lead, and a lead of about four chunks
#: absorbed most of it. A larger lead overflows the firmware instead (a
#: 35 ms lead dropped 51 chunks of a 5 second sentence), so this is a narrow
#: window rather than "more is better".
LEAD_SECONDS = 0.021

#: Chunks sent back-to-back per wakeup. `time.sleep` on a busy Pi overshoots
#: by a few milliseconds, which shows up as a tail of late chunks (measured
#: p95 gap 8.6 ms against a 3.5 ms ideal) — and the firmware's buffer is too
#: small to absorb that. Sleeping once per *group* means three times fewer
#: timing events and three times more audio behind each one.
PACE_GROUP = 3

#: How far behind the schedule audio may fall before chunks are skipped.
#: Upstream's PulseAudio buffer played the same role: a hiccup delayed a
#: write but the samples were still there afterwards, so the audio came out
#: complete and slightly late. Dropping on the first late chunk (the old
#: behaviour) turned every hiccup into an audible gap. A second of lag is
#: inaudible in an announcement; beyond it something is badly wrong and
#: skipping ahead is the lesser evil.
MAX_LAG_SECONDS = 1.0

SINE = "sine"
SQUARE = "square"
TRIANGLE = "triangle"
NOISE = "noise"


def pcm16(samples: Iterable[float]) -> bytes:
    """Pack floats in -1..1 into little-endian signed 16-bit PCM."""
    out = array.array("h")
    for sample in samples:
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        out.append(value)
    return out.tobytes()


def resample(pcm: bytes, from_rate: int, to_rate: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolate mono s16le PCM from one rate to another.

    TTS engines emit 22.05 kHz; the cube wants 32 kHz. Pure Python (a few
    hundred ms for a sentence), with numpy used when it is installed.
    """
    if from_rate == to_rate or not pcm:
        return pcm
    try:
        import numpy as np          # noqa: PLC0415 — optional fast path
        source = np.frombuffer(pcm, dtype="<i2").astype("float64")
        count = int(len(source) * to_rate / from_rate)
        positions = np.arange(count) * (from_rate / to_rate)
        left = np.floor(positions).astype("int64")
        right = np.clip(left + 1, 0, len(source) - 1)
        frac = positions - left
        mixed = source[left] * (1 - frac) + source[right] * frac
        return mixed.astype("<i2").tobytes()
    except ImportError:
        pass
    source = array.array("h")
    source.frombytes(pcm)
    count = int(len(source) * to_rate / from_rate)
    ratio = from_rate / to_rate
    out = array.array("h")
    last = len(source) - 1
    for index in range(count):
        position = index * ratio
        left = int(position)
        right = min(left + 1, last)
        frac = position - left
        out.append(int(source[left] * (1 - frac) + source[right] * frac))
    return out.tobytes()


def wav_to_pcm(data: bytes, target_rate: int = SAMPLE_RATE) -> bytes:
    """Decode WAV bytes to mono s16le at `target_rate`."""
    import io
    with wave.open(io.BytesIO(data), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"only 16-bit WAV is supported, got {width * 8}-bit")
    if channels == 2:
        stereo = array.array("h")
        stereo.frombytes(frames)
        mono = array.array("h", stereo[0::2])       # take the left channel
        for index in range(1, len(stereo), 2):
            mono[index // 2] = (mono[index // 2] + stereo[index]) // 2
        frames = mono.tobytes()
    elif channels != 1:
        raise ValueError(f"unsupported channel count {channels}")
    return resample(frames, rate, target_rate)


class Speaker:
    """Streams audio to the cube's speaker.

    `write` is the cube's name-based field writer (`LumiCube.write_named`);
    `read` is an optional reader for a published field (used for
    `free_space_in_bytes` flow control when telemetry is available).
    """

    def __init__(self, write: Callable[..., None], *,
                 read: Callable[[str], int | None] | None = None,
                 run: Callable[..., object] | None = None,
                 sample_rate: int = SAMPLE_RATE,
                 chunk_samples: int = CHUNK_SAMPLES,
                 volume: int = DEFAULT_VOLUME,
                 timeout: float = 0.5,
                 pace: bool = True,
                 lead: float = LEAD_SECONDS,
                 pace_group: int = PACE_GROUP,
                 max_lag: float = MAX_LAG_SECONDS,
                 await_response: bool = False,
                 max_in_flight: int = 32) -> None:
        self._write = write
        #: Fast path for sample blocks (`LumiCube.write_run`); falls back to
        #: `write` (name-based) when absent, e.g. in tests.
        self._run = run
        self._read = read
        self.sample_rate = sample_rate
        self.chunk_samples = chunk_samples
        self.timeout = timeout
        self.pace = pace
        # The firmware services speaker writes without answering them (the
        # daemon uses a NullContinuation), so by default we only wait for the
        # link-level ACK — and pipelining means we usually don't wait at all.
        self.await_response = await_response
        #: Upper bound on writes in flight; the link sustains ~380 chunks/s
        #: with a few dozen outstanding, and audio needs 286/s.
        self.max_in_flight = max_in_flight
        #: Seconds of audio sent ahead of real time, so the firmware's small
        #: buffer stays fed instead of sitting empty (see `write_pcm`).
        self.lead = float(lead)
        #: Chunks per wakeup when pacing (see `write_pcm`).
        self.pace_group = max(1, int(pace_group))
        #: How late audio may run before it is skipped (see `MAX_LAG_SECONDS`).
        self.max_lag = max(0.0, float(max_lag))
        self._volume = max(0, min(MAX_VOLUME, volume))
        self._stopped = False
        self.dropped_chunks = 0
        self.written_chunks = 0

    # ---------------- volume ----------------

    @property
    def volume(self) -> int:
        """Last volume written (0..200). The firmware cannot be read back."""
        return self._volume

    @volume.setter
    def volume(self, value: int) -> None:
        self._volume = max(0, min(MAX_VOLUME, int(value)))
        self._write("speaker", {"volume": self._volume},
                    await_response=self.await_response)

    def note_volume(self, value: int) -> None:
        """Record a volume someone else wrote, without writing it again.

        The firmware cannot be read back, so whoever sets the volume has to
        tell the object — the daemon uses this when a *client* changes it,
        so what it publishes to Home Assistant stays truthful.
        """
        self._volume = max(0, min(MAX_VOLUME, int(value)))

    # ---------------- streaming ----------------

    def stop(self) -> None:
        """Stop the current stream at the next chunk boundary."""
        self._stopped = True

    def write_pcm(self, pcm: bytes, *, realtime: bool | None = None) -> None:
        """Stream mono s16le PCM to the speaker.

        With `realtime` (the default when `pace` is set) writes are paced to
        the sample rate, so a caller can hand over a whole clip and get it
        played at the right speed.

        Chunks are **pipelined**: a single write costs ~50-100 ms of latency
        on this hardware, but the link sustains ~380 frames/s when several
        are in flight (measured), which is comfortably above the 286
        chunks/s that 32 kHz mono needs.

        The pacing is what makes it sound right, and it is worth being
        precise about, because the firmware's own buffer is small (~3 KB,
        about 47 ms): pacing *exactly* to real time leaves that buffer empty,
        so every scheduling hiccup is an audible gap. So:

        * `lead` seconds go out ahead of the schedule, to start with the
          buffer full rather than empty.
        * `pace_group` chunks are sent per wakeup, so there are that many
          fewer `sleep()` overshoots to be late with.
        * lateness is *tolerated* (`max_lag`): the audio plays slightly late
          rather than being thrown away, which is what upstream's PulseAudio
          buffer effectively did. Only a genuine runaway skips ahead.
        * the in-flight window is never *waited* on — the Java daemon used a
          `NullContinuation` and never blocked either; waiting here is what
          turns a busy link into a stall.
        """
        self._stopped = False
        if realtime is None:
            realtime = self.pace
        chunk_bytes = self.chunk_samples * 2
        chunk_duration = self.chunk_samples / self.sample_rate
        started = time.monotonic()
        sent = 0
        offset = 0
        total = len(pcm)
        in_flight: deque[Future] = deque()

        while offset < total - 1:
            if self._stopped:
                break
            chunk = pcm[offset : offset + chunk_bytes]
            if len(chunk) < chunk_bytes:
                chunk = chunk + b"\x00" * (chunk_bytes - len(chunk))   # pad
            if realtime:
                now = time.monotonic()
                schedule = started + sent * chunk_duration
                if now - schedule > self.max_lag:
                    # Hopelessly behind: skip *audio* (not just this chunk)
                    # so the rest is not played a second late.
                    remaining = (total - offset) // chunk_bytes
                    skip = max(1, min(int((now - schedule) / chunk_duration),
                                      remaining))
                    self.dropped_chunks += skip
                    sent += skip
                    offset += skip * chunk_bytes
                    continue
                due = schedule - self.lead
                if sent % self.pace_group == 0 and now < due:
                    # Sleep once per group: the schedule is absolute, so a
                    # group that arrives late is caught up by the next one,
                    # but there are `pace_group` times fewer sleeps to
                    # overshoot. The lead never tightens this: it is a head
                    # start, not an earlier deadline.
                    time.sleep(due - now)
                self._wait_for_space()
            while in_flight and in_flight[0].done():
                in_flight.popleft()
            if len(in_flight) >= self.max_in_flight:
                # Do not block. The future is only for error reporting and
                # audio does not need it; waiting turns a busy link into a
                # stall, which is exactly what the firmware cannot absorb.
                in_flight.popleft()

            samples = array.array("h")
            samples.frombytes(chunk)
            try:
                if self._run is not None:
                    future = self._run(samples, timeout=self.timeout,
                                       await_response=self.await_response,
                                       wait=False)
                else:
                    values: dict[str, int] = {
                        f"data+{index}": sample for index, sample in enumerate(samples)
                    }
                    future = self._write("speaker", values, timeout=self.timeout,
                                         await_response=self.await_response, wait=False)
            except Exception:  # noqa: BLE001 — never let audio kill the caller
                log.warning("speaker write failed; dropping chunk", exc_info=True)
                self.dropped_chunks += 1
            else:
                self.written_chunks += 1
                if future is not None:
                    in_flight.append(future)
            sent += 1
            offset += chunk_bytes

        # Let the last writes land, without failing the caller if one is late.
        for future in in_flight:
            try:
                future.result(timeout=max(self.timeout, 2.0))
            except Exception:  # noqa: BLE001
                self.dropped_chunks += 1

    def _wait_for_space(self, *, min_free: int = 224, timeout: float = 0.05) -> None:
        """Best-effort flow control using the published `free_space_in_bytes`."""
        if self._read is None:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            free = self._read("free_space_in_bytes")
            if free is None or free >= min_free:
                return
            time.sleep(0.002)

    # ---------------- generators ----------------

    def tone(self, frequency: float = 261.626, duration: float = 0.5,
             amplitude: float = 0.25, function: str = SINE,
             *, fade: float = 0.01) -> None:
        """Play a test tone. `function` is sine/square/triangle/noise."""
        count = int(self.sample_rate * duration)
        fade_samples = max(1, int(self.sample_rate * fade))
        samples = []
        for index in range(count):
            phase = (index * frequency / self.sample_rate) % 1.0
            if function == SINE:
                value = math.sin(2 * math.pi * phase)
            elif function == SQUARE:
                value = 1.0 if phase < 0.5 else -1.0
            elif function == TRIANGLE:
                value = 4 * abs(phase - 0.5) - 1.0
            elif function == NOISE:
                value = random.uniform(-1, 1)
            else:
                raise ValueError(f"unknown waveform {function!r}")
            # Short fade in/out so the speaker does not click.
            if index < fade_samples:
                value *= index / fade_samples
            elif index > count - fade_samples:
                value *= (count - index) / fade_samples
            samples.append(value * amplitude)
        self.write_pcm(pcm16(samples))

    # ---------------- files and speech ----------------

    def play(self, path: str) -> None:
        """Play a WAV file."""
        with open(path, "rb") as handle:
            self.write_pcm(wav_to_pcm(handle.read(), self.sample_rate))

    def say(self, text: str, *, voice: str | None = None, speed: int = 130) -> None:
        """Speak `text` through the cube using espeak-ng (or espeak).

        Mirrors what the daemon's Python service did with pyttsx3, but
        without a sound server: espeak writes a WAV, we resample it to the
        firmware's rate and stream it.
        """
        engine = shutil.which("espeak-ng") or shutil.which("espeak")
        if engine is None:
            raise RuntimeError("say() needs espeak-ng or espeak installed")
        command = [engine, "-s", str(speed), "--stdout"]
        if voice:
            command += ["-v", voice]
        command.append(text)
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError(
                f"{engine} failed ({result.returncode}): "
                f"{result.stderr.decode(errors='replace').strip()}")
        self.write_pcm(wav_to_pcm(result.stdout, self.sample_rate))


