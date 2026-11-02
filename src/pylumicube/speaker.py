"""Speaker driver: stream PCM into the cube's speaker.

The cube's speaker is fed by writing samples into `speaker.data`, a run of
112 signed 16-bit fields (keys 128..239 on the cube board). The daemon
(`SpeakerThread.java`) captures its PulseAudio sink monitor with ``pamon
--format=s16le --channels=1 --rate=32000`` and pushes **three 112-sample
batches per read** — one batch is 224 bytes, i.e. 3.5 ms of audio at the
firmware's 32 kHz rate.

This implementation skips PulseAudio entirely: it generates or decodes PCM
itself, so ``tone()``, ``play()`` and ``say()`` work on a headless Pi with no
sound server.

How fast the buffer drains is `speaker.frequency`, and that turned out to be
the difference between working and not: one 112-sample block every
``112 / frequency`` seconds, so 32 kHz asks for 286 blocks a second and this
link serves about 258. The buffer empties, the firmware holds and repeats
blocks, and the result is the choppy, warbling sound no amount of careful
pacing could fix. `SPEAKER_RATE` is what the daemon runs it at instead.

Pacing follows the link rather than a clock (a clock pacer can only ever be
late, and lateness is what empties the buffer): the shipping profile keeps a
few blocks *outstanding* — submitted but not yet acknowledged — see
`PROFILES`. Overfilling is bounded the same way; frames that would arrive
late are dropped rather than queued, because audio latency matters more than
completeness (the daemon does the same: "Audio samples skipped due to backoff
signal").

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
from dataclasses import dataclass, field

from .constants import DROPPED

log = logging.getLogger(__name__)

#: The highest rate the firmware declares for `speaker.frequency`. Running the
#: speaker *at* it starves: the firmware services about 258 frames/s (measured),
#: and 32 kHz mono needs 286, so the ring drains however the sender is paced —
#: measured link round trip 510 ms and a choppy tone at 32 kHz, against 7 ms and
#: a clean one at 22050 Hz. See `SPEAKER_RATE`.
SAMPLE_RATE = 32000

#: The rate the speaker actually runs at. `speaker.frequency` is a settable
#: field, so the daemon writes this at startup; 22050 needs only 197 frames/s,
#: which leaves ~25 % headroom against the firmware's ~258, and it is espeak's
#: native rate — so `say()` resamples nothing at all. The audio band becomes
#: 11 kHz, which is beyond what this speaker reproduces anyway.
SPEAKER_RATE = 22050
#: Samples per batch: the `speaker.data` block's span.
CHUNK_SAMPLES = 112
#: Speaker volume field range (`speaker.volume`, 0..200 per api.txt).
MAX_VOLUME = 200
#: Default volume. The firmware powers up at full blast, which is far too
#: loud in a room; measured on this cube, speech is comfortable around 20.
DEFAULT_VOLUME = 40

#: Audio sent ahead of real time, in seconds, for the *clock*-paced profiles.
#: Superseded as the shipping strategy: pacing against the wall clock can only
#: ever be late, and it is late exactly when the previous ACK was slow, which
#: empties the buffer. `PROFILES["default"]` now paces on outstanding blocks
#: instead (`ahead`); this value remains here because the shootout's variants
#: still compare against it, and `"fine"`/`"deep"` are defined in terms of it.
LEAD_SECONDS = 0.021

#: Chunks sent back-to-back per wakeup, for the clock-paced profiles (the
#: outstanding-block pacer submits one at a time on purpose: batching hides
#: the ACK that says whether the link is keeping up). `time.sleep` on a busy
#: Pi overshoots by milliseconds, which is a tail of late chunks; sleeping once
#: per *group* means fewer timing events and more audio behind each one.
PACE_GROUP = 3

#: How far behind the schedule audio may fall before chunks are skipped.
#: Upstream's PulseAudio buffer played the same role: a hiccup delayed a
#: write but the samples were still there afterwards, so the audio came out
#: complete and slightly late. Dropping on the first late chunk (the old
#: behaviour) turned every hiccup into an audible gap. A second of lag is
#: inaudible in an announcement; beyond it something is badly wrong and
#: skipping ahead is the lesser evil.
MAX_LAG_SECONDS = 1.0

#: Voice used when a caller does not name one: an espeak language or voice
#: (``de``, ``de-DE``, ``en-gb``, ``german``, ``de+f3``). ``None`` = espeak's
#: own default. Set it with `set_speak_language()`; the daemon wires that to
#: ``--speak-language`` and the clock to its ``SPEAK_LANGUAGE`` setting.
SPEAK_LANGUAGE: str | None = None

#: ISO-639-3 codes people reach for and espeak rejects: it wants ISO-639-1
#: (``de``), so ``deu`` fails with "The specified espeak-ng voice does not
#: exist". Anything not listed here falls back to its first two letters.
LANGUAGE_ALIASES = {
    "deu": "de", "eng": "en", "fra": "fr", "fre": "fr", "nld": "nl",
    "dut": "nl", "spa": "es", "ita": "it", "por": "pt", "rus": "ru",
    "jpn": "ja", "zho": "zh", "chi": "zh", "ell": "el", "gre": "el",
    "pol": "pl", "swe": "sv", "dan": "da", "fin": "fi", "ces": "cs",
    "cze": "cs", "hun": "hu", "tur": "tr", "ukr": "uk", "ron": "ro",
    "rum": "ro", "hrv": "hr", "slk": "sk", "slo": "sk", "cat": "ca",
}

_voices: set[str] | None = None
_languages: list[str] | None = None


def available_voices() -> set[str]:
    """The voices espeak has here (lower-cased), listed once and cached.

    espeak's own listing is the only honest source: it accepts a language
    (``de``), a locale-ish code (``de-DE``) and a voice name (``german``),
    but rejects ISO-639-3 (``deu``).
    """
    global _voices
    if _voices is None:
        _voices = set()
        engine = shutil.which("espeak-ng") or shutil.which("espeak")
        if engine is not None:
            try:
                result = subprocess.run([engine, "--voices"], timeout=10,
                                        capture_output=True, text=True, check=False)
                for line in result.stdout.splitlines()[1:]:
                    columns = line.split()
                    if len(columns) >= 4:
                        _voices.add(columns[1].lower())     # language: de
                        _voices.add(columns[3].lower())     # name: german
            except (OSError, subprocess.SubprocessError):   # pragma: no cover
                log.debug("could not list espeak voices", exc_info=True)
    return _voices


def available_languages() -> list[str]:
    """The language codes espeak has here, sorted, without the voice names.

    This is what a picker wants: `de`, `en`, `en-gb`, ... — not the ~100
    voice names (`german`, `mb-de1`) that `available_voices()` also carries
    for resolving a caller's request.
    """
    global _languages
    if _languages is None:
        engine = shutil.which("espeak-ng") or shutil.which("espeak")
        codes: set[str] = set()
        if engine is not None:
            try:
                result = subprocess.run([engine, "--voices"], timeout=10,
                                        capture_output=True, text=True, check=False)
                for line in result.stdout.splitlines()[1:]:
                    columns = line.split()
                    if len(columns) >= 4:
                        code = columns[1].lower()
                        codes.add(code)
                        # Also offer the bare language (espeak lists `en-us`
                        # and `en-gb` but not `en`, which is what it uses by
                        # default and what `-v en` means).
                        base = code.split("-")[0]
                        if 2 <= len(base) <= 3 and base.isalpha():
                            codes.add(base)
            except (OSError, subprocess.SubprocessError):  # pragma: no cover
                log.debug("could not list espeak languages", exc_info=True)
        _languages = sorted(codes)
    return list(_languages)


def resolve_voice(name: str | None) -> str | None:
    """Turn a requested language/voice into one espeak will accept.

    Unknown names return ``None`` (espeak's default) *after* logging, because
    the alternatives are both worse: raising would break an announcement, and
    passing the name through would have espeak read German in an English
    accent, or refuse with an error mid-sentence.
    """
    if not name:
        return None
    wanted = str(name).strip()
    voices = available_voices()
    if not voices:                          # no espeak here: let it decide
        return wanted
    if wanted.lower() in voices:
        return wanted
    alias = LANGUAGE_ALIASES.get(wanted.lower(), wanted[:2].lower())
    if alias in voices:
        log.info("espeak voice %r -> %r", wanted, alias)
        return alias
    # A bare language code is valid when espeak has *any* voice for it, even
    # though its listing only names the variants: `en` is absent while
    # `en-us`/`en-gb` are present, yet `espeak -v en` works and is the
    # built-in default. Rejecting it made the default look unknown.
    lowered = wanted.lower()
    if 2 <= len(lowered) <= 3 and lowered.isalpha():
        if any(voice.startswith(lowered + "-") for voice in voices):
            log.info("espeak language %r (variants installed)", wanted)
            return wanted
    log.warning("no espeak voice for %r; using the default", wanted)
    return None


def set_speak_language(name: str | None) -> str | None:
    """Set the default voice for `say()`; returns what it resolved to.

    An unknown name leaves the previous default in place, so a typo in a
    config file cannot quietly change how everything sounds.
    """
    global SPEAK_LANGUAGE
    if name is None or str(name).strip() == "":
        SPEAK_LANGUAGE = None
    else:
        resolved = resolve_voice(name)
        if resolved is not None:
            SPEAK_LANGUAGE = resolved
    return SPEAK_LANGUAGE


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


#: How many ms of inter-chunk gap counts as "the buffer had certainly
#: emptied". Measured: 4032 bytes = 18 blocks = 63 ms at 22050 Hz (56 ms of
#: that accepted in one burst, draining at 224 bytes per 3.5 ms), so a send
#: gap beyond ~40 ms is a hole the firmware cannot cover, and the sound says
#: so long before the numbers do.
GAP_UNDERRUN_MS = 40.0


@dataclass(frozen=True)
class PacingProfile:
    """One way of feeding the firmware's audio buffer.

    The knobs the smoothness shootout (`scripts/speaker_shootout.py`) varies.
    `None` means "keep the Speaker's own setting", so the `"default"` profile
    is exactly the shipping behaviour and every variant is one field away from
    it. The daemon's `tone`/`say`/`play` ops take `profile=<name>`, so the same
    code path can be auditioned without editing files — which is the point:
    which of these sounds smoothest is a question for the ear, not for
    reasoning about jitter.
    """

    #: Seconds of audio sent ahead of the schedule (`None` = the Speaker's).
    lead: float | None = None
    #: Chunks sent per wakeup (`None` = the Speaker's).
    pace_group: int | None = None
    #: `False` paces nothing at all: push as fast as the link accepts, and let
    #: the firmware's buffer be the only buffer.
    realtime: bool | None = None
    #: "firmware" lets `speaker.volume` scale the samples, which is what the
    #: cube has always done. "software" pins the firmware at `MAX_VOLUME` and
    #: pre-scales the PCM instead: the same loudness with the firmware's
    #: integer scaling (and its quantisation of a 20/200 signal) out of the
    #: path.
    volume_mode: str = "firmware"
    #: Random +/- seconds added to each chunk's due time. A perfectly regular
    #: cadence can lock onto the receiver's own cadence (the firmware appears
    #: to service audio in groups of three blocks, i.e. every 10.5 ms); a
    #: jittered one cannot, so this distinguishes "our schedule resonates with
    #: theirs" from "their playout is at fault".
    jitter: float = 0.0
    #: Sample rate the *content* is generated at and the chunks are paced to.
    #: `None` = the Speaker's own (`SPEAKER_RATE`, 22050 in the daemon).
    #: The firmware takes at most one 112-sample block per block period, and the
    #: measured service rate on this cube is ~258 frames/s — *below* the 286/s
    #: that 32 kHz mono needs, which is why the ring drains however the sender
    #: is paced. At 16 kHz the same audio needs only 143 frames/s, which leaves
    #: real headroom (at the cost of an 8 kHz audio band — ample for speech on
    #: this speaker).
    rate: int | None = None
    #: Keep this many chunks *outstanding* (submitted but not yet ACKed) and
    #: pace on that instead of on the wall clock — the way upstream's speaker
    #: thread did, where `pamon` handed it bytes as fast as PulseAudio produced
    #: them and a deep pipe absorbed everything. Delivery then runs at whatever
    #: the link can carry (measured ~400 chunks/s against 286 needed), which
    #: keeps the firmware's ring stocked instead of hovering at empty.
    #: `None` = wall-clock pacing (the original behaviour).
    ahead: int | None = None

    def effective_rate(self, default: int) -> int:
        """The rate this profile will actually pace at.

        Content has to be generated or resampled to this rate, or a rate
        profile plays back slow: `say()` asks espeak for a 22.05 kHz WAV and
        resamples once to whatever the stream is paced at.
        """
        return self.rate if self.rate is not None else default


PROFILES: dict[str, PacingProfile] = {
    # What ships: pace on what the link has *taken* (8 chunks outstanding, about
    # 40 ms of audio in flight) instead of on the wall clock. A wall-clock pacer
    # can only ever be late, so the firmware's ring sits at empty and every
    # hiccup is an audible hold-and-repeat; keeping a fixed depth outstanding
    # keeps the ring stocked and needs no knowledge of its level.
    "default": PacingProfile(ahead=8),
    # Finer pacing: one chunk per 3.5 ms wakeup, smaller lead — steadier
    # delivery, more scheduling events that can each overshoot.
    "fine": PacingProfile(lead=0.012, pace_group=1),
    # Deeper lead: hand the firmware ~45 ms (nearly its whole buffer) so it
    # never runs dry, at the risk of overflowing it instead.
    "deep": PacingProfile(lead=0.045, pace_group=3),
    # A quarter second in hand. The flood measurement shows the firmware takes
    # 4 s of audio in ~1 s, so it has a real buffer — this keeps that buffer
    # stocked instead of merely non-empty.
    "buffered": PacingProfile(lead=0.25, pace_group=3),
    # --- round 3: the lead has to cover the *link's* pipeline too ------------
    # Measured with the firmware's own gauge (ref_data/tools/audio_ring_probe.py):
    # the ring holds 4032 bytes = 18 blocks = 63 ms, and it accepts a 56 ms
    # burst without complaint. But up to 16 frames (56 ms of audio) can also
    # sit in *this* link's 16-slot window before ever reaching the firmware —
    # so a lead below ~56 ms is swallowed by the window and the ring is left
    # empty, which is exactly what the phone recording showed (a block held
    # and repeated every so often). A lead of ~90 ms fills the window *and*
    # leaves ~35 ms in the ring.
    "pipeline": PacingProfile(lead=0.090, pace_group=1),
    "pipeline-shallow": PacingProfile(lead=0.060, pace_group=1),
    "pipeline-deep": PacingProfile(lead=0.120, pace_group=1),
    # --- round 4: nothing sent ahead, nothing duplicated --------------------
    # One block per 3.5 ms with no lead at all, so the retransmit timer is the
    # only thing left that can disturb the stream (see the round-4 variants).
    "trickle": PacingProfile(lead=0.0, pace_group=1),
    # --- round 5: paced by what the link has taken, not by the wall clock ----
    # Upstream's arrangement (a deep pipe feeding the link as fast as it would
    # go) is reproduced by keeping N chunks outstanding and pushing whenever
    # there is room. Measured: the link carries ~400 chunks/s, the audio needs
    # 286, so the surplus is what keeps the firmware's 63 ms ring stocked.
    "pump-8": PacingProfile(ahead=8),
    "pump-10": PacingProfile(ahead=10),
    "pump-12": PacingProfile(ahead=12),
    "pump-14": PacingProfile(ahead=14),
    "pump-16": PacingProfile(ahead=16),
    "pump-24": PacingProfile(ahead=24),
    "pump-32": PacingProfile(ahead=32),
    # --- round 6: saturated window, bounded by one ring of lead --------------
    # `ahead` fills the 16-slot window (56 ms of audio in flight); `lead` stops
    # the overfeed at roughly what the firmware's 63 ms ring can hold, so the
    # ring is stocked and nothing is discarded.
    "pump-cap-16-60": PacingProfile(ahead=16, lead=0.060),
    "pump-cap-20-45": PacingProfile(ahead=20, lead=0.045),
    "pump-cap-16-90": PacingProfile(ahead=16, lead=0.090),
    # --- round 9: half the rate, twice the headroom -------------------------
    # The firmware services ~258 frames/s; 32 kHz mono needs 286, so the ring
    # drains no matter how the sender is paced. 16 kHz needs 143 frames/s.
    # Needs `speaker.frequency` set to 16000 on the cube as well.
    "rate16k": PacingProfile(rate=16000, ahead=8),
    "rate16k-lead": PacingProfile(rate=16000, lead=0.060),
    "rate16k-pump": PacingProfile(rate=16000, ahead=16),
    "rate22k": PacingProfile(rate=22050, ahead=8),
    # --- round 4b: deliberate jitter, to break any cadence lock -------------
    "jittered": PacingProfile(lead=0.0, pace_group=1, jitter=0.003),
    "jittered-pipeline": PacingProfile(lead=0.090, pace_group=1, jitter=0.003),
    # No pacing at all — feed it as fast as the ACK window allows.
    "flood": PacingProfile(realtime=False),
    # Same stream, but loudness is applied here instead of in the firmware.
    "soft-volume": PacingProfile(volume_mode="software"),
}


def resolve_profile(profile: "str | PacingProfile | None") -> PacingProfile:
    """Look a profile up by name (`None` = `"default"`)."""
    if profile is None:
        return PROFILES["default"]
    if isinstance(profile, PacingProfile):
        return profile
    try:
        return PROFILES[str(profile)]
    except KeyError:
        raise ValueError(f"unknown pacing profile {profile!r}; "
                         f"known: {sorted(PROFILES)}") from None


def profile_name(profile: PacingProfile) -> str:
    """The registry name of a profile, for reports (`"custom"` if unnamed)."""
    for name, candidate in PROFILES.items():
        if candidate is profile:
            return name
    return "custom"


def _percentiles(values: "list[float]") -> dict[str, float]:
    """p50/p95/p99/max of a series, rounded for a report."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    ordered = sorted(values)

    def at(fraction: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * fraction))], 2)

    return {"p50": at(0.5), "p95": at(0.95), "p99": at(0.99),
            "max": round(ordered[-1], 2)}


