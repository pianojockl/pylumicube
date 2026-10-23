# pylumicube

Pure-Python driver for the [Abstract Foundry LumiCube](https://github.com/abstractfoundry).
Speaks the reverse-engineered wire protocol directly over `/dev/ttyAMA0`,
so you don't need the original Java `foundry-daemon` AppImage to drive
the panel. Protocol details are in [`PROTOCOL.md`](./PROTOCOL.md).

## Status

The reverse-engineered wire protocol is implemented end to end: the LED
matrix is driven directly from Python on real hardware, and upstream
LumiCube community scripts run unchanged via a compatibility shim
(verified on a LumiCube Advanced Kit, firmware shipped with AppImage
2.0.1). The roadmap below tracks progress towards full parity with the
Java `foundry-daemon`.

**Wire & transport**

- [x] **Link layer** — bidirectional PING/PONG handshake honouring the firmware's 256-PONG drain, `INITIALISE`/`INITIALISED`, 16-slot sliding window with retransmits.
- [x] **Node discovery** — passive harvest from `NODE_STATUS` broadcasts, plus a 3-stage dynamic node-ID allocator for cold-boot scenarios.
- [x] **Module discovery** — `GET_PREFERRED_NAME` to pick the `cube` base board over the `button_and_light_sensor` board.
- [x] **Schema discovery** — `ENUMERATE_FIELDS` walker (`utilities/snapshot_hardware.py`) that decodes in-line sub-dicts and resolves block floors via probing + binary search. See [`PROTOCOL.md`](./PROTOCOL.md) §4.5.1.
- [x] **Telemetry** — `SUBSCRIBE_DEFAULT_FIELDS` with heartbeat renewal and `PUBLISHED_FIELDS` decoding into a per-node cache (`pylumicube.telemetry`), self-healing when the firmware answers a walk probe with the wrong block. Exposed as `LumiCube.light_sensor` / `.buttons` / `.modules`.

**Hardware modules**

- [x] **LED matrix** — `SET_FIELDS` writes covering all 192 LEDs (3 frames). Exposed as the `lumicube-leds` CLI and `LumiCube.display`.
- [x] **Sensor board** — `light_sensor` (ambient light, RGB, proximity, gestures) and `buttons` (presses + counters) read from telemetry, with the discovered schema grouped into modules the way the daemon's `Namespace.build` does (`pylumicube.modules`). `env_sensor` / `imu` work the same way when the add-on board is present.
- [x] **Upstream-script compat shim** — `pylumicube.compat` recreates the foundry-daemon globals (`cube`, `display`, `hsv_colour`, `noise_*`, colour constants, etc.). `lumicube-run script.py` execs a community script in that namespace; display-only scripts (rainbow, rain, binary_clock, conways_game_of_life, autumn_scene, land_grab, lava_lamp, ripples, scrolling_clock — all under `scripts/original/`) run unchanged. Sensor scripts work too: `light_sensor` / `buttons` / `env_sensor` / `imu` are telemetry-backed and `pi` is implemented locally.
- [ ] **Microphone input** — telemetry plumbing is in place; still missing the audio capture path (`microphone.start_recording`, voice recognition, `get_frequency_buckets`).
- [x] **Secondary LCD screen** — `LumiCube.screen`: `set_pixel`, `set_pixels`, `draw_rectangle`, `clear`, `write_text`, `draw_image` (Pillow, `[screen]` extra), plus `rotation` / `invert_colours` / `resolution_scaling`. Recipes transcribed from `ScreenSetHalfRowMethod` / `ScreenDrawRectangleMethod` / `ScreenWriteTextMethod`; verified on hardware.
- [x] **Speaker** — `LumiCube.speaker`: `tone`, `play` (WAV), `say` (espeak-ng), `volume`, streaming 32 kHz mono PCM into `speaker.data` in the firmware's 112-sample chunks, pipelined and paced (no PulseAudio needed). Verified on hardware.

**Tooling**

- [ ] **FastAPI daemon** — replace the Java `foundry-daemon` with a Python REST API (Swagger-documented), shipped as a systemd unit.
- [ ] **Web frontend** for the daemon.

Protocol-side open questions are tracked in [`PROTOCOL.md`](./PROTOCOL.md) §7.

## Install

Requirements: Python **3.11+** and access to a serial device at
3 Mbaud (`/dev/ttyAMA0` on a Pi). Runtime dependencies are `pyserial`
and `opensimplex` (the latter only used by `compat.noise_2d/3d/4d`).

### Option A — `uv` (recommended for development)

[`uv`](https://github.com/astral-sh/uv) manages the virtualenv and
lockfile for you. From a fresh clone:

```bash
git clone https://github.com/chrislibuilds/pylumicube.git
cd pylumicube
uv sync                 # creates .venv and installs runtime deps
uv sync --extra dev     # adds pytest for the test suite
uv sync --extra extras  # adds requests for scripts/digital_clock.py (optional)
```

Run commands with `uv run …` so they pick up the project venv without
you activating it:

```bash
uv run pytest
uv run lumicube-leds all FF0000
uv run lumicube-run scripts/original/rainbow.py
```

### Option B — classic `venv` + `pip`

Works in any 3.11+ Python install. From a fresh clone:

```bash
git clone https://github.com/chrislibuilds/pylumicube.git
cd pylumicube
python3 -m venv .venv
source .venv/bin/activate            # PowerShell: .venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e '.[dev]'              # editable install + dev extras (pytest)
pip install -e '.[extras]'           # optional: deps for scripts/digital_clock.py
# Or combine the groups:
# pip install -e '.[dev,extras]'
```

After `activate`, the `lumicube-leds`, `lumicube-run`, and `pytest`
commands are all on your `PATH`:

```bash
pytest
lumicube-leds all FF0000
lumicube-run scripts/original/rainbow.py
```

### Option C — from PyPI (once released)

```bash
pip install pylumicube
```

### Pre-flight on the Raspberry Pi (optional)

A vanilla Raspberry Pi OS 13 (Bookworm/Trixie) install — including the
Lite / headless image without X or Wayland — has nothing using
`/dev/ttyAMA0`, so you can skip this section and go straight to the CLI.
The optional bits are:

- **Have you ever installed the Abstract Foundry `foundry-daemon`
  AppImage?** It holds `/dev/ttyAMA0` exclusively, so pylumicube will
  fail with `Resource busy` until you stop it:

  ```bash
  # Preferred: the user-scope service the AppImage installs.
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  systemctl --user stop foundry-daemon.service

  # Fallback if the user systemd isn't reachable (non-login SSH):
  pkill -f foundry-daemon
  ```

- **Bringing up a brand-new Pi image?** `utilities/check_pi_uart.sh`
  audits the boot config (UART enabled, serial console disabled, no
  daemon installed). Run it once to confirm the OS is ready to talk to
  the cube.

## CLI

Two console scripts ship with the package: `lumicube-leds` for direct
LED control and `lumicube-run` for executing upstream community scripts.
Prefix with `uv run` if you're using uv; activate the venv first if
you're using classic pip.

### `lumicube-leds` — direct LED control

One-shot LED writes that exit when done. Useful for diagnostics or for
piping from shell scripts.

```bash
# Solid colours
lumicube-leds all FF0000              # whole matrix red
lumicube-leds all 0000FF              # whole matrix blue
lumicube-leds off                     # turn every LED off

# Single LED by 0..191 index
lumicube-leds single 42 00FF00        # LED 42 = green

# Other options
lumicube-leds --port /dev/ttyAMA0 --debug all 0000FF
lumicube-leds --help                  # full flag list
```

Exit codes: `0` on success, `1` on any error (no cube found, bad
handshake, port busy, …). Pass `--debug` to see the link-layer logs.

### `lumicube-run` — run a community script

Recreates the foundry-daemon's pre-populated globals (`cube`, `display`,
`hsv_colour`, colour constants, `noise_2d/3d/4d`, `time`/`math`/`random`,
etc.) and `exec`s the given script in that namespace, so upstream
[community scripts](https://github.com/abstractfoundry/lumicube/tree/main/community-scripts)
work unchanged.

```bash
# Display-only scripts — fully working today (upstream community
# scripts live under scripts/original/).
lumicube-run scripts/original/rainbow.py
lumicube-run scripts/original/binary_clock.py
lumicube-run scripts/original/lava_lamp.py
lumicube-run scripts/original/scrolling_clock.py    # uses the built-in font

# Stop the script with Ctrl-C — the matrix is blanked on exit unless
# you pass --no-clear.
lumicube-run --no-clear scripts/original/rainbow.py
```

Sensor / audio / screen modules (`microphone`, `speaker`, `screen`,
`buttons`, `light_sensor`, `imu`, `env_sensor`, `pi`): `buttons`,
`light_sensor`, `env_sensor` and `imu` are served from real telemetry, and
`screen` / `speaker` are served by the native drivers — so
`screen.draw_image(...)`, `screen.write_text(...)`, `speaker.say(...)`,
`speaker.tone(...)` and `speaker.volume` work in upstream-style scripts.
`microphone` remains a warn-and-no-op stub (capturing audio would need the
Pi's own sound input), and `pi` is a thin wrapper over local sensors.
Anything a script pokes that is still a stub prints a one-time
`RuntimeWarning` per attribute and silently skips the call.

### Native-API scripts

The top-level `scripts/` directory ships three native-API examples:

- `scripts/digital_clock.py` — a "from scratch" clock that shows how to
  compute (x, y) → LED-index yourself and push frames via
  `Display.set_leds`.
- `scripts/plasma.py` — a 3D plasma / lava-lamp effect using 4D
  OpenSimplex noise. Ported from the upstream community-script
  `scripts/original/lava_lamp.py`, but with precomputed surface
  geometry, vectorised HSV→RGB, and the async display path so the next
  frame's compute overlaps the previous frame's wire push.
- `scripts/ambient_clock.py` — the community clock rewritten against the
  native API, with ambient-light auto-dimming.
- `scripts/screen_speaker_demo.py` — exercises the secondary LCD and the
  speaker (tones, then speech) for a quick hardware check.

The screen and speaker are plain attributes on the cube:

```python
from pylumicube import LumiCube

with LumiCube(telemetry=True,
              schema_node_names=("button_and_light_sensor",)) as cube:
    cube.walk_all_schemas()          # keys for every board
    cube.screen.clear(0x0000FF)
    cube.screen.write_text(20, 100, "hello", 3, 0xFFFFFF)
    cube.speaker.volume = 150
    cube.speaker.say("hello from pylumicube")
```

Two hardware notes: screen commands take ~100-200 ms each (so a full-screen
image is a tens-of-seconds operation), and audio needs the cube board's
telemetry switched off — it competes for the link (308 vs 56 chunks/s),
which is why the example subscribes to the sensor board only and walks the
cube board's schema separately.

Both are run as plain Python scripts:

```bash
uv run python scripts/digital_clock.py
uv run python scripts/plasma.py
# or, in an activated venv:
python scripts/digital_clock.py
python scripts/plasma.py
```

They also work under `lumicube-run` — the runner registers its open
cube via `pylumicube.compat.get_hosted_cube()`, and the scripts pick
that up through `open_or_use_hosted(port)` instead of opening a second
serial connection:

```bash
uv run lumicube-run scripts/digital_clock.py
uv run lumicube-run scripts/plasma.py
```

To make your own native-API script dual-mode-compatible, replace the
bare `with LumiCube(port) as cube:` with the helper:

```python
from pylumicube.compat import open_or_use_hosted

with open_or_use_hosted('/dev/ttyAMA0') as cube:
    cube.display.fill(0x00FF00)
```

Standalone, the helper opens and tears down a fresh `LumiCube(port)`.
Hosted by `lumicube-run`, it yields the runner's cube and leaves
teardown to the runner.

`digital_clock.py`'s optional weather feature uses `requests`, which is
part of the `extras` install group (`pip install -e '.[extras]'` or
`uv sync --extra extras`). `plasma.py` only needs `opensimplex` (a
base-install runtime dependency) plus `numpy` (already a transitive dep
through `opensimplex`).

To enable digital_clock's weather feature, copy the example config and
edit your OpenWeatherMap API key + city ID:

```bash
cp scripts/digital_clock_config.py.example scripts/digital_clock_config.py
$EDITOR scripts/digital_clock_config.py    # set OPENWEATHERMAP_API_KEY
```

`scripts/digital_clock_config.py` is listed in `.gitignore` so your key
never lands in version control. Without the config file the clock still
runs — it just skips the weather overlay.

## Library

`pylumicube` is usable as a library two ways: the native API for direct
field-level control, and the compat shim for upstream-style scripting.

### Native API

```python
from pylumicube import LumiCube

# The context manager opens the serial port, runs the handshake,
# discovers nodes, and closes everything cleanly on exit.
with LumiCube('/dev/ttyAMA0') as cube:
    cube.display.fill(0x00FF00)                          # whole matrix green
    cube.display.set_leds({0: 0xFF0000, 1: 0xFFFFFF})    # by 0..191 index
    cube.display.set_brightness(128)                     # 0..255
```

**Async by default.** `set_leds`, `fill`, `show`, and `set_brightness`
return immediately and push the write on a background thread, so a
frame loop can compute frame N+1 while frame N is still being pushed
over the wire. At most one frame is in flight (cube firmware constraint)
— the next async call blocks until the previous one is ACK'd, so a
script that calls `fill` faster than the wire can drain gets natural
backpressure. The cube is drained automatically on `cube.__exit__`.

If you want the old "block until ACK" behaviour for a particular write
(e.g. one-shot CLI scripts that need to know the write committed before
exiting, or to surface errors at the call site), pass `await_ack=True`:

```python
from pylumicube import LumiCube

with LumiCube() as cube:
    cube.display.fill(0xFF0000, await_ack=True)   # blocks; raises on error
    cube.display.flush(timeout=1.0)               # or: drain any async writes
```

### Sensors and telemetry

Nodes do not answer read requests — telemetry is push-based. Reading a
sensor means subscribing to the node's `PUBLISHED_FIELDS` stream, which
needs that node's field schema (discovered at runtime with
`ENUMERATE_FIELDS`). `LumiCube(telemetry=True)` does both, then groups the
fields into modules:

```python
from pylumicube import LumiCube

with LumiCube(telemetry=True) as cube:
    light = cube.light_sensor            # FieldModule or None
    print(sorted(light.field_names))     # ambient_light, red, green, blue, …
    ambient = light.wait_for('ambient_light', timeout=5)   # blocks
    print(ambient, light.red, light.within_proximity)

    if cube.buttons.top_pressed:
        print("top button", cube.buttons.top_pressed_count)

    # Diagnostics
    print(cube.telemetry.stats())        # frames, nodes, decode_errors, repairs
```

Notes:

- Telemetry is **off by default**, so display-only code keeps its old
  startup cost and behaviour.
- Only walk/subscribe the nodes you need —
  `LumiCube(telemetry=True, schema_node_names=("button_and_light_sensor",))`
  (a full walk of the `cube` base board is slow: ~100–180 ms per
  `ENUMERATE_FIELDS` round-trip).
- The schema walk is time-budgeted (`schema_budget`, default 15 s per
  node) and **self-healing**: a telemetry frame that references a block
  the walk missed triggers a targeted re-probe of that key. A truncated
  walk costs a couple of dropped frames, not a broken sensor.
- `ambient_light` is a 32-bit raw reading (hundreds to thousands in
  normal room light, much higher in daylight) — not 0..255. See
  [`PROTOCOL.md`](./PROTOCOL.md) §5.2 before writing a dimming curve.

### Compat-shim API (upstream-style)

```python
from pylumicube.compat import run_script

# One-shot: open the cube and run a community script in the
# foundry-daemon-compatible namespace.
run_script('scripts/original/binary_clock.py')
```

Or drive things yourself with the upstream helpers:

```python
from pylumicube import LumiCube
from pylumicube.compat import build_globals

with LumiCube() as cube:
    ns = build_globals(cube)
    display = ns['display']
    display.set_led(0, 0, ns['red'])                # (x, y) coords
    display.set_3d({(0, 0, 8): ns['cyan']}, show=True)  # 3D face coords
    display.scroll_text('Hello', ns['cyan'])        # uses built-in font
```

The compat namespace mirrors the upstream daemon's
[`api.txt`](https://github.com/abstractfoundry/lumicube/blob/main/official-documentation/api.txt):
`cube`, `display`, `buttons`, …, plus `hsv_colour`, `random_colour`,
`noise_2d/3d/4d`, `run_async`, and the colour constants.

## Testing

```bash
uv run pytest          # with uv
# or
pytest                 # with an activated venv
```

The suite is fully offline — no hardware required. It covers COBS,
CRC, framing, FlatDictionary, UAVCAN `messageId` encoding, an end-to-end
handshake against a fake serial emulator, and the compat shim's
coordinate mappings (pinned to the upstream daemon's formulae so the
shim can't silently drift).

## Project layout

```
src/pylumicube/
    constants.py         # protocol constants
    cobs.py              # in-place COBS (matches Java)
    crc.py               # CRC-16/CCITT-FALSE
    framing.py           # delimited frame builder + parser
    link.py              # SerialLink: handshake + sliding window
    uavcan.py            # messageId encoding/decoding
    transport.py         # Transport: transferIds, request/response
    flat_dictionary.py   # FlatDictionary TLV encoder/decoder
    metadata.py          # FieldSpec + hardcoded display schema
    discovery.py         # ENUMERATE_FIELDS schema walker
    telemetry.py         # SUBSCRIBE_DEFAULT_FIELDS + PUBLISHED_FIELDS cache
    modules.py           # module namespace (light_sensor, buttons, …)
    allocator.py         # 3-stage dynamic node-ID allocator
    node.py              # LumiCube top-level API
    display.py           # Display module helpers
    cli.py               # lumicube-leds CLI
    compat/
        runtime.py       # upstream-API shim: DisplayShim, stubs, build_globals, run_script
        font.py          # 5x7 ASCII bitmap font for scroll_text
        cli.py           # lumicube-run CLI

tests/                   # offline pytest suite
scripts/
    digital_clock.py     # native-API clock example
    plasma.py            # native-API 3D plasma effect
    original/            # the 17 upstream community scripts, run via lumicube-run
utilities/               # on-device debug + bring-up helpers
PROTOCOL.md              # canonical protocol spec
CHANGELOG.md             # versioned change history
LICENSE                  # GPL-3.0
```

## Scripts and utilities

`scripts/` is split between two kinds of programs:

- **Upstream LumiCube community / user scripts** under
  `scripts/original/` (e.g. `rainbow.py`, `binary_clock.py`,
  `lava_lamp.py`, `scrolling_clock.py`) — written against the
  foundry-daemon globals. Run with
  `lumicube-run scripts/original/<script.py>`.
- **Native-API examples** at the top level (`scripts/digital_clock.py`,
  `scripts/plasma.py`, `scripts/ambient_clock.py`) — use
  `pylumicube.LumiCube` directly and are launched as plain Python scripts
  (`python scripts/<script.py>`). They also work under `lumicube-run`
  via `pylumicube.compat.open_or_use_hosted`. The clock's optional
  weather feature depends on the `[extras]` install group; see the CLI
  section above.

`scripts/ambient_clock.py` is the ambient-light clock: hours on the left
panel, minutes on the right, with the brightness following
`light_sensor.ambient_light` through an auto-ranging curve
(`pylumicube.dimming`), so it dims in a dark room. Buttons scroll the
time (top), reboot (middle, `ALLOW_REBOOT`) and draw a CPU-temperature
chart on the secondary LCD (bottom — press again, or any other button, to
blank it). The speaker volume is pinned on startup (`SPEAKER_VOLUME`),
because the firmware powers up at full blast. Configuration goes in
`scripts/ambient_clock_config.py` (copy the `.example`) or in any file
passed as `--config`; run it with

```bash
python scripts/ambient_clock.py            # forever
python scripts/ambient_clock.py --once     # single frame, for testing
python scripts/ambient_clock.py --seconds 30 --debug
python scripts/ambient_clock.py --config ~/.config/pylumicube/clock.py
```

`deploy/` holds the systemd user unit and a step-by-step guide for running
the clock on the cube instead of the Java daemon (`deploy/README.md`).


`utilities/` contains helper tools used during bring-up and reverse-
engineering (require a connected cube):

- `snapshot_hardware.py` — walk every node's `ENUMERATE_FIELDS` schema
  and print one row per field. Useful as a reference dump.
- `query_key.py <key> [...]` — query a single field's metadata by
  absolute wire key.
- `dump_metadata.py`, `find_leds.py`, `probe_set_fields.py` — earlier
  debug helpers from the reverse-engineering work; kept for posterity.
- `check_pi_uart.sh` — verify a fresh Raspberry Pi OS image is ready to
  talk to the LumiCube via `/dev/ttyAMA0` (correct boot config, no
  serial console, no daemon installed, etc.).

## Compatibility

- Python 3.11 or newer.
- Linux (tested on Raspberry Pi OS Bookworm). Other POSIX platforms
  should work if you can open `/dev/ttyAMA0` at 3 Mbaud.
- LumiCube firmware as shipped with the AppImage 2.0.1 image. Older
  firmware revisions may use different field-key layouts.

## Contributing

Pull requests welcome. Please:
- Keep changes covered by `pytest tests/`.
- Update [`PROTOCOL.md`](./PROTOCOL.md) when you discover something new
  about the wire format.
- Note user-visible changes in [`CHANGELOG.md`](./CHANGELOG.md).

## License

GPL-3.0-or-later. See [`LICENSE`](./LICENSE).