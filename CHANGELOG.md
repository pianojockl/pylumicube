# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Wire-protocol findings (vs. the Java daemon)
- **Metadata field keys are absolute, and the walk must follow the firmware's
  table order.** The response's leading skip is the field's key counted from
  0 (the daemon's parser starts its accumulator at 0), not a distance from
  the queried key; and `min_value`/`max_value` are length-prefixed
  (`UINT, size = 0`). Reading the skip as relative produced wrong keys —
  `screen.rectangle_x` 3315 instead of 2048, `speaker.data` 241 instead of
  128, `rdp_level` 60014 instead of 60000 — and an "overlapping blocks"
  mystery that did not exist. With both fixed, the daemon's
  `(key0, key1)` walk finds **49 fields on the cube board (was 16)** in
  ~3.5 s, including the whole `screen` and `speaker` modules. See
  PROTOCOL.md §4.5.1 and §5.1.
- **The link drops replies**, so a query that goes unanswered must be
  retried and distinguished from "the firmware answered with no field"
  (the end of the table). One lost reply used to truncate the schema and
  silently remove whole modules; preferred-name discovery retries too.
- **Service requests cost ~50-105 ms of latency regardless of payload**
  (a tiny request and a 224-byte speaker frame are the same), but pipelined
  writes sustain 300-600 frames/s. Anything at streaming rates must not
  wait for a ServiceResponse; the drivers use the link-level ACK instead.
- **Telemetry confirmed end to end on hardware.** `SUBSCRIBE_DEFAULT_FIELDS`
  takes a 2-byte payload (`duration = cadence * 2`, `bandwidth = 255`) and
  the node answers with incremental `PUBLISHED_FIELDS` snapshots; there is
  **no `GET_FIELDS` service**, so reads are push-only. See PROTOCOL.md §4.6.
- **The light sensor is 32-bit.** `ambient_light`, `red`, `green`, `blue`,
  `num_gestures` and `num_times_within_proximity` are `UINT` size 4 on
  `button_and_light_sensor` (measured 2026-10-23, PROTOCOL.md §5.2). The
  commonly assumed 0..255 range is wrong, which matters for any dimming
  curve.
- **Node ids are not stable across boots** — captures have seen the
  sensor board at 125 and at 124. Resolve nodes by `GET_PREFERRED_NAME`.
- `ENUMERATE_FIELDS` omits `gettable`/`settable`/`idempotent` (absent
  means "not reported", not false) and (when set) reports
  `min_value`/`max_value`/`units`. See PROTOCOL.md §5.2.
- **Subscribing to the cube board costs link throughput** (~308 -> ~56
  speaker chunks/s), so audio playback runs with only the sensor board
  subscribed and walks the cube board's schema separately
  (`LumiCube.walk_all_schemas()`).

### Added
- **Secondary LCD screen** (`pylumicube.screen.Screen`, `LumiCube.screen`):
  `draw_rectangle`, `set_pixel`, `clear`, `set_half_row`/`set_pixels` (the
  firmware's pixel-window stream), `write_text` and `draw_image` (Pillow,
  via the new `[screen]` extra), plus `rotation`, `invert_colours` and
  `resolution_scaling`. Colours convert to the panel's RGB565 exactly as
  upstream does, and the firmware's limits are enforced (80 pixels per
  batch, 160 per call, 127 characters).
- **Speaker** (`pylumicube.speaker.Speaker`, `LumiCube.speaker`): `tone`
  (sine/square/triangle/noise), `play` (WAV), `say` (espeak-ng or espeak),
  `volume` and `write_pcm`, streaming mono s16le at 32 kHz in the
  firmware's 112-sample chunks. Pipelined and paced to the wall clock, with
  chunk drops rather than queueing when the link falls behind; optional
  `free_space_in_bytes` flow control. No PulseAudio, no numpy (the
  resampler falls back to pure Python).
- **`LumiCube.write_fields` / `write_named` / `write_run`**: raw, name-based
  and run-based field writes, with `await_response` and `wait` controls for
  peripherals that answer slowly or not at all. `write_named` addresses
  homogeneous runs as `"pixel_data_stream+3"` / `"text+0"`.
- **`LumiCube.walk_all_schemas()`** to discover every node's field keys
  without subscribing to its telemetry stream.
- **`flat_dictionary.serialise_run`** for homogeneous runs.
- **`scripts/screen_speaker_demo.py`** — draws on the LCD, plays tones and
  speaks, for a quick hardware sanity check.
- **`[screen]` extra** (`pip install -e '.[screen]'`) for Pillow.
- The compat shim now serves `screen` and `speaker` from the native drivers
  (`ScreenShim` / `SpeakerShim`), including upstream's convention that a
  relative image/audio path resolves against `~/Desktop`.
