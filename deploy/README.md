# Deploying pylumicube on the cube

How the LumiCube runs the Python clock instead of the Abstract Foundry Java
daemon. This is the setup that is live on the cube as of 2026-10-23 — the
Java daemon is stopped and disabled, a `pylumicube` daemon owns the cube,
the clock is a client of it, and Home Assistant talks to the cube over MQTT.

```
Home Assistant ──MQTT──> pylumicube-daemon ──serial──> LumiCube
                              ^  (control socket)
                              |
                    pylumicube-clock, scripts, lumicube-say
```

## 1. Install the package in a venv

```bash
cd ~/git/pylumicube
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[mqtt]'      # drop [mqtt] for no Home Assistant
```

`--system-site-packages` is deliberate on 32-bit Raspberry Pi OS: piwheels'
Pillow wheel is rejected by pip (its metadata says `pillow`, pip wants
`Pillow`) and the fallback is a 47 MB source build, while the distribution's
`python3-pil` is already installed and works fine. Pillow is only needed by
`Screen.draw_image` — the clock itself needs none of it. On 64-bit (arm64)
PyPI has proper aarch64 wheels, so `pip install -e '.[screen]'` works there.

Speech needs `espeak-ng` or `espeak` on `PATH` (`apt install espeak-ng`);
Raspberry Pi OS ships `espeak` already.

## 2. Put the settings outside the checkout

```bash
mkdir -p ~/.config/pylumicube
cp ~/git/pylumicube/scripts/ambient_clock_config.py.example \
   ~/.config/pylumicube/clock.py     # then edit
```

Every name is optional. The ones worth knowing about:

| Setting | Meaning |
| --- | --- |
| `ADAPT`, `LOW`, `HIGH` | auto-range the dimming curve, or pin the light range |
| `BRIGHTNESS_MIN` | floor in a dark room — `0.02` is a barely-visible glow |
| `SPEAKER_VOLUME` | 0..200; the firmware powers up at full blast |
| `ALLOW_REBOOT` | whether the middle button reboots the Pi |
| `TEMP_SAMPLE_SECONDS`, `TEMP_HISTORY` | CPU-temperature chart on the LCD |
| `STATUS_INTERVAL` | seconds between `ambient X -> brightness Y` log lines |

For Home Assistant, put the broker credentials next to it (see §4):

```bash
cat > ~/.config/pylumicube/mqtt.env <<'ENV'
PYLUMICUBE_MQTT_HOST=192.168.5.101
PYLUMICUBE_MQTT_USERNAME=lumicube
PYLUMICUBE_MQTT_PASSWORD=...
ENV
chmod 600 ~/.config/pylumicube/mqtt.env
```

## 3. Install the daemon and the clock

```bash
cp ~/git/pylumicube/deploy/pylumicube-daemon.service \
   ~/git/pylumicube/deploy/pylumicube-clock.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user disable --now foundry-daemon     # the old Java daemon
systemctl --user enable --now pylumicube-daemon
systemctl --user enable --now pylumicube-clock
```

The daemon owns the serial port, walks every schema once (~6 s), subscribes
to the boards that do not compete with streaming writes, and serves a
control socket at `$XDG_RUNTIME_DIR/pylumicube.sock` (mode 600, local only).
The clock connects to that socket; if no daemon is running it opens the port
itself, exactly as it did before — so the daemon is an upgrade, not a
dependency.

That split is why the clock can restart without dropping the cube link, and
why Home Assistant keeps working while it does. Clients:

```bash
.venv/bin/lumicube-say "Dinner is ready"        # speak through the cube
.venv/bin/lumicube-do stats                     # ask the daemon how it is
.venv/bin/lumicube-do read module=light_sensor field=ambient_light
.venv/bin/lumicube-run scripts/original/rainbow.py   # community scripts too
```

Everything above uses the daemon when it is there and falls back to the
serial port when it is not.

## 4. Home Assistant over MQTT
The Mosquitto broker add-on (or any broker) plus the `mqtt.env` file from §2
is all the configuration there is. The daemon publishes discovery payloads,
so Home Assistant creates the entities by itself:

| Entity | What it is |
| --- | --- |
| `sensor.lumicube_ambient_light`, `..._red/_green/_blue` | the light sensor |
| `sensor.lumicube_cube_cpu_temperature` | the Pi's CPU temperature |
| `sensor.lumicube_something_within_proximity` | proximity flag |
| `number.lumicube_speaker_volume` | 0..200, two-way |
| `number.lumicube_display_brightness` | 0..255, two-way |
| `text.lumicube_speak` | type a sentence, the cube says it |
| `event.lumicube_top_button`, `..._middle`, `..._bottom`, `..._base` | button presses, for automations |

Commands are plain payloads where that makes sense:

| Topic | Payload |
| --- | --- |
| `lumicube/speaker/say` | the sentence (`{"text": ..., "volume": 60}` also works) |
| `lumicube/speaker/tone` | `440` or `{"frequency": 880, "duration": 0.3, "waveform": "square"}` |
| `lumicube/speaker/play` | a WAV path on the cube |
| `lumicube/speaker/stop` | anything — also stops a stream another client started |
| `lumicube/speaker/volume` | 0..200 |
| `lumicube/screen/text` | `{"text": "hello", "size": 2, "colour": "#ffffff"}` |
| `lumicube/screen/image` | an image path on the cube |
| `lumicube/screen/clear`, `lumicube/display/fill` | a colour (`#rrggbb` or a number) |
| `lumicube/display/brightness` | 0..255 |

