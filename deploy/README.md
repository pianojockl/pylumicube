# Deploying pylumicube on the cube

How the LumiCube runs the Python clock instead of the Abstract Foundry Java
daemon. This is the setup that is live on the cube as of 2026-10-23 — the
Java daemon is stopped and disabled, the clock is a systemd **user** unit
(the same way the old daemon was installed), and the display, LCD, buttons
and speaker are all driven by `pylumicube`.

## 1. Install the package in a venv

```bash
cd ~/git/pylumicube
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
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

## 3. Install the unit

```bash
cp ~/git/pylumicube/deploy/pylumicube-clock.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user disable --now foundry-daemon     # the old Java daemon
systemctl --user enable --now pylumicube-clock
```

The unit runs

```
%h/git/pylumicube/.venv/bin/python %h/git/pylumicube/scripts/ambient_clock.py \
    --config %h/.config/pylumicube/clock.py
```

with `Restart=always`. It handles `SIGTERM`, so a stop or restart blanks the
display and releases `/dev/ttyAMA0` cleanly instead of leaving a frozen
frame. Follow it with:

```bash
systemctl --user status pylumicube-clock
journalctl --user -u pylumicube-clock -f
```

A healthy start looks like this (about 8 s, most of it schema discovery):

```
node 124 (button_and_light_sensor): 17 field block(s) discovered in 2.0s
node 125 (cube): 49 field block(s) discovered in 4.7s
speaker volume set to 100
startup brightness 0.212 (range 50..1999)
```

User units start at boot without a login session because lingering is
enabled (`loginctl enable-linger $USER`).

## 4. Getting the brightness right

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

## 5. Rolling back to the Java daemon

```bash
systemctl --user disable --now pylumicube-clock
systemctl --user enable --now foundry-daemon
```

`foundry-daemon.service` (in `~/.local/share/systemd/user/`) still exists,
and `~/AbstractFoundry/` is untouched.

## Notes

- **Only one of the two can run at a time**: both own `/dev/ttyAMA0`.
- **Why the clock walks the cube board but does not subscribe to it**: the
  drivers resolve their field keys through the board's schema, but the cube
  board's telemetry stream competes with speaker writes for the link
  (~308 → ~56 chunks/s), which is the difference between speech and
  stutter. `cube.walk_all_schemas()` is what walks without subscribing.
- The old community scripts in `~/AbstractFoundry/Daemon/Scripts/` can still
  be run, against the same cube, with
  `.venv/bin/lumicube-run ~/AbstractFoundry/Daemon/Scripts/main.py`.
