"""The daemon has to set the speaker's sample rate (no hardware).

`speaker.frequency` decides how fast the firmware drains its audio buffer: one
112-sample block every `112 / frequency` seconds. At the declared 32 kHz that is
286 blocks a second and the link serves about 258, so the buffer empties and the
speaker holds and repeats blocks. The value lives in the firmware and a power
cycle loses it, so the daemon writes it every time it starts — which is the only
reason a chicken-and-egg rebuild can produce smooth audio.
"""

from __future__ import annotations

from pylumicube.daemon import Daemon
from pylumicube.speaker import SPEAKER_RATE, Speaker


class FakeCube:
    """Just the two things `_apply_speaker_rate` touches."""

    def __init__(self) -> None:
        self.speaker = Speaker(lambda *a, **k: None)
        self.written: list[tuple[str, dict]] = []

    def write_named(self, node: str, values: dict) -> None:
        self.written.append((node, values))


def _daemon(rate: int | None) -> Daemon:
    daemon = Daemon(socket_path="/tmp/pylumicube-test.sock",
                    subscribe_except=(), speaker_rate=rate)
    daemon.cube = FakeCube()                     # no serial port in this test
    return daemon


def test_the_speaker_rate_is_written_at_startup() -> None:
    daemon = _daemon(SPEAKER_RATE)
    daemon._apply_speaker_rate()
    assert daemon.cube.written == [("speaker", {"frequency": SPEAKER_RATE})]


def test_the_driver_and_the_firmware_are_told_the_same_rate() -> None:
    """A mismatch plays at the wrong speed *and* starves the buffer."""
    daemon = _daemon(22050)
    daemon._apply_speaker_rate()
    assert daemon.cube.speaker.sample_rate == 22050
    assert daemon.cube.written[-1][1]["frequency"] == 22050


def test_the_default_is_the_rate_the_link_can_feed() -> None:
    assert SPEAKER_RATE == 22050
    #: 197 blocks a second, against the ~258 the link services and the 286
    #: that 32 kHz would need.
    assert 32000 / 112 > 258 > 22050 / 112


def test_no_rate_means_leave_the_firmware_alone() -> None:
    daemon = _daemon(None)
    daemon._apply_speaker_rate()
    assert daemon.cube.written == []


def test_a_failure_to_set_the_rate_is_not_fatal() -> None:
    """Audio is never worth failing the daemon over — it has a clock to serve."""
    daemon = _daemon(SPEAKER_RATE)

    def broken(node: str, values: dict) -> None:
        raise OSError("serial port went away")

    daemon.cube.write_named = broken
    daemon._apply_speaker_rate()                 # must not raise