So the washing machine is:

```yaml
automation:
  - alias: Washing machine finished
    triggers:
      - trigger: state
        entity_id: sensor.washing_machine_state
        to: "finished"
    actions:
      - action: mqtt.publish
        data:
          topic: lumicube/speaker/say
          payload: "The washing machine has finished"
```

...and the cube's buttons can drive automations:

```yaml
    triggers:
      - trigger: state
        entity_id: event.lumicube_top_button
        attribute: event_type
        to: "press"
```

Without a broker (or with `--no-mqtt`) the daemon simply runs without the
bridge; nothing else changes.

## 5. Getting the brightness right

The dimming curve has two knobs, and only one of them works at a time:

- **`ADAPT = True`** (auto-ranging): the darkest reading the curve has seen
  sits at the *bottom* of its range, so in a dark room the position is ~0 and
  **`GAMMA` has nothing to lift** — `BRIGHTNESS_MIN` (the floor) alone decides
  how bright the clock is. Simple and stable; every dark room looks the same.
- **`ADAPT = False`** with `LOW`/`HIGH` pinned to the room's real range, plus
  `GAMMA < 1` to lift the dark end: an evening and the middle of the night
  can differ. Needs measurements.

Either way, measure instead of guessing:

```bash
systemctl --user stop pylumicube-clock
.venv/bin/python scripts/calibrate_dimming.py          # judge each level by eye
systemctl --user start pylumicube-clock
```

It shows the clock's own digits at a series of brightnesses, prints the live
ambient reading at each one, and ends with ready-to-paste config lines (it
also solves for the `GAMMA` that puts your chosen brightness at the reading
you saw). Over a plain `ssh` command there is no terminal to prompt on, so
pass `--readable 0.5` instead.

For the second option you want readings from a whole day, which the clock is
already collecting in `LIGHT_LOG` (default
`~/.local/state/pylumicube/light.csv`, one row per `STATUS_INTERVAL`):

```bash
.venv/bin/python scripts/calibrate_dimming.py --report
```

```
hour     n  ambient min     avg     max   brightness avg
  18    30           92      96     118             0.66
  23    30           24      28      31             0.50
```

## 6. Rolling back to the Java daemon

```bash
systemctl --user disable --now pylumicube-clock
systemctl --user enable --now foundry-daemon
```

`foundry-daemon.service` (in `~/.local/share/systemd/user/`) still exists,
and `~/AbstractFoundry/` is untouched.

## Notes

- **Only one process can own `/dev/ttyAMA0`**: the daemon when it is
  running, the clock (or a script) when it is not. Two `LumiCube`s at once
  means one of them gets `Resource busy`.
- **The control protocol** is line-delimited JSON on
  `$XDG_RUNTIME_DIR/pylumicube.sock` (mode 600, never a TCP port — MQTT is
  the network face). A request is `{"id": 1, "op": "read", "module": ...,
  "field": ...}`; the reply echoes the id, and telemetry arrives as
  unsolicited `{"event": "values", ...}`. `lumicube-do OP k=v` sends any op
  by hand. Ops: `hello`, `schemas`, `walk`, `subscribe`, `read`, `read_all`,
  `write`, `run`, `frame`, `say`, `tone`, `play`, `stop_audio`, `snapshot`,
  `stats`, `ping`.
- **Why the daemon walks the cube board but does not subscribe to it**: the
  drivers resolve their field keys through the board's schema, but the cube
  board's telemetry stream competes with speaker writes for the link
  (~308 → ~56 chunks/s), which is the difference between speech and
  stutter. `cube.walk_all_schemas()` is what walks without subscribing.
- **Audio is serialised on the daemon**, not on the clients: a `say` holds
  the speaker for the whole utterance, so an announcement from Home
  Assistant cannot talk over the clock's hourly chime. Display writes are
  *not* held up by that — they use a separate lock.
- **The microphone is off by default.** Capturing needs the cube board
  subscribed, and the firmware publishes every field on that board at full
  rate (its 192-LED block included): measured at 30 service timeouts in 30
  seconds, with display writes failing. The daemon refuses it unless started
  with `--allow-cube-telemetry`, and the capture itself wants the clock
  stopped:

  ```bash
  systemctl --user stop pylumicube-clock
  .venv/bin/python scripts/record_microphone.py --seconds 5 --play
  systemctl --user start pylumicube-clock
  ```
- A healthy daemon start looks like this (about 6 s, most of it discovery):

  ```
  node 124 (button_and_light_sensor): 17 field block(s) discovered in 2.0s
  node 125 (cube): 49 field block(s) discovered in 3.9s
  telemetry: walking node 125 (cube) but not subscribing to it
  control socket listening on /run/user/1000/pylumicube.sock
  MQTT bridge connecting to 192.168.5.101:1883 as pylumicube
  daemon ready: cube on /dev/ttyAMA0, 9 modules
  ```

  and the clock then logs `connected to the daemon on ...` instead of
  opening the port itself.
- User units start at boot without a login session because lingering is
  enabled (`loginctl enable-linger $USER`).
- The old community scripts in `~/AbstractFoundry/Daemon/Scripts/` can still
  be run, against the same cube, with
  `.venv/bin/lumicube-run ~/AbstractFoundry/Daemon/Scripts/main.py`.
