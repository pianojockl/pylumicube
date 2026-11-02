#!/usr/bin/env python3
"""Smoothness shootout: play one continuous tone through each pacing variant.

The cube's speaker is fed by streaming 112-sample chunks into `speaker.data`,
and *how* those chunks are released decides whether the sound is smooth or
choppy. That is not something to settle by reasoning about jitter: the
firmware's buffer is ~47 ms, the link's round trip is tens of milliseconds,
and the difference between "smooth" and "stuttering" is a few milliseconds of
delivery.

So this plays the *same* tone through each variant in turn and prints what
each one did. Listen, then say which number sounded smoothest; the numbers
are there to explain the verdict, not to replace it.

    python scripts/speaker_shootout.py                 # all variants, 4 s each
    python scripts/speaker_shootout.py --silent        # same, no sound
    python scripts/speaker_shootout.py --variants 1,3,5
    python scripts/speaker_shootout.py --speech "the quick brown fox jumps"

Each variant is a pacing `profile` (`pylumicube.speaker.PROFILES`) plus, for
some, a daemon setting (`ingress`, `retransmit_ms`, the CPU governor). Every
knob is restored afterwards, so running this changes nothing permanently.

Reading the report line:

    chunks/wall     how much audio went out, and how long that took. Audio
                    that takes longer to send than to play cannot be smooth.
    gaps p95/max    between chunk sends, ms. A gap wider than ~40 ms is one
                    the firmware's buffer cannot cover — an audible hole.
                    `long` counts those.
    behind          how late each chunk went out against the real-time
                    schedule, ms.
    tail            how long the wait for the last frames took. If the link,
                    not the pacer, decides when a stream ends, this is big.
    link p50/p95    the link's own submit-to-ACK latency for audio frames.

Needs a running daemon (`lumicube-daemon`), because that is what owns the
port and the audio path.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pylumicube.client import CubeClient, daemon_available  # noqa: E402
from pylumicube.control import ControlError  # noqa: E402
from pylumicube.speaker import SPEAKER_RATE  # noqa: E402

log = logging.getLogger("speaker_shootout")

#: The tone the variants share. 1 kHz sits in the speaker's comfortable
#: range and is steady enough that a gap or a roughness is obvious.
DEFAULT_TONE_HZ = 1000.0
DEFAULT_SECONDS = 4.0
DEFAULT_AMPLITUDE = 0.25
#: Space between variants, so the previous one is clearly over.
PAUSE_SECONDS = 1.5
#: Marker blips before a tone: N of them, N being the variant's number.
BLIP_HZ = 1600.0
BLIP_SECONDS = 0.12
BLIP_GAP = 0.06
#: The sentence `--speech` uses when it is given no text.
DEFAULT_SPEECH = "the quick brown fox jumps over the lazy dog"

GOVERNOR_PATH = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"


@dataclass(frozen=True)
class Variant:
    """One way of feeding the firmware's audio buffer."""

    key: str
    title: str
    profile: str = "default"
    #: Daemon settings applied for this variant only (see `ControlServer`'s
    #: `config` op): `ingress`, `retransmit_ms`, `switch_interval`.
    config: dict[str, Any] = field(default_factory=dict)
    #: CPU governor to hold for this variant ("performance", "ondemand", ...).
    governor: str | None = None
    #: Extra seconds to wait after this variant before the next one starts.
    #: The flood test hands the firmware seconds of audio at once; without a
    #: drain the next variant's tone would queue behind it.
    drain: float = 0.0
    #: Which round of the investigation this variant belongs to (`--round`):
    #: 1 was the broad sweep, 3 the "fill the link's pipeline too" round.
    round: int = 1
    #: Register writes applied for this variant and undone afterwards, e.g.
    #: `{"speaker": {"frequency": 16000}}`. `speaker.frequency` is the knob that
    #: matters: the firmware consumes one 112-sample block per block period, so
    #: halving the rate halves the frames it has to service per second.
    fields: dict[str, dict[str, int]] = field(default_factory=dict)


