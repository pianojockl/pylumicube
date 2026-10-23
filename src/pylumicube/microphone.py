"""Microphone: 16 kHz mono s16le capture from the cube's microphone.

The firmware publishes the microphone as a *block* field (`microphone.data`)
inside `PUBLISHED_FIELDS` — 112 samples per frame, 143 frames/s — so
capturing is a matter of being subscribed to the cube board and assembling
that block's keys in order. The format is the Java daemon's: it piped the
same values to PulseAudio as `--format=s16le --channels=1 --rate=16000`, and
16000 / 112 = 142.9 frames/s matches what the stream actually does.

One caveat worth knowing: **enabling the microphone means subscribing to the
cube board**, and the firmware publishes *every* field on that board at full
rate (its 192-LED block included). Measured on the cube: 30 service timeouts
in 30 seconds, display writes failing, and the daemon unable to complete a
handshake while it lasts — so it is refused by default, and the daemon needs
`--allow-cube-telemetry` to permit it. The capture path itself is correct
(16 kHz mono s16le, assembled from the published block), but the link cost
is the firmware's, not ours. Record while the clock is stopped.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

log = logging.getLogger(__name__)

#: The firmware's microphone rate (`--rate=16000` in the Java daemon).
SAMPLE_RATE = 16000

#: Samples per published frame (the `microphone.data` block's span).
CHUNK_SAMPLES = 112

#: Bytes per sample: s16le.
SAMPLE_BYTES = 2


class Microphone:
    """Records from the cube's microphone.

    `module` is the `microphone` `FieldModule`; `telemetry` is whatever store
    the cube has (`TelemetryStore` locally, `RemoteTelemetry` through the
    daemon) and `subscribe` is a no-argument callable that subscribes to the
    cube board, which is where the microphone lives.
    """

    def __init__(self, module: Any, *, telemetry: Any,
                 write: Callable[[str, Mapping[str, int]], None] | None = None,
                 subscribe: Callable[[], None] | None = None,
                 unsubscribe: Callable[[], None] | None = None,
                 sample_rate: int = SAMPLE_RATE,
                 max_chunks: int = 512) -> None:
        self._module = module
        self._telemetry = telemetry
        self._write = write
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self.sample_rate = int(sample_rate)
        self._lock = threading.Lock()
        self._chunks: deque[bytes] = deque(maxlen=max_chunks)
        self._field = None
        self._enabled = False
        self._registered = False
        self.frames = 0
        self.dropped = 0

    # ---------------- configuration ----------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def chunk_samples(self) -> int:
        """Samples per published frame (the block's span)."""
        return int(self._data_ref().spec.span) or CHUNK_SAMPLES

    def _data_ref(self):
        if self._field is None:
            self._field = self._module.ref("data")
        return self._field

    # ---------------- enable / disable ----------------

    def enable(self, *, subscribe: bool = True) -> None:
        """Turn the microphone on (and subscribe to the cube board)."""
        if subscribe and self._subscribe is not None:
            try:
                self._subscribe()
            except Exception:  # noqa: BLE001 — the write below still works
                log.warning("could not subscribe to the microphone's board",
                            exc_info=True)
        self._register()
        if self._enabled:
            return
        self._set_enabled(1)
        self._enabled = True
        log.info("microphone enabled (%d samples per frame at %d Hz)",
                 self.chunk_samples, self.sample_rate)

    def disable(self) -> None:
        """Turn the microphone off, and give the link back to the speaker."""
        if not self._enabled:
            return
        self._set_enabled(0)
        self._enabled = False
        self.unregister()
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001 — the flag write already stopped it
                log.warning("could not unsubscribe from the microphone's board",
                            exc_info=True)
        log.info("microphone disabled (%d frames captured)", self.frames)

    def _set_enabled(self, value: int) -> None:
        if self._write is None:
            raise RuntimeError("the microphone driver has no writer; "
                               "construct it with write=cube.write_named")
        self._write(self._module.name, {"enable": int(value)})

    # ---------------- frame handling ----------------

    def _handler(self, node_id: int, values: Mapping[int, int]) -> None:
        ref = self._data_ref()
        if node_id != ref.node_id:
            return
        block = {key - ref.key: value for key, value in values.items()
                 if ref.key <= key < ref.key + ref.spec.span}
        if not block:
            return
        chunk = bytearray()
        for offset in sorted(block):
            chunk += int(block[offset]).to_bytes(SAMPLE_BYTES, "little",
                                                 signed=False)
        with self._lock:
            if len(self._chunks) == self._chunks.maxlen:
                self.dropped += 1
            self._chunks.append(bytes(chunk))
            self.frames += 1

    def _register(self) -> None:
        if self._registered:
            return
        add = getattr(self._telemetry, "add_update_handler", None)
        if add is None:
            add = getattr(self._telemetry, "on_values", None)
        if add is None:
            log.warning("telemetry has no update hook; capture will be empty")
            return
        add(self._handler)
        self._registered = True

    def unregister(self) -> None:
        if not self._registered:
            return
        for name in ("remove_update_handler", "remove_values_handler"):
            remove = getattr(self._telemetry, name, None)
            if remove is not None:
                remove(self._handler)
                break
        self._registered = False

    # ---------------- capture ----------------

    def read_chunk(self, *, timeout: float | None = None) -> bytes | None:
        """Return the next frame of PCM, or None if none arrives in time."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if self._chunks:
                    return self._chunks.popleft()
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def drain(self) -> int:
        """Throw away anything buffered; returns how many frames were dropped."""
        with self._lock:
            count = len(self._chunks)
            self._chunks.clear()
        return count

    def capture(self, seconds: float, *, timeout: float = 5.0,
                enable: bool = True) -> bytes:
        """Record `seconds` of audio and return it as s16le PCM."""
        if enable:
            self.enable()
        self.drain()
        wanted = int(self.sample_rate * seconds)
        pcm = bytearray()
        deadline = time.monotonic() + seconds + timeout
        while len(pcm) < wanted * SAMPLE_BYTES:
            chunk = self.read_chunk(timeout=max(0.0, deadline - time.monotonic()))
            if chunk is None:
                log.warning("microphone stopped delivering frames after %.1fs",
                            len(pcm) / (SAMPLE_BYTES * self.sample_rate))
                break
            pcm += chunk
        return bytes(pcm[: wanted * SAMPLE_BYTES])

    def record(self, path: str, seconds: float, **kwargs: Any) -> int:
        """Record `seconds` to a WAV file; returns the sample count."""
        pcm = self.capture(seconds, **kwargs)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(SAMPLE_BYTES)
            handle.setframerate(self.sample_rate)
            handle.writeframes(pcm)
        samples = len(pcm) // SAMPLE_BYTES
        log.info("recorded %d samples (%.1fs) to %s", samples,
                 samples / self.sample_rate, path)
        return samples

    def chunks(self, seconds: float, **kwargs: Any) -> Iterable[bytes]:
        """Yield frames of PCM for `seconds` (for streaming consumers)."""
        if kwargs.get("enable", True):
            self.enable()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            chunk = self.read_chunk(timeout=0.5)
            if chunk is not None:
                yield chunk

    @property
    def stats(self) -> dict[str, int]:
        return {"frames": self.frames, "dropped": self.dropped,
                "buffered": len(self._chunks)}