@dataclass
class StreamReport:
    """What one `write_pcm` actually did.

    Dropped chunks were already counted; these add the timing, because a
    stutter is a *delay* rather than a loss — the audio arrives complete and
    too late for the firmware's 47 ms buffer. `gaps_ms` is the one to watch:
    an inter-chunk gap over `GAP_UNDERRUN_MS` (counted in `long_gaps`) is a
    gap the DAC cannot have covered.
    """

    profile: str = "default"
    chunks: int = 0
    dropped: int = 0
    audio_seconds: float = 0.0
    wall_seconds: float = 0.0
    #: How long the trailing wait for the last in-flight frames took. If this
    #: is not near zero, the link — not the pacer — decided when the stream
    #: ended, and the audio was delivered late.
    tail_wait_seconds: float = 0.0
    #: Inter-chunk send gaps, ms.
    gaps_ms: dict[str, float] = field(default_factory=dict)
    #: How far behind the real-time schedule each chunk went out, ms.
    behind_ms: dict[str, float] = field(default_factory=dict)
    #: Chunks whose arrival gap exceeded `GAP_UNDERRUN_MS`.
    long_gaps: int = 0
    #: The widest send gaps with the time they happened, `[[seconds, ms], ...]`
    #: — the sender's own view of a stall, which is what a periodic hiccup looks
    #: like from this side (the phone recording says ~0.2 s, i.e. 5 Hz, and this
    #: is how to find which periodic task in the process it is).
    slow_sends: list[list[float]] = field(default_factory=list)
    #: How long the *submission* of each chunk took (p50/p95/p99/max, ms). If a
    #: send gap is a slow body rather than a late wakeup, it shows up here.
    body_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "chunks": self.chunks,
            "dropped": self.dropped,
            "audio_seconds": round(self.audio_seconds, 3),
            "wall_seconds": round(self.wall_seconds, 3),
            "tail_wait_seconds": round(self.tail_wait_seconds, 3),
            "gaps_ms": dict(self.gaps_ms),
            "behind_ms": dict(self.behind_ms),
            "long_gaps": self.long_gaps,
            "slow_sends": [list(item) for item in self.slow_sends],
            "body_ms": dict(self.body_ms),
            "error": self.error,
        }