#: The variants, in the order they are played. `1` is what shipped when the
#: shootout was written, so every round still has a baseline to compare with.
VARIANTS: tuple[Variant, ...] = (
    Variant("1", "as shipped (lead 21 ms, 3 chunks/wakeup)"),
    Variant("2", "fine pacing (lead 12 ms, 1 chunk/wakeup)", profile="fine"),
    Variant("3", "deep lead (45 ms, 3 chunks/wakeup)", profile="deep"),
    Variant("4", "flood (unpaced: as fast as the link accepts)", profile="flood",
            drain=3.0),
    Variant("5", "deferred ingress (telemetry decoded off the serial thread)",
            config={"ingress": "deferred"}),
    Variant("6", "software volume (firmware at 200, samples pre-scaled)",
            profile="soft-volume"),
    Variant("7", "slow retransmit (50 ms instead of 12.5 ms)",
            config={"retransmit_ms": 50.0}),
    Variant("8", "performance CPU governor + 1 ms GIL interval",
            config={"switch_interval": 0.001}, governor="performance"),
    Variant("9", "long lead (a quarter second in hand)", profile="buffered"),
    # --- round 3: the lead must also cover the link's 16-frame window --------
    Variant("10", "shipped (baseline for this round)", round=3),
    Variant("11", "lead 60 ms, one block per 3.5 ms", profile="pipeline-shallow",
            config={"retransmit_ms": 50.0}, round=3),
    Variant("12", "lead 90 ms, one block per 3.5 ms", profile="pipeline",
            config={"retransmit_ms": 50.0}, round=3),
    Variant("13", "lead 120 ms, one block per 3.5 ms", profile="pipeline-deep",
            config={"retransmit_ms": 50.0}, round=3),
    Variant("14", "lead 90 ms, default retransmit (isolates that knob)",
            profile="pipeline", round=3),
    # --- round 4: the retransmit timer against a ~45 ms ACK latency ----------
    # Measured: a lone frame's ACK takes 54-88 ms once the link is idle, and
    # ~45 ms under load. The shipped timer re-sends after 12.5 ms, i.e. three
    # or four times per frame — duplicate traffic that spends the egress byte
    # budget before fresh audio gets it. These variants sweep the timer.
    Variant("20", "trickle, retransmit 12.5 ms (shipped timer)", profile="trickle",
            config={"retransmit_ms": 12.5}, round=4),
    Variant("21", "trickle, retransmit 50 ms", profile="trickle",
            config={"retransmit_ms": 50.0}, round=4),
    Variant("22", "trickle, retransmit 150 ms", profile="trickle",
            config={"retransmit_ms": 150.0}, round=4),
    Variant("23", "trickle, retransmit 300 ms", profile="trickle",
            config={"retransmit_ms": 300.0}, round=4),
    Variant("24", "lead 90 ms + retransmit 150 ms", profile="pipeline",
            config={"retransmit_ms": 150.0}, round=4),
    # --- round 4b: if a regular cadence is locking onto the firmware's ------
    Variant("25", "jittered pacing (+/-3 ms) + retransmit 150 ms",
            profile="jittered", config={"retransmit_ms": 150.0}, round=4),
    Variant("26", "jittered pacing + lead 90 ms + retransmit 150 ms",
            profile="jittered-pipeline", config={"retransmit_ms": 150.0}, round=4),
    # --- round 5: upstream's architecture — pace on what the link has taken --
    # The Java daemon's speaker thread read `pamon` as fast as PulseAudio
    # produced bytes: delivery ran at whatever the link could carry, with the
    # pipe as the reserve, and it sounds clean on this hardware (measured:
    # ±95 Hz sidebands -45 dB vs our -32..-41 dB). These variants reproduce
    # that shape by bounding the *outstanding* chunks instead of the clock.
    Variant("30", "shipped (baseline for this round)", round=5),
    Variant("31", "pump: 8 chunks outstanding", profile="pump-8",
            config={"retransmit_ms": 60.0}, round=5),
    Variant("32", "pump: 16 chunks outstanding", profile="pump-16",
            config={"retransmit_ms": 60.0}, round=5),
    Variant("33", "pump: 24 chunks outstanding", profile="pump-24",
            config={"retransmit_ms": 60.0}, round=5),
    Variant("34", "pump: 32 chunks outstanding", profile="pump-32",
            config={"retransmit_ms": 60.0}, round=5),
    Variant("35", "pump 16, shipped retransmit timer", profile="pump-16", round=5),
    # --- round 6: fill the window, cap the lead at one ring ------------------
    Variant("40", "shipped (baseline for this round)", round=6),
    Variant("41", "pump 16 + 60 ms lead cap", profile="pump-cap-16-60",
            config={"retransmit_ms": 60.0}, round=6),
    Variant("42", "pump 20 + 45 ms lead cap", profile="pump-cap-20-45",
            config={"retransmit_ms": 60.0}, round=6),
    Variant("43", "pump 16 + 90 ms lead cap", profile="pump-cap-16-90",
            config={"retransmit_ms": 60.0}, round=6),
    Variant("44", "pump 16 uncapped (overfeeds; reference)",
            profile="pump-16", config={"retransmit_ms": 60.0}, round=6),
    # --- round 8: the firmware rejects whole windows; recover fast or wedge ---
    # Probe log: "egress window stalled for 1.0s at sequence 77 (last ACK 0, 16
    # in flight)" — the firmware stopped acknowledging entirely, our in-order
    # window then blocked for the watchdog's second (a second of silence per
    # event), and it repeated. So: keep the pipeline fed (ahead) *and* retry the
    # blocking head quickly, rather than waiting 12.5-60 ms as most frames do.
    Variant("60", "shipped (baseline for this round)", round=8),
    Variant("61", "pump 16 + head retry 8 ms", profile="pump-16",
            config={"retransmit_ms": 8.0}, round=8),
    Variant("62", "pump 16 + head retry 5 ms", profile="pump-16",
            config={"retransmit_ms": 5.0}, round=8),
    Variant("63", "pump 24 + head retry 8 ms", profile="pump-24",
            config={"retransmit_ms": 8.0}, round=8),
    Variant("64", "pump 16 + 60 ms cap + head retry 8 ms",
            profile="pump-cap-16-60", config={"retransmit_ms": 8.0}, round=8),
    # --- round 9: change the rate the firmware consumes at -------------------
    # Measured: the firmware services ~258 speaker frames/s, and 32 kHz mono
    # needs 286 — so the ring drains whatever the sender does. At 16 kHz the
    # same audio needs 143 frames/s, and the measured ACK rate is 147/s: the
    # first configuration with real margin. The pitch tells us whether the
    # firmware honours `speaker.frequency` (if it does not, a 1 kHz tone will
    # sound an octave low).
    Variant("70", "32 kHz, shipped (baseline for this round)", round=9),
    Variant("71", "16 kHz, 8 outstanding", profile="rate16k",
            fields={"speaker": {"frequency": 16000}}, round=9),
    Variant("72", "22 kHz, 8 outstanding", profile="rate22k",
            fields={"speaker": {"frequency": 22050}}, round=9),
    Variant("73", "16 kHz, 16 outstanding", profile="rate16k-pump",
            fields={"speaker": {"frequency": 16000}}, round=9),
)


