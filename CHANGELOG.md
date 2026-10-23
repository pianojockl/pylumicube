# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **The `stats` op reports the speech path's counters** (`audio`:
  `written_chunks`, `dropped_chunks`, `loss_percent`), so smoothness is
  checkable from a running daemon — `lumicube-do stats` — instead of only by
  ear. Dropped chunks are the audible failure mode (the firmware's audio
  buffer is ≈47 ms) and were previously invisible from outside the process;
  the fix that made speech smooth was found by measuring exactly these.
- **A single-instance lock on the daemon.** A second daemon used to open
  `/dev/ttyAMA0` (the port the first one owns) and only *then* discover the
  socket was taken, so a stray launch disturbed the running daemon's link
  before failing — frames the firmware expected from the real owner went to
  the intruder. `acquire_instance_lock()` takes an `flock` on
  `<socket>.lock` before the cube is touched, so a duplicate now fails with
  `another pylumicube daemon already owns ...` and leaves the running daemon
  alone. Verified on the cube: the duplicate is refused and the clock does
  not even restart.

### Fixed
- **The hourly chime said "sixteen o'clock".** It handed espeak-ng the raw
  24-hour number (`f"it's {now.hour} o'clock"`), and espeak reads a bare
  number literally — verified with its phoneme output: `it's 16 o'clock` →
  `Its s'Iksti:n @kl'0k`. So 13:00-19:00 were announced as
  "thirteen"…"nineteen" while the morning was accidentally right (8-12 read
  as "eight"…"twelve"). The hour now comes from a `HOUR_WORDS` table indexed
  by `hour % 12`, so the chime is 12-hour by ear — eight, nine, ten, eleven,
  twelve, one, two, three, four, five, six, seven — and a test asserts no
  spoken chime contains a digit at all (`08` would be read as "zero eight",
  which is how the pre-rewrite version announced 8:00).
- **The MQTT bridge heard its own state as a command, and looped with Home
  Assistant.** It subscribed to `lumicube/speaker/#`, which includes
  `lumicube/speaker/volume` — the topic it *publishes* the volume on. Each
  state it published came back as a command, Home Assistant's number entity
  answered that, and the pair settled into a loop: **10,470 state changes on
  `number.lumicube_speaker_volume` in a few minutes**, cycling 0/5/10 at
  ~25 Hz. Commands now live under `/set` (`lumicube/speaker/volume/set`,
  `lumicube/display/brightness/set`) with state on the bare topic, which is
  the convention Home Assistant's MQTT entities expect; the bridge subscribes
  to an explicit list of command topics, never a `#` wildcard, and ignores a
  command whose value matches the state it last published. Verified: one
  retained message in 15 s where there had been ~25/s, and HA reports the
  cube's real volume.
- **A daemon restart left the clock silently dead.** Two causes, both fixed.
  The control client's reader marked its futures failed on EOF but never
  marked the client closed, so `connected` kept returning true for a socket
  that could only produce `Broken pipe`; and the clock's link check ran only
  *after* an exception, which the async display path never raises — writes
  fail inside the worker and telemetry just freezes, so the clock could draw
  nothing for hours while looking perfectly healthy. The client now closes
  itself on EOF (and gained `CubeClient.reconnect()`, which rebuilds the
  connection and re-issues the subscriptions), and the clock checks the link
  on every pass, reconnects, re-applies its brightness and volume, and
  redraws. Verified on hardware: a daemon restart under a running clock
  produces one warning, ~9 s of retries, then `reconnected to the daemon;
  resuming` — with no clock restart, so the learned dimming range survives.

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
- **`Display.scroll_pixels(rows, ...)`** — scroll a pre-rendered bitmap
  across the 16x8 area, so a caller can scroll its own glyphs;
  `Display.scroll_text` now renders with the built-in 5x7 font and
  delegates to it.
- The clock scrolls **its own 3x5 digits** (`SCROLL_FONT = "digits"`, the
  default) instead of the taller built-in font, so the scrolled time is
  the same size as the static display; `SCROLL_FONT = "firmware"`
  restores the old look.