def scale_pcm(pcm: bytes, gain: float) -> bytes:
    """Multiply mono s16le samples by `gain` (clipped), for software volume."""
    if gain >= 1.0:
        return pcm
    if gain <= 0.0:
        return b"\x00" * len(pcm)
    try:                                        # numpy is optional
        import numpy as np
    except ImportError:                         # pragma: no cover - Pi has it
        samples = array.array("h")
        samples.frombytes(pcm)
        for index, value in enumerate(samples):
            samples[index] = int(value * gain)
        return samples.tobytes()
    block = np.frombuffer(pcm, dtype="<i2").astype("float32") * gain
    return np.clip(block, -32768, 32767).astype("<i2").tobytes()


class Speaker:
    """Streams audio to the cube's speaker.

    `write` is the cube's name-based field writer (`LumiCube.write_named`);
    `read` is an optional reader for a published field (used for
    `free_space_in_bytes` flow control when telemetry is available).
    """

    def __init__(self, write: Callable[..., None], *,
                 read: Callable[[str], int | None] | None = None,
                 run: Callable[..., object] | None = None,
                 sample_rate: int = SPEAKER_RATE,
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
        #: Timing of the last stream (see `StreamReport`) and how many have
        #: run — what `lumicube-do stats` and the shootout read.
        self.last_stream: StreamReport | None = None
        self.stream_count = 0

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

    def write_pcm(self, pcm: bytes, *, realtime: bool | None = None,
                  profile: "str | PacingProfile | None" = None) -> None:
        """Stream mono s16le PCM to the speaker.

        With `realtime` (the default when `pace` is set) writes are paced to
        the sample rate, so a caller can hand over a whole clip and get it
        played at the right speed.

        Chunks are **pipelined**: a single write costs ~50-100 ms of latency
        on this hardware, but the link sustains ~380 frames/s when several
        are in flight (measured), which is comfortably above the 286
        chunks/s that 32 kHz mono needs.

        The pacing is what makes it sound right, and it is worth being
        precise about, because the firmware's own buffer is only about
        47 ms (≈3 KB): pacing *exactly* to real time leaves that buffer
        empty, so every scheduling hiccup is an audible gap. So:

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

        `profile` picks one of `PROFILES` (or a `PacingProfile`) for this
        stream only, so variants can be auditioned without changing anything
        global. The stream's timing ends up in `self.last_stream`.
        """
        chosen = resolve_profile(profile)
        restore: int | None = None
        if chosen.volume_mode == "software" and 0 < self._volume < MAX_VOLUME:
            # Apply the loudness here and hand the firmware a full-scale
            # signal, so its own 20/200 scaling — and the quantisation that
            # comes with it — is out of the path.
            pcm = scale_pcm(pcm, self._volume / MAX_VOLUME)
            restore = self._volume
            self._write("speaker", {"volume": MAX_VOLUME},
                        await_response=self.await_response)
        try:
            self._stream(pcm, realtime=realtime, profile=chosen)
        finally:
            if restore is not None:
                self._write("speaker", {"volume": restore},
                            await_response=self.await_response)

    def _stream(self, pcm: bytes, *, realtime: bool | None,
                profile: PacingProfile) -> None:
        """The loop behind `write_pcm`; the rationale is in its docstring."""
        self._stopped = False
        lead = self.lead if profile.lead is None else float(profile.lead)
        pace_group = (self.pace_group if profile.pace_group is None
                      else max(1, int(profile.pace_group)))
        jitter = max(0.0, float(profile.jitter))
        ahead = None if profile.ahead is None else max(1, int(profile.ahead))
        if realtime is None:
            realtime = (self.pace if profile.realtime is None
                        else bool(profile.realtime))
        chunk_bytes = self.chunk_samples * 2
        chunk_duration = self.chunk_samples / profile.effective_rate(self.sample_rate)
        started = time.monotonic()
        sent = 0
        offset = 0
        total = len(pcm)
        in_flight: deque[Future] = deque()
        report = StreamReport(profile=profile_name(profile))
        dropped_before = self.dropped_chunks
        gaps: list[float] = []          # ms between consecutive sends
        behind: list[float] = []        # ms late against the schedule
        slow: list[list[float]] = []    # the widest gaps, with their time
        body: list[float] = []          # ms spent submitting each chunk
        last_send: float | None = None
        last_progress = time.monotonic()   # last time a frame was ACKed

        while offset < total - 1:
            if self._stopped:
                break
            chunk = pcm[offset : offset + chunk_bytes]
            if len(chunk) < chunk_bytes:
                chunk = chunk + b"\x00" * (chunk_bytes - len(chunk))   # pad
            if ahead:
                # Pace by what the link has taken, not by the clock: keep at
                # most `ahead` chunks unacknowledged and push whenever there is
                # room. This is upstream's shape (a deep pipe feeding the link
                # as fast as it would go), and the reason to want it is that
                # the firmware's ring is only 63 ms: a wall-clock pacer that is
                # *ever* late can never catch up, so the ring sits at empty and
                # every hiccup is an audible hold-and-repeat.
                #
                # Saturation alone would overfeed — measured at 1.4x real time,
                # which the firmware can only answer by discarding — so the
                # clock still applies as an upper bound (`lead`): the window is
                # kept full, the ring gets what fits, and nothing is thrown
                # away beyond it.
                while True:
                    while in_flight and in_flight[0].done():
                        finished = in_flight.popleft()
                        last_progress = time.monotonic()
                        try:
                            if finished.result() == DROPPED:
                                self.dropped_chunks += 1
                        except Exception:  # noqa: BLE001 — a failed frame is lost
                            self.dropped_chunks += 1
                    room = len(in_flight) < ahead
                    if room and realtime:
                        lead_for_gate = 0.0 if lead is None else lead
                        due = (started + sent * chunk_duration - lead_for_gate)
                        if jitter:
                            due += random.uniform(-jitter, jitter)
                        if time.monotonic() < due:
                            room = False
                    if room or self._stopped:
                        break
                    if time.monotonic() - last_progress > 2.0:
                        # A frame whose future never resolves (a wedged window
                        # the stall watchdog has not cleared) must not hold the
                        # stream for ever: this used to wedge the daemon's whole
                        # audio path behind `audio_lock` until a restart.
                        log.warning("no audio frame acknowledged for 2s; "
                                    "abandoning the rest of the stream")
                        self.dropped_chunks += max(0, total // chunk_bytes - sent)
                        report.error = "stalled waiting for acknowledgements"
                        break
                    time.sleep(0.0005)
                if self._stopped or report.error:
                    break
            elif realtime:
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
                due = schedule - lead
                if jitter:
                    due += random.uniform(-jitter, jitter)
                if sent % pace_group == 0 and now < due:
                    # Sleep once per group: the schedule is absolute, so a
                    # group that arrives late is caught up by the next one,
                    # but there are `pace_group` times fewer sleeps to
                    # overshoot. The lead never tightens this: it is a head
                    # start, not an earlier deadline.
                    time.sleep(due - now)
                self._wait_for_space()
            if not ahead:
                while in_flight and in_flight[0].done():
                    finished = in_flight.popleft()
                    # The link may have dropped the frame instead of queueing it
                    # (bounded queue, oldest droppable frame first). That is a
                    # hole in the audio, and it used to be invisible: the future
                    # was discarded without being read, so the loss never showed
                    # up in `dropped_chunks`.
                    try:
                        if finished.result() == DROPPED:
                            self.dropped_chunks += 1
                    except Exception:  # noqa: BLE001 — a failed frame is a lost frame
                        self.dropped_chunks += 1
                if len(in_flight) >= self.max_in_flight:
                    # Do not block. The future is only for error reporting and
                    # audio does not need it; waiting turns a busy link into a
                    # stall, which is exactly what the firmware cannot absorb.
                    # (In `ahead` mode the loop above has already waited for
                    # room, so a future is never discarded there.)
                    in_flight.popleft()

            samples = array.array("h")
            samples.frombytes(chunk)
            body_started = time.monotonic()
            try:
                if self._run is not None:
                    # Classified as audio: the link's queue is ordered by class,
                    # so a speech frame is never stuck behind display frames —
                    # which is what makes speech stutter when other traffic is
                    # on the link. Droppable because a late audio chunk is
                    # worthless anyway (the schedule is absolute).
                    future = self._run(samples, timeout=self.timeout,
                                       await_response=self.await_response,
                                       wait=False, label="audio",
                                       droppable=True)
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
            stamp = time.monotonic()
            body.append((stamp - body_started) * 1000.0)
            if last_send is not None:
                gap = (stamp - last_send) * 1000.0
                gaps.append(gap)
                if gap > 6.0:
                    # Keep the outliers with their time: a *periodic* hiccup is
                    # a periodic stall in this list, and its period names the
                    # task responsible.
                    slow.append([round(stamp - started, 3), round(gap, 1)])
                    if len(slow) > 200:
                        slow.sort(key=lambda item: item[1], reverse=True)
                        del slow[100:]
            last_send = stamp
            if realtime:
                behind.append(max(0.0, (stamp - (started + sent * chunk_duration))
                                  * 1000.0))
            sent += 1
            offset += chunk_bytes

        # Let the last writes land, without failing the caller if one is late.
        tail_started = time.monotonic()
        for future in in_flight:
            try:
                future.result(timeout=max(self.timeout, 2.0))
            except Exception:  # noqa: BLE001
                self.dropped_chunks += 1
        report.tail_wait_seconds = time.monotonic() - tail_started
        report.chunks = sent
        report.audio_seconds = sent * chunk_duration
        report.wall_seconds = time.monotonic() - started
        report.dropped = self.dropped_chunks - dropped_before
        report.gaps_ms = _percentiles(gaps)
        report.behind_ms = _percentiles(behind)
        report.long_gaps = sum(1 for gap in gaps if gap > GAP_UNDERRUN_MS)
        slow.sort(key=lambda item: item[1], reverse=True)
        report.slow_sends = slow[:12]
        report.body_ms = _percentiles(body)
        self.last_stream = report
        self.stream_count += 1
        if report.long_gaps or report.tail_wait_seconds > 0.1:
            # Worth saying out loud: these are the two shapes a stutter takes
            # (a gap the buffer could not cover, or a link that ended the
            # stream rather than the pacer).
            log.info("stream %r: %d chunks in %.2fs, %d gaps > %.0fms, "
                     "tail wait %.2fs, gaps p95 %.1fms max %.1fms",
                     report.profile, report.chunks, report.wall_seconds,
                     report.long_gaps, GAP_UNDERRUN_MS,
                     report.tail_wait_seconds, report.gaps_ms["p95"],
                     report.gaps_ms["max"])

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
             *, fade: float = 0.01,
             profile: "str | PacingProfile | None" = None) -> None:
        """Play a test tone. `function` is sine/square/triangle/noise."""
        rate = resolve_profile(profile).effective_rate(self.sample_rate)
        count = int(rate * duration)
        fade_samples = max(1, int(rate * fade))
        samples = []
        for index in range(count):
            phase = (index * frequency / rate) % 1.0
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
        self.write_pcm(pcm16(samples), profile=profile)

    # ---------------- files and speech ----------------

    def play(self, path: str, *,
             profile: "str | PacingProfile | None" = None) -> None:
        """Play a WAV file."""
        with open(path, "rb") as handle:
            data = handle.read()
        rate = resolve_profile(profile).effective_rate(self.sample_rate)
        self.write_pcm(wav_to_pcm(data, rate), profile=profile)

    def say(self, text: str, *, voice: str | None = None, speed: int = 130,
            profile: "str | PacingProfile | None" = None) -> None:
        """Speak `text` through the cube using espeak-ng (or espeak).

        Mirrors what the daemon's Python service did with pyttsx3, but
        without a sound server: espeak writes a WAV, we resample it to the
        firmware's rate and stream it.
        """
        engine = shutil.which("espeak-ng") or shutil.which("espeak")
        if engine is None:
            raise RuntimeError("say() needs espeak-ng or espeak installed")
        command = [engine, "-s", str(speed), "--stdout"]
        chosen = resolve_voice(voice) if voice else SPEAK_LANGUAGE
        if chosen:
            command += ["-v", chosen]
        command.append(text)
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError(
                f"{engine} failed ({result.returncode}): "
                f"{result.stderr.decode(errors='replace').strip()}")
        rate = resolve_profile(profile).effective_rate(self.sample_rate)
        self.write_pcm(wav_to_pcm(result.stdout, rate), profile=profile)