- **`deploy/`** — a systemd user unit (`pylumicube-clock.service`) and a
  step-by-step guide for running the clock on the cube instead of the Java
  daemon: venv layout, settings in `~/.config/pylumicube/clock.py`, the
  enable/disable dance, and how to roll back.
- **`Speaker` starts at half volume** (`DEFAULT_VOLUME = 100`, not
  `MAX_VOLUME`): the firmware powers up at full blast, which is far too
  loud in a room. `Speaker(volume=...)`, the demo's `--volume` and the
  clock's `SPEAKER_VOLUME` all override it.
- **`LumiCube.enable_telemetry(subscribe_except=...)`** — walk a node's
  field keys without subscribing to its telemetry stream, the configuration
  that makes audio work; the compat shim uses it so a script that reads
  sensors *and* speaks does not lose 3/4 of its audio throughput.
- **The clock's bottom button draws a CPU-temperature chart on the LCD**,
  with `TEMP_SAMPLE_SECONDS` / `TEMP_HISTORY` / `CHART_REFRESH` settings,
  instead of only logging the reading. It toggles (any other button blanks
  the LCD) and refreshes while shown — the community script it replaces
  looped forever on that button and never came back to the clock.
- **`ambient_clock.py --config PATH`**, so the service's settings can live
  outside the git checkout; `STATUS_INTERVAL` logs
  `ambient <reading> -> brightness <value> (range low..high)` so the
  dimming curve can be watched in the journal.

### Fixed
- **Metadata discovery read field keys as relative offsets**, which produced
  wrong keys for every block the walk reached via a gap (`screen`,
  `speaker`, the `60000+` system fields) and made the walker probe and
  binary-search to compensate. The leading skip is absolute; the walk now
  follows the firmware's table order like the daemon does, finding 49 fields
  on the cube board instead of 16, in about a quarter of the time.
- **`min_value`/`max_value` are length-prefixed**, so parsing a field that
  reports them (e.g. `display.brightness`, max 100) no longer derails the
  rest of the sub-dictionary.
- **A dropped reply no longer truncates the schema.** The walk retries
  unanswered queries and only gives up after several consecutive silences,
  and preferred-name discovery retries too.
- The module namespace is rebuilt when a new schema lands, so walking a
  board after subscribing to a different one still exposes its modules.

- **Ambient-light dimming** (`pylumicube.dimming`): `BrightnessCurve` maps
  a raw `light_sensor.ambient_light` reading to a 0..1 brightness with an
  auto-ranging, decaying range (so it learns the room), optional gamma and
  output smoothing; `scale_colour()` dims a 0xRRGGBB colour per channel so
  the matrix's 8-bit precision survives (unlike the 0..100 global
  brightness field).
- **`scripts/ambient_clock.py`** — a native-API rewrite of the classic
  community clock: hours/minutes panels with the original 3x5 digits and
  hue shimmer, brightness driven by the ambient sensor, button handling
  (scroll / reboot / CPU-temperature chart on the LCD), hourly chime, and a
  config file (`scripts/ambient_clock_config.py.example`). Verified on
  hardware: renders at the right brightness for the measured room light,
  adapts its range, and survives the firmware's system-block decode quirks.
- **Native coordinate and text rendering**: `Display.set_leds_xy`,
  `set_led`, `set_all`, `set_panel`, `set_3d` and `scroll_text`, plus the
  coordinate mappings (`xy_to_index`, `xyz_to_index`, `led_index`) that
  used to live only in the compat shim. The shim now delegates, so there
  is a single implementation. The 5x7 font moved to `pylumicube.font`
  (`compat.font` re-exports it).
