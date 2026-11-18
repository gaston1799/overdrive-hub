# TODO

Status as of 2026-11-18. Tested on Windows 11 with real cars (Thermo, Guardian, firmware 0x3060)
on two real tracks, plus the simulator. `tools/` has the scripts used for every measurement below.

## Next up

- [ ] **Multi-car driving.** AI racers do not avoid each other yet. Most urgent at the `+` crossover,
      where two cars can meet side-on. Needs: crossover reservation (only one car on the
      crossover and the piece before it at a time), following distance, overtaking only on straights.
      Live tests so far were one car at a time.
- [ ] **Live test of game modes with real cars** (race, battle, king of the hill, elimination).
      All modes pass in the simulator only.
- [ ] **Find the two missing cars** (Groundshock `06:67`, presumably Thermo `9F:47`): charge them,
      Find cars, Check charge. The user's list also mentions 2 Skulls and 2 Nukes, but only one of
      each has ever been seen. Confirm the real total (14 or 16).
- [ ] **Human driving test with a gamepad** on the real track (keyboard and gamepad code paths
      are untested on hardware).

## Position telemetry

Current numbers (replays in `data/test-logs`, run `tools/replay_loc.py`):
piece identification 100 % / never lost; in-piece estimate at piece end median 0.99 to 1.02,
10–90 % range about 0.86–1.10.

- [ ] Use location IDs inside a piece (the cars report them about 1.3 times per second) to correct
      the in-piece estimate mid-piece instead of only at transitions. Needs a learned
      (piece, location) → fraction table, built from logs (no third-party table).
- [ ] Persist learned piece lengths and per-car speed factors between sessions (they reset on
      restart; it takes about one lap to relearn).
- [ ] The first 2 to 6 s after connecting the car is not located yet. Could start from the last
      known piece if the car has not moved (check on_track and charger status).
- [ ] Cars that crash (`DELOCALIZED`) recover slowly. Consider stopping them and asking the user
      to put them back, with a "put car back" prompt in the UI.
- [ ] Remove the remaining small display jumps (about 4 per 30 s for Thermo): blend the estimate
      toward the transition instead of holding at 0.99 then jumping.

## Track scanning

- [ ] Multi-car scan: drive every connected car, not just one (faster, cross-validates laps).
- [ ] Jump ramp and landing pieces are modelled but never tested on a real track.
- [ ] Drive mats (Oval / Bottleneck / Crossroads / F1 / Double Cross, ids 70–244) are not supported.
- [ ] Scan should avoid parked cars (currently it can drive into a car left on the track).
- [ ] Track editor: drag-to-reorder or add/remove pieces by hand.

## Garage / hardware

- [ ] Charge check: optionally re-check every N minutes, and warn when a racing car gets low.
- [ ] Remember firmware per car and show V4 cars (0x3061 / 0x3062) explicitly (handled but untested).
- [ ] Windows caches advertised names, so advert-based charger flags are stale. Investigate
      `BluetoothLEAdvertisementWatcher` raw scan responses to read the live flag without connecting.
- [ ] Bluetooth adapter limit: the app caps at 8 connections. Measure the real limit on this
      Intel adapter.

## AI racers / game modes

- [ ] Tune default racer speeds per track: learn safe curve speeds from crashes (lower the cap
      for the piece where a car came off).
- [ ] Optional LLM "strategist" racer (slow decisions every few seconds: lane choice, when to
      fire), on top of the hard-coded drivers.
- [ ] More built-in modes: tag / chase, relay, survival with track hazards.
- [ ] Sound effects (engine, blasters, countdown) and a race results screen with lap charts.

## App / packaging

- [ ] Installer (electron-builder) that bundles Python + dependencies, so the app runs without
      the `.venv` setup.
- [ ] Settings page (adapter choice, max cars, scan speed, units).
- [ ] Code signing / auto-update.
- [ ] CI: run `pytest tests` on push (simulator only, never real cars).