- **`deploy/`** — a systemd user unit (`pylumicube-clock.service`) and a
  step-by-step guide for running the clock on the cube instead of the Java
  daemon: venv layout, settings in `~/.config/pylumicube/clock.py`, the
  enable/disable dance, and how to roll back.
- **`Speaker` starts at a sane volume** (`DEFAULT_VOLUME`, not
  `MAX_VOLUME`): the firmware powers up at full blast, which is far too
  loud in a room. Measured since on this cube: comfortable speech is around
  **20** of 200, so the default is 40, and `Speaker(volume=...)`, the demo's
  `--volume` and the clock's `SPEAKER_VOLUME` all override it.
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
- **A daemon that owns the cube, and clients that talk to it**
  (`pylumicube.control`, `pylumicube.daemon`, `pylumicube.client`,
  `lumicube-daemon`): line-delimited JSON on a Unix socket, telemetry
  pushed to subscribers instead of polled per read, `await_response` /
  `wait` carried through so streaming callers are not forced to wait, and a
  separate audio lock so a three-second announcement cannot stall the
  clock's display. `connect_or_open()` / `open_or_use_hosted()` prefer the
  daemon and fall back to the serial port, so nothing needs it.
- **`lumicube-say` and `lumicube-do`** — speak through the daemon, or send
  any control operation and print the reply.
- **An MQTT bridge for Home Assistant** (`pylumicube.mqtt`, `[mqtt]`
  extra): commands as plain payloads (`lumicube/speaker/say` takes the
  sentence), retained state for the light sensor, CPU temperature, volume
  and display brightness, an `online`/`offline` LWT, and MQTT discovery so
  HA creates the entities itself — including event entities for the cube's
  four buttons, so a button press can trigger an automation.
- **`deploy/pylumicube-daemon.service`** and the daemon section of the
  deploy guide, including the Home Assistant setup and the washing-machine
  automation.
- **Microphone capture** (`pylumicube.microphone`, `LumiCube.microphone`):
  16 kHz mono s16le assembled from the published `microphone.data` block
  (112 samples per frame — the same format the Java daemon piped to
  PulseAudio), with `capture()`, `record()` (WAV) and `chunks()`, and
  `scripts/record_microphone.py` to record and play it back. It is
  **refused by default** on the daemon: subscribing to the cube board floods
  the link (measured: 30 service timeouts in 30 seconds, display writes
  failing, no handshake possible) because the firmware publishes every field
  on that board at full rate. `lumicube-daemon --allow-cube-telemetry` opts
  in; record with the clock stopped.
- **`TelemetryStore.add_update_handler` / `RemoteTelemetry.on_values`** —
  runtime frame consumers (what the microphone uses), separate from the
  constructor's single `on_update`.
- **An `unsubscribe` operation** (and `TelemetryStore.unsubscribe`), so a
  client that wanted the link back can stop renewing its subscription.
- **`LIGHT_LOG`** — the clock appends `timestamp,ambient,brightness,low,high`
  to a CSV every `STATUS_INTERVAL` (default
  `~/.local/state/pylumicube/light.csv`), so a room's light over a whole day
  is on disk to calibrate the dimming curve from.
- **`scripts/calibrate_dimming.py`** — walks the display through brightness
  levels using the clock's own rendering, prints the live ambient reading at
  each one, and ends with ready-to-paste settings for both ways of driving
  the curve, including the `GAMMA` that puts your chosen brightness at the
  reading you saw. `--report` summarises the light log by hour; `--readable
  LEVEL` covers the non-interactive case.