# --------------------------------------------------------------------- helpers


#: Settings the daemon's `config` op accepts, i.e. everything a variant may
#: change and everything this script restores.
TUNING_KEYS = ("ingress", "retransmit_ms", "switch_interval")

LOCK_PATH = os.path.join(tempfile.gettempdir(), "pylumicube-speaker-shootout.lock")


def _take_lock() -> int:
    """Refuse to run twice at once (two runs interleave their settings)."""
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit(
            f"speaker_shootout: another run holds {LOCK_PATH} — finish it, or "
            f"`pkill -f '[s]peaker_shootout'` if it is stale") from None
    return fd


def _restore(client: CubeClient, baseline: dict[str, Any]) -> None:
    """Put the daemon's own settings back (never one variant's into another)."""
    restore = {key: value for key, value in baseline.items() if key in TUNING_KEYS}
    if restore:
        client.configure(**restore)


def _variant_list(spec: str | None, round_number: int | None = None) -> list[Variant]:
    """The variants named by `--variants 1,3,5`, or one round's worth."""
    pool = [variant for variant in VARIANTS
            if round_number is None or variant.round == round_number]
    if not spec:
        return pool
    wanted = {item.strip() for item in spec.split(",") if item.strip()}
    chosen = [variant for variant in VARIANTS if variant.key in wanted]
    missing = wanted - {variant.key for variant in chosen}
    if missing:
        raise SystemExit(
            f"speaker_shootout: no variant {', '.join(sorted(missing))} "
            f"(have {', '.join(v.key for v in VARIANTS)})")
    return chosen


def _read_governor() -> str | None:
    try:
        with open(GOVERNOR_PATH) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _set_governor(name: str) -> bool:
    """Set every CPU's governor; True if it worked (`sudo -n` is used)."""
    cpus = [f"/sys/devices/system/cpu/cpu{index}/cpufreq/scaling_governor"
            for index in range(len(os.sched_getaffinity(0)))]
    targets = [path for path in cpus if os.path.exists(path)]
    if not targets:
        return False
    script = " && ".join(f"echo {name} | tee {path} > /dev/null" for path in targets)
    result = subprocess.run(["sudo", "-n", "sh", "-c", script],
                            capture_output=True, check=False)
    if result.returncode != 0:
        log.warning("could not set the governor to %r (%s)", name,
                    result.stderr.decode(errors="replace").strip()
                    or f"exit {result.returncode}")
        return False
    return True


