# Overdrive Hub

A Windows desktop app (Electron + Python) for Anki Overdrive cars. It scans your track and keeps
it, shows the track and the cars live in 3D, and runs races against AI racers. You can build your
own racers and game modes.

Not affiliated with Anki or Digital Dream Labs. It talks to the cars directly over Bluetooth LE.

## Features

- **Track scanning, live in 3D.** A car drives two slow laps and the map builds piece by piece as it
  goes. Supported: crossovers (`+`), the Fast & Furious bonus piece, and which way the start line
  faces. Every scan is saved. Scanning a known track updates it instead of duplicating it. With
  no track selected, the app recognises a saved track after a car drives a few pieces.
- **Position tracking.** Pieces are identified by id and driving direction. Between the car's
  position reports (about 1 per second), the position is estimated from the commanded speed and
  calibrated with the cars' wheel odometry. Measured on real cars: correct piece 100 % of the time,
  in-piece estimate within about ±10 % at piece ends.
- **Garage for many cars, duplicates included.** Each car is identified by its Bluetooth address,
  with a short tag (e.g. `EB:D9`) you can write on the car, a nickname, an engine-light colour,
  and a *Blink* button to find it. *Check charge* reads every car's real charger and battery status
  (the advertised flag is cached by Windows and goes stale).
- **AI racers.** Six built-in personalities in four difficulties, plus your own from the Builder (sliders).
  Advanced: drop a Python file in `data/racers/` for custom logic.
- **Game modes.** Race, Time Trial, Battle (virtual blasters and mines, HP, knockouts), King of the Hill,
  Elimination, Free Drive. Copy any mode and change its rules.
- **Driving.** Keyboard or one gamepad per human driver. **STOP ALL** / `Esc` stops every car.
- **Simulator** with virtual cars, for trying things without hardware.

## Setup (Windows)

Requires Python 3.11+, Node.js 18+, and a Bluetooth LE adapter.

```powershell
git clone https://github.com/gaston1799/overdrive-hub
cd overdrive-hub
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
cd app
npm install
npm approve-scripts electron   # npm 11 blocks Electron's download script by default
npm rebuild electron
cd ..
```

Run: double-click `Overdrive Hub.cmd`, or `Overdrive Hub (Simulator).cmd` for 4 virtual cars (Bluetooth off,
separate data folder).

## First run with real cars

1. **Garage**: *Find cars*, then *Check charge*. Rename duplicates, pick colours, *Blink* to match a
   physical car. Connect up to 8.
2. **Tracks**: pick a car, then *Scan*. Watch the track build. If the map is mirrored: *Edit*, then *Mirror*.
3. **Play**: pick a mode, tick cars, set each to *Me* or an AI racer, then *Start*.

Keyboard (first human car): W/S speed, A/D lanes, Space fire, M mine, X brake, Esc stop all.
Gamepad: RT speed, LT brake, bumpers/D-pad/stick lanes, A fire, B mine.

## Layout

| Path | What |
|---|---|
| `odhub/protocol.py` | Bluetooth message encoding/decoding |
| `odhub/links.py` | Bluetooth (bleak) and the simulator |
| `odhub/track.py` | track model, scanner, layout check, per-car localisation |
| `odhub/car.py` | one car: telemetry, speed estimate, commands, lights |
| `odhub/drivers.py`, `odhub/modes.py` | AI racers, game modes |
| `odhub/engine.py`, `odhub/server.py` | control loop, WebSocket API |
| `web/` | UI and the Three.js scene |
| `app/` | Electron shell (starts the engine, opens the window) |
| `tools/` | `probe.py` (read a car without moving it), `live_test.py`, `replay_loc.py` |
| `data/` | your garage, tracks, logs (git-ignored) |

Tests: `.venv\Scripts\python -m pytest tests`. These use the simulator only and never touch real cars.

See [TODO.md](TODO.md) for what's next.

## Credits

Protocol knowledge from the public [anki/drive-sdk](https://github.com/anki/drive-sdk), and from studying
[Anki-Partydrive](https://github.com/MasterAirscrachDev/Anki-Partydrive) (piece reporting, start-line
halves, V4 firmware speed flag) and [overdrive-arena](https://github.com/Leicas/overdrive-arena)
(model ids, advertisement layout). No code was copied from either project.