### Fixed
- **Choppy speech, properly this time.** A probe that attributes every
  millisecond (`ref_data/tools/audio_stall_probe.py`) showed the write itself
  was only ~10% of the loop's time: 20-78% was spent *outside* it, waiting on
  the in-flight window. `write_pcm` blocked on the oldest future whenever 32
  writes were outstanding — and the Java daemon never blocked on anything (it
  passed a `NullContinuation`). Three changes:
  - the in-flight window is **never waited on** (the future is only for error
    reporting; audio does not need it),
  - lateness is **tolerated** up to `MAX_LAG_SECONDS` instead of dropping the
    first late chunk — upstream's PulseAudio buffer played exactly that role,
    so a hiccup delayed the audio rather than deleting part of it,
  - the `lead` no longer tightens the drop threshold (it is a head start, not
    an earlier deadline; conflating them made a *bigger* lead drop *more*).
  Measured over a 7-second sentence: dropped chunks **37-446 → 0-4**, worst
  gap **52-66 ms → 13-25 ms** (the firmware's own buffer is ≈47 ms, so it no
  longer underruns), stalls over 20 ms **6-55 → 0-1**, and loop time outside
  the write **20-78% → 5-8%**. A nine-second announcement through the daemon
  now reports **0% loss** (2 chunks of 2614). Confirmed by ear on the cube
  (the numbers can show gaps closing, but only listening says whether what is
  left is audible), and a fresh 7-second sentence through the *running*
  daemon reports **0 dropped of 1499 chunks** via `lumicube-do stats`.
- **Choppy speech.** The stream was paced to exactly real time, which leaves
  the firmware's small audio buffer empty: measured on the cube, the p50 gap
  between chunks was spot on (3.5 ms) but the p95 was 8.6 ms and spikes
  reached 50 ms, so ~10% of chunks arrived late and the buffer underran.
  Two changes: a **lead** (`LEAD_SECONDS`, 14 ms — measured as the sweet
  spot; 35 ms overflows the firmware and *drops* chunks) and **grouped
  pacing** (`PACE_GROUP`, three chunks per wakeup, so there are three times
  fewer `sleep()` overshoots to be late with). Drops for a nine-second
  sentence went from 7% to ~2%, best case 0. `espeak-ng` is also preferred
  over `espeak` when installed — its synthesis is markedly clearer, and it
  is what the probe was measuring.
- **`LumiCube.modules` no longer requires telemetry.** It raised if the cube
  was opened without it, which broke `write_named` — and so the whole
  speaker driver — on a telemetry-less cube even though writing by name only
  needs the field *keys*. The namespace is now built against an empty store,
  so reads are None and writes work.
- **Telemetry broadcasts ran on the link's I/O thread.** Serialising a
  frame for every client — a cube-board frame carries a couple of hundred
  values — was slow enough to stall the wire, which made service requests
  time out and the daemon unable to complete a handshake. Broadcasts now go
  through a bounded queue and a sender thread, so a slow client loses frames
  instead of the cube losing its link.
- **The clock died on a dropped frame.** A timed-out display write
  propagated out of `render()` and killed the process (systemd restarted it,
  noisily). Drawing now logs and retries, and the main loop survives any
  transient link failure — a hiccup should cost a frame, not the clock.
- **A telemetry store decoded frames before it was armed.** The broadcast
  handler was registered in the constructor, so frames arriving during
  `enable_telemetry`'s schema walk were decoded against a half-built
  namespace and fired the update callback before the modules existed (the
  daemon's button watcher logged a traceback per frame). The store now
  arms itself in `start()`, which is also when the renewal heartbeat
  begins.
- **`GAMMA` cannot brighten a dark room while `ADAPT = True`.** Auto-ranging
  puts the darkest reading the curve has seen at the bottom of its range, so
  in a dark room the position is ~0 and `0 ** gamma` is still 0: only
  `BRIGHTNESS_MIN` (the floor) decides the brightness. Both the example
  config and `deploy/README.md` now say so, the example's floor moved from
  0.02 (effectively off) to 0.45, and `calibrate_dimming.py` measures the
  floor you actually want.
- **Scrolling ran at a fraction of the requested speed.** Every frame
  waited for its ServiceResponse, and a display write costs ~50-100 ms of
  *latency* regardless of payload, so `scroll_text` crawled at 6.5 fps
  where 14 was asked for — and the frames arrived in 100 ms bursts, which
  reads as stutter. Frames are now sent fire-and-forget (the way the
  speaker streams), paced to `0.05 / speed`, with at most four on the
  wire, and only the pixels that changed: **12.9 fps measured on
  hardware**, within 8% of the target. A final full frame, sent
  synchronously, guarantees the end state. Upstream never had the problem
  because its `display.set_leds` ran *inside* the daemon process.
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