def _blips(client: CubeClient, count: int) -> None:
    """N short beeps — the variant's number, for when the screen is away."""
    for _ in range(count):
        client.tone(BLIP_HZ, BLIP_SECONDS, amplitude=0.2)
        time.sleep(BLIP_GAP)


def _gap_summary(stream: dict[str, Any]) -> str:
    gaps = stream.get("gaps_ms") or {}
    behind = stream.get("behind_ms") or {}
    return (f"gaps p95 {gaps.get('p95', 0):5.1f} max {gaps.get('max', 0):5.1f} ms "
            f"({stream.get('long_gaps', 0)} over 40 ms)  "
            f"behind p95 {behind.get('p95', 0):5.1f} max {behind.get('max', 0):5.1f} ms")


def _timing_summary(stream: dict[str, Any]) -> str:
    return (f"{stream.get('chunks', 0):5d} chunks / "
            f"{stream.get('audio_seconds', 0.0):4.2f} s audio in "
            f"{stream.get('wall_seconds', 0.0):5.2f} s wall, "
            f"{stream.get('dropped', 0)} dropped, "
            f"tail {stream.get('tail_wait_seconds', 0.0):4.2f} s")


def _link_summary(stats: dict[str, Any], deltas: dict[str, float] | None = None) -> str:
    link = stats.get("link") or {}
    audio = (link.get("latency_by_label") or {}).get("audio") or {}
    ingress = link.get("ingress") or {}
    bits = []
    if audio:
        bits.append(f"link p50 {audio.get('p50', 0):4.1f} "
                    f"p90 {audio.get('p90', 0):5.1f} max {audio.get('max', 0):5.1f} ms")
    if deltas:
        bits.append(f"sent {deltas.get('submitted', 0):5.0f} "
                    f"resent {deltas.get('resends', 0):5.0f} "
                    f"stale {deltas.get('ack_stale', 0):5.0f} "
                    f"stalls {deltas.get('resets', 0):3.0f}")
    bits.append(f"retransmit {link.get('retransmit_ms', 0):4.1f} ms")
    bits.append(f"ingress {ingress.get('mode', '?')}")
    return "  ".join(bits)


def _what_is_playing(args: argparse.Namespace, speech: str | None) -> str:
    if speech:
        return f"speech {speech!r}"
    if args.silent:
        return f"silent {args.tone:.0f} Hz for {args.seconds:g} s"
    return f"{args.tone:.0f} Hz for {args.seconds:g} s"


# ------------------------------------------------------------------- the run


def _play_variant(client: CubeClient, variant: Variant, *, seconds: float,
                  tone_hz: float, amplitude: float, speech: str | None,
                  blips: bool, index: int = 0) -> dict[str, Any]:
    """Apply the variant's settings and play its tone; return the reply."""
    if variant.config:
        client.configure(**variant.config)
    for module, values in variant.fields.items():
        # e.g. `speaker.frequency`: the rate the firmware drains its ring at.
        client.write_named(module, values)
    governor_changed = False
    if variant.governor:
        governor_changed = _set_governor(variant.governor)
    try:
        if blips:
            # Markers count the variant's *position in this run* (1..9), not
            # its number: `--variants 40,41` used to play forty blips, which is
            # seven seconds of beeps before the first tone and useless to count.
            _blips(client, min(9, index + 1))
        if speech:
            return client.say(speech, profile=variant.profile)
        return client.tone(tone_hz, seconds, amplitude=amplitude,
                           profile=variant.profile)
    finally:
        if governor_changed:
            _set_governor("ondemand")