- `open_or_use_hosted()` forwards `LumiCube` keyword arguments, so a
  native script can ask for telemetry and still run under `lumicube-run`.

  heartbeat renewal, `PUBLISHED_FIELDS` decoding into a per-node value
  cache, `get`/`snapshot`/`wait_for`/`last_update`, and self-healing
  schemas — a frame referencing an unknown key schedules a targeted
  re-probe (off the link's I/O thread) instead of being dropped forever.
  Undecodable frames are counted per node and never kill the link.
- **Runtime schema discovery** (`pylumicube.discovery`): the
  `ENUMERATE_FIELDS` walker (previously only in
  `utilities/snapshot_hardware.py`, which now imports it) as a library
  function, with the well-known-key pass, the `rel == k` disambiguation,
  the backward binary search, and a wall-clock budget.
- **Module namespace** (`pylumicube.modules`): groups discovered fields
  into `light_sensor`, `buttons`, `env_sensor`, `imu`, … the way the Java
  daemon's `Namespace.build` does, including the `buttons` →
  `system_button` rename on the cube base board. `FieldModule` gives
  `light_sensor.ambient_light`-style reads from the telemetry cache.
- **`LumiCube` telemetry API**: `LumiCube(telemetry=True)`,
  `enable_telemetry(node_ids=…, node_names=…)`, `discover_schema()`,
  `schemas`, `modules`, `module(name)`, `light_sensor`, `buttons`,
  `node_id_for_name()`. Off by default, so display-only users see no
  change.
- **Compat shim parity for the sensor board**: `light_sensor`, `buttons`,
  `env_sensor` and `imu` are now real telemetry-backed modules instead of
  warn-and-no-op stubs. They are wired lazily (a `_LazyModule` in
  `build_globals`), so display-only community scripts still start without
  touching telemetry. `pi` is now implemented locally
  (`cpu_temp`, `cpu_percent`, `ram_percent_used`, `disk_percent`,
  `ip_address`), matching `global_service/service.py`.
  `screen`, `speaker` and `microphone` remain stubs (see the README).
- `flat_dictionary.deserialise()` now decodes 4-byte floats and fixed-size
  `UTF8_CHAR` blocks, skips variable-length strings, and raises a typed
  `UndecodablePayload` carrying the offending key.
- `FieldSpec` carries the metadata `ENUMERATE_FIELDS` reports (module,
  units, min/max, gettable/settable/system/debug), and `signed` now covers
  the `ENUM`/`BOOLEAN`/`TIME` types per PROTOCOL §4.7.
- Tests: `tests/test_discovery.py` (metadata parsing + a firmware-emulating
  transport that pins the walk's disambiguation and budget behaviour),
  `tests/test_telemetry.py` (subscription, decoding, unknown-key handling,
  repair scheduling off the I/O thread), `tests/test_compat_modules.py`
  (namespace building, shim semantics, `pi`, laziness).

### Performance
- `Display.set_leds` / `fill` / `show` / `set_brightness` are now async
  by default (new `await_ack: bool = False` keyword). The wire I/O runs
  on a single background worker thread, so a frame loop can compute
  frame N+1 while frame N is still being pushed. At most one frame is
  in flight at a time (firmware constraint); subsequent async calls
  block on the previous one for natural backpressure. Sync semantics
  remain available via `await_ack=True`. New `Display.flush(timeout)`
  drains the queue on demand; `LumiCube.stop()` drains it before
  closing the wire. Mirrors how the Java daemon's
  `_Display.set_leds → run_async(self.set, ...)` decouples script
  compute from wire transmission.
- `LumiCube.display` is now cached for the LumiCube's lifetime (it used
  to return a fresh `Display` per access). Required so the async
  worker thread and pending-write state survive across calls.
- `flat_dictionary.SizeTracker` — incremental encoded-size calculator
  used by `display._batch_for_frame`. Replaces the previous O(n²) split
  algorithm (which re-serialised the candidate batch on every key) with
  an O(n) one. ~22× faster on a 192-LED frame in microbench; reduces
  per-frame Python CPU dramatically on the Pi.

### Wire-protocol findings (vs. the Java daemon)
- The cube firmware does **not** service concurrent SET_FIELDS service
  requests — submitting two or three in flight at once causes all but
  the first to be dropped (no ServiceResponse, so the caller times out).
  An attempted pipelining of `_send_set_fields` was reverted; the Java
  daemon also issues SET_FIELDS strictly sequentially. Sequential
  semantics are now pinned by a unit test.
  *Updated 2026-10-23:* the firmware does process pipelined frames — it
  just does not answer them promptly. Waiting for the ServiceResponse is
  what serialises writes; submitting with `wait=False` and relying on the
  link-level ACK sustains 300-600 frames/s (see the notes at the top of
  this file). `Display` keeps its sequential worker, which is the right
  trade-off for LED frames; the screen and speaker drivers pipeline.

### Added
- `scripts/plasma.py` — native-API rewrite of the upstream community
  `lava_lamp.py` script. Precomputes the 192 surface coordinates and
  their LED indices at startup, replaces the per-pixel
  `colorsys.hsv_to_rgb` with a vectorised numpy packer, and benefits
  from the new async display so noise computation overlaps the previous
  frame's wire push.
- `scripts/digital_clock.py` — native-API example script (does not use
  the compat shim). Renders hours, minutes, and a filling-dot seconds
  animation, with an optional OpenWeatherMap temperature overlay.
  Configurable via `scripts/digital_clock_config.py`; a template is
  checked in as `scripts/digital_clock_config.py.example`.
- `[extras]` optional-dependencies group in `pyproject.toml`:
  `requests` for the digital-clock weather fetch. Install via
  `pip install -e '.[extras]'` or `uv sync --extra extras`.
- `.gitignore` rule for `scripts/digital_clock_config.py` so per-user
  API keys never reach version control.
- `pylumicube.compat.get_hosted_cube()` and `open_or_use_hosted(port)`
  helpers — let native-API scripts run both standalone (`python my.py`)
  *and* under `lumicube-run my.py`. The runner registers its open cube
  before exec'ing the script; `open_or_use_hosted` yields that cube
  when registered, or opens a fresh `LumiCube(port)` otherwise.
  `scripts/digital_clock.py` uses this pattern.

## [0.1.3] - 2026-05-28

### Added
- `pylumicube.compat` — compatibility shim mirroring the
  `foundry_api/standard_library.py` namespace that the upstream Java
  `foundry-daemon` injects into community scripts. Provides:
  - `LumiCubeCompat` facade with `display`, plus warn+no-op stubs for
    the not-yet-implemented modules (`microphone`, `speaker`, `screen`,
    `buttons`, `light_sensor`, `imu`, `env_sensor`, `pi`).
  - `DisplayShim` with `set_all`, `set_led`, `set_leds` (accepting int,
    `(x, y)`, or `(x, y, z)` keys), `set_panel`, `set_3d`, `brightness`
    property (clamped 0..100, mapped to the wire's 0..255), and a
    built-in `scroll_text` (5x7 ASCII font, no PIL).
  - Colour constants (`black`, `red`, etc.), `hsv_colour`,
    `random_colour`, `noise_2d/3d/4d` (via `opensimplex`), `run_async`,
    and the speaker waveform sentinels.
  - `build_globals(cube)` for embedding the namespace into a custom
    runner; `run_script(path)` for end-to-end execution.
- `lumicube-run <script.py>` CLI — opens the cube, builds the compat
  namespace, exec's the script, blanks the matrix on Ctrl-C.
- Tests pinning the (x, y) → LED-index and (x, y, z) → LED-index
  mappings to the upstream daemon's formulae.

### Changed
- Runtime dependency added: `opensimplex>=0.4` (used by the noise
  helpers; the rest of pylumicube still has no extra deps).
- `utilities/` directory (formerly `scripts/`) holds the reverse-
  engineering helpers; `scripts/` now holds upstream LumiCube
  community/user scripts that run via `lumicube-run`.

## [0.1.1] - 2026-05-14

### Added
- Initial public release of `pylumicube`, ported from the
  internal `lumicube-python` working tree.
- `LumiCube` high-level API with passive node discovery, preferred-name
  resolution, and a `Display` helper for the LED matrix.
- 3-stage dynamic node-ID allocator for cold-boot scenarios.
- `lumicube-leds` CLI (`all`, `single`, `off`) for the 24x8 LED panel.
- `scripts/snapshot_hardware.py` — `ENUMERATE_FIELDS` walker that
  decodes the cube's in-line sub-dictionaries, resolves block floors
  via forward-probe disambiguation + backwards binary search, and
  optionally probes well-known display keys to surface fields the
  walk can't reach.
- `scripts/check_pi_uart.sh` — fresh-Pi-image verification (boot
  config, no serial console, no `foundry-daemon` install).
- Protocol reference documented in `PROTOCOL.md`.

### Wire-protocol findings (vs. the Java daemon)
- Cube firmware silently drops `MESSAGE` payloads ≥ 245 bytes
  (between-delimiter count = 255 trips the parser); the safe ceiling
  is 244 bytes. `MAX_UAVCAN_PAYLOAD = 244`.
- Cube emits each metadata field as its own `01 01 <value>` run-1
  rather than coalescing into run-N. Receivers must handle both.
- Variable-length string prefix is **1 byte** (max 255) on the cube
  firmware vs LEB128 on the daemon. Identical for lengths < 128.
- `ENUMERATE_FIELDS` response leading-skip is contextual (echo when
  the query lands inside a block, relative-skip-to-next-floor in a
  gap) — see `PROTOCOL.md` §4.5.1 for the disambiguation algorithm.
- The `cube` base board reports overlapping field blocks
  (speaker.data 241..352, display.led_colour 266..1264,
  display.brightness 256). SET_FIELDS routes correctly per peripheral
  regardless. Open question — see `PROTOCOL.md` §7 item 5.

[0.1.3]: https://github.com/chrislibuilds/pylumicube/releases/tag/v0.1.3
[0.1.1]: https://github.com/chrislibuilds/pylumicube/releases/tag/v0.1.1