def run(args: argparse.Namespace) -> int:
    round_number = None if not args.round_number else args.round_number
    variants = _variant_list(args.variants, round_number)
    if not daemon_available(args.socket):
        print("speaker_shootout: no daemon is listening — start "
              "`lumicube-daemon` first (it owns the audio path).", flush=True)
        return 1
    lock_fd = _take_lock()                     # released when we exit

    amplitude = 0.0 if args.silent else args.amplitude
    speech = args.speech
    if speech == "":
        speech = DEFAULT_SPEECH

    with CubeClient(args.socket) as client:
        baseline = client.configure().get("config") or {}
        if args.volume is not None:
            client.control.request("write", module="speaker",
                                   values={"volume": int(args.volume)})
        governor_before = _read_governor()
        print(flush=True)
        print(f"speaker shootout: {len(variants)} variant(s), "
              f"{'silent (measuring only)' if args.silent else 'listening'}",
              flush=True)
        print(f"playing {_what_is_playing(args, speech)}; "
              f"volume {args.volume if args.volume is not None else 'unchanged'}; "
              f"governor {governor_before}", flush=True)
        print(f"daemon baseline: {baseline}", flush=True)
        print(flush=True)

        results: list[tuple[Variant, dict[str, Any]]] = []
        previous_link: dict[str, Any] = {}
        try:
            for index, variant in enumerate(variants):
                print(f"=== {index + 1}/{len(variants)}  {variant.key} — "
                      f"{variant.title} ===", flush=True)
                _restore(client, baseline)      # each variant starts clean
                try:
                    reply = _play_variant(
                        client, variant, seconds=args.seconds, tone_hz=args.tone,
                        amplitude=amplitude, speech=speech,
                        blips=not args.no_blips, index=index)
                except ControlError as exc:
                    print(f"    failed: {exc}", flush=True)
                    results.append((variant, {}))
                    continue
                stream = reply.get("stream") or {}
                try:
                    stats = client.stats()
                except ControlError:
                    stats = {}
                link = stats.get("link") or {}
                deltas = {key: link.get(key, 0) - previous_link.get(key, 0)
                          for key in ("submitted", "resends", "completed",
                                      "ack_stale", "resets")}
                previous_link = dict(link)
                print(f"    {_timing_summary(stream)}", flush=True)
                print(f"    {_gap_summary(stream)}", flush=True)
                print(f"    {_link_summary(stats, deltas) if stats else 'no stats'}",
                      flush=True)
                results.append((variant, stream))
                if index != len(variants) - 1:
                    time.sleep(PAUSE_SECONDS + variant.drain)
                if variant.fields.get("speaker", {}).get("frequency"):
                    # Put the firmware's rate back: the next variant (and the
                    # clock afterwards) expects 32 kHz.
                    client.write_named("speaker", {"frequency": SPEAKER_RATE})
        finally:
            _restore(client, baseline)
            if governor_before:
                _set_governor(governor_before)
            try:
                # Whatever happened, the firmware must be left at 32 kHz.
                client.write_named("speaker", {"frequency": SPEAKER_RATE})
            except ControlError:
                pass
            os.close(lock_fd)

    print(flush=True)
    print("--- summary " + "-" * 58, flush=True)
    print(f"{'#':3} {'variant':52} {'audio/wall':>11} {'p95 gap':>8} "
          f"{'max gap':>8} {'long':>5}", flush=True)
    for variant, stream in results:
        audio = stream.get("audio_seconds", 0.0)
        wall = stream.get("wall_seconds", 0.0)
        gaps = stream.get("gaps_ms") or {}
        print(f"{variant.key:3} {variant.title[:52]:52} "
              f"{audio:5.2f}/{wall:5.2f} {gaps.get('p95', 0):8.1f} "
              f"{gaps.get('max', 0):8.1f} {stream.get('long_gaps', 0):5d}",
              flush=True)
    print(flush=True)
    print("Which number sounded smoothest (and which second best)?", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="speaker_shootout",
        description="Play one continuous tone through each audio pacing "
                    "variant and report what each did.")
    parser.add_argument("--variants", default=None,
                        help="comma-separated variant numbers (default: --round)")
    parser.add_argument("--round", dest="round_number", type=int, default=8,
                        help="which round of variants to play (default 6; 1 was "
                             "the broad sweep, 3 the leads, 4 the retransmit "
                             "timer; use 0 for every round)")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help="tone length (default 4)")
    parser.add_argument("--tone", type=float, default=DEFAULT_TONE_HZ,
                        help="tone frequency in Hz (default 1000)")
    parser.add_argument("--amplitude", type=float, default=DEFAULT_AMPLITUDE,
                        help="tone amplitude 0..1 (default 0.25)")
    parser.add_argument("--volume", type=int, default=None,
                        help="speaker volume 0..200 for the test "
                             "(default: leave the daemon's alone)")
    parser.add_argument("--silent", action="store_true",
                        help="send the same PCM with amplitude 0: measure the "
                             "stream without making a sound")
    parser.add_argument("--speech", nargs="?", const="", default=None,
                        metavar="TEXT",
                        help="speak TEXT through each variant instead of a tone "
                             f"(default text: {DEFAULT_SPEECH!r})")
    parser.add_argument("--no-blips", action="store_true",
                        help="skip the marker blips that announce each variant")
    parser.add_argument("--socket", default=None, help="control socket path")
    parser.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
