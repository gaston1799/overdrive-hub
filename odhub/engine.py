"""The engine: owns the cars, the active track, scanning, the game and the 20 Hz control loop."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from . import protocol as P
from .car import Car
from .drivers import Context, Driver, HumanDriver, Rival, make_driver
from .library import Library
from .links import Advert, BleLink, SimWorld, scan_ble
from .modes import Game, forward_gap
from .track import Track, TrackScanner, sample_track

log = logging.getLogger("odhub.engine")

TICK = 0.05
MAX_CONNECTED = 8
SCAN_SPEED = 450


class Engine:
    def __init__(self, library: Library, sim_cars: int = 0, use_ble: bool = True):
        self.lib = library
        self.use_ble = use_ble
        self.cars: dict[str, Car] = {}
        self.adverts: dict[str, Advert] = {}
        self.drivers: dict[str, Driver] = {}
        self.controllers: dict[str, str] = {}           # address -> 'none' | 'human' | 'ai:id:diff'
        self.track_key: Optional[str] = None
        self.track: Optional[Track] = None
        self.game: Optional[Game] = None
        self.scan_job: Optional[dict] = None
        self.sim: Optional[SimWorld] = None
        if sim_cars:
            self.sim = SimWorld(sample_track(), sim_cars)
            if not self.lib.tracks and use_ble is False:
                self.lib.save_track(sample_track("Sim Oval"))
        self.listeners: list[Callable[[dict], None]] = []
        self.log: list[dict] = []
        self.ble_error: Optional[str] = None
        self.charge: dict[str, dict] = {}            # address -> live status from check_charge()
        self._task: Optional[asyncio.Task] = None
        last = self.lib.settings.get("last_track")
        if last in self.lib.tracks:
            self.select_track(last)

    # ---------------------------------------------------------------- notifications
    def notify(self, text: str, level: str = "info", **extra) -> None:
        entry = {"t": time.time(), "text": text, "level": level, **extra}
        self.log = (self.log + [entry])[-200:]
        for fn in self.listeners:
            fn({"type": "notice", **entry})
        log.info(text)

    def push_catalog(self) -> None:
        """Send the saved tracks/racers/modes to every window (after anything is saved)."""
        cat = self.catalog()
        for fn in self.listeners:
            fn(cat)

    def name(self, address: str) -> str:
        c = self.cars.get(address)
        return c.nickname if c else self.lib.garage.get(address, {}).get("nickname", address)

    # ---------------------------------------------------------------- discovery
    async def discover(self, seconds: float = 4.0) -> None:
        found: list[Advert] = []
        if self.sim:
            found += self.sim.adverts()
        if self.use_ble:
            try:
                found += await scan_ble(seconds)
                self.ble_error = None
            except Exception as e:  # noqa: BLE001
                self.ble_error = f"Bluetooth scan failed: {e}"
                self.notify(self.ble_error, "error")
        self.adverts = {a.address: a for a in found}
        for a in found:
            self.lib.car_profile(a.address, P.model_name(a.model_id))
        self._sync_names()
        self.notify(f"Found {len(found)} car(s)")

    def _sync_names(self) -> None:
        for a, c in self.cars.items():
            g = self.lib.garage.get(a)
            if g:
                c.nickname = g["nickname"]
                c.model = g.get("model", c.model)

    async def check_charge(self) -> None:
        """Windows caches the advertised name that carries the charger flag, so it goes stale.
        Connect to each car briefly (it does not move), read its live status + battery, disconnect."""
        targets = [a for a in self.adverts.values() if a.address not in self.cars and not a.simulated]
        self.notify(f"Checking {len(targets)} car(s), about 3 s each…")
        for adv in targets:
            got: dict = {}
            link = BleLink(adv.address)

            def on_msg(data: bytes, got=got) -> None:
                mid, val = P.parse(data)
                if mid == P.STATUS:
                    got["status"] = val
                elif mid == P.BATTERY_RSP:
                    got["mv"] = val
            link.on_message = on_msg
            try:
                await asyncio.wait_for(link.connect(), 10)
                await link.send(P.sdk_mode(True))
                await link.send(P.battery_request())
                for _ in range(20):
                    if "status" in got and "mv" in got:
                        break
                    await asyncio.sleep(0.1)
                    if _ == 8 and "mv" not in got:
                        await link.send(P.battery_request())
            except Exception as e:  # noqa: BLE001
                got["error"] = str(e)
            finally:
                try:
                    await asyncio.wait_for(link.close(), 3)
                except Exception:  # noqa: BLE001
                    pass
            st = got.get("status")
            self.charge[adv.address] = {
                "t": time.time(), "error": got.get("error"),
                "on_charger": st.on_charger if st else None, "full": st.battery_full if st else None,
                "low": st.battery_low if st else None, "on_track": st.on_track if st else None,
                "battery": P.battery_percent(got["mv"]) if "mv" in got else None, "mv": got.get("mv"),
            }
            self.push_catalog()
        n = sum(1 for c in self.charge.values() if c.get("on_charger"))
        self.notify(f"Charge check done: {n} on a charger", "success")

    async def connect(self, address: str) -> None:
        if address in self.cars and self.cars[address].connected:
            return
        if sum(c.connected for c in self.cars.values()) >= MAX_CONNECTED:
            self.notify(f"Already {MAX_CONNECTED} cars connected; disconnect one first", "warn")
            return
        adv = self.adverts.get(address)
        model_id = adv.model_id if adv else 0
        prof = self.lib.car_profile(address, P.model_name(model_id))
        simulated = bool(adv and adv.simulated)
        link = self.sim.get(address) if simulated else BleLink(address)
        car = Car(address, link, model_id, prof["nickname"], prof["color"], simulated)
        car.on_event = self._car_event
        car.loc.on_lap = lambda lap_s, a=address: self._on_lap(a, lap_s)
        car.loc.set_track(self.track)
        self.cars[address] = car
        self.controllers.setdefault(address, "none")
        self.drivers[address] = make_driver(self.controllers[address], self.lib.racers, self.lib.plugins)
        try:
            await car.connect()
            self.notify(f"Connected {car.nickname}")
        except Exception as e:  # noqa: BLE001
            self.notify(f"Could not connect {car.nickname}: {e}", "error")
            self.cars.pop(address, None)

    async def disconnect(self, address: str) -> None:
        car = self.cars.pop(address, None)
        if car:
            await car.disconnect()
            self.notify(f"Disconnected {car.nickname}")

    async def connect_all(self) -> None:
        for a in list(self.adverts):
            if sum(c.connected for c in self.cars.values()) >= MAX_CONNECTED:
                break
            await self.connect(a)

    def _car_event(self, kind: str, data: dict) -> None:
        a = data["car"]
        if kind == "disconnected":
            self.notify(f"{self.name(a)} lost connection. Reconnecting...", "warn")
            asyncio.get_running_loop().create_task(self._reconnect(a))
        elif kind == "delocalized":
            if a in self.cars:
                self.cars[a].recovering = True
            self.notify(f"{self.name(a)} came off the track", "warn", car=a)

    async def _reconnect(self, address: str) -> None:
        car = self.cars.get(address)
        for attempt in range(5):
            if not car or address not in self.cars:
                return
            await asyncio.sleep(2 + attempt * 2)
            try:
                await car.connect()
                self.notify(f"{car.nickname} reconnected")
                return
            except Exception:  # noqa: BLE001
                continue
        self.notify(f"{self.name(address)} could not reconnect", "error")

    # ---------------------------------------------------------------- car settings
    async def update_car(self, address: str, nickname: Optional[str] = None, color: Optional[str] = None) -> None:
        self.lib.update_car(address, nickname=nickname, color=color)
        self._sync_names()
        car = self.cars.get(address)
        if car:
            if color:
                car.color = color
                await car.apply_color()

    async def blink(self, address: str) -> None:
        car = self.cars.get(address)
        if car:
            await car.flash("#ffffff", 3.0, 60)

    def set_controller(self, address: str, spec: str) -> None:
        self.controllers[address] = spec
        self.drivers[address] = make_driver(spec, self.lib.racers, self.lib.plugins)

    def human_input(self, address: str, throttle: Optional[float] = None, lane: Optional[int] = None,
                    lane_step: Optional[int] = None, fire: bool = False, mine: bool = False) -> None:
        d = self.drivers.get(address)
        if not isinstance(d, HumanDriver):
            return
        car = self.cars.get(address)
        if throttle is not None:
            d.throttle = max(0.0, min(1.0, float(throttle)))
        if lane is not None:
            d.lane_index = int(lane)
        if lane_step and car:
            cur = d.lane_index if d.lane_index is not None else min(range(4), key=lambda i: abs(P.LANES[i] - car.offset))
            d.lane_index = max(0, min(3, cur + lane_step))
        d.fire |= bool(fire)
        d.mine |= bool(mine)

    # ---------------------------------------------------------------- tracks
    def select_track(self, key: Optional[str]) -> None:
        self.track_key = key if key in self.lib.tracks else None
        self.track = self.lib.tracks.get(key) if key else None
        for c in self.cars.values():
            c.loc.set_track(self.track)
        if self.sim and self.track and self.track.closed:
            self.sim.track = self.track
        self.lib.set_setting("last_track", self.track_key)

    def start_track_scan(self, address: str) -> None:
        car = self.cars.get(address)
        if not car or not car.connected:
            self.notify("Connect a car first", "warn")
            return
        if self.game:
            self.notify("Stop the game before scanning", "warn")
            return
        car.scanner = TrackScanner(laps=2)
        self.scan_job = {"car": address, "started": time.monotonic()}
        self.notify(f"Scanning track with {car.nickname}: it will drive two slow laps")

    async def cancel_track_scan(self) -> None:
        if self.scan_job:
            car = self.cars.get(self.scan_job["car"])
            if car:
                car.scanner = None
                await car.stop()
        self.scan_job = None

    async def _finish_scan(self, car: Car) -> None:
        scanner, car.scanner, self.scan_job = car.scanner, None, None
        await car.stop()
        try:
            import json
            logdir = self.lib.root / "scan-logs"
            logdir.mkdir(exist_ok=True)
            (logdir / f"scan-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps({
                "car": car.address, "model": car.model, "firmware": car.version,
                "laps": scanner.laps, "messages": scanner.raw}), encoding="utf-8")
        except OSError as e:
            log.warning("could not save scan log: %s", e)
        track = scanner.result(self.lib.next_track_name())
        if not track:
            self.notify("Scan failed: no full lap recorded", "error")
            return
        same = self.lib.find_same_track(track)
        if same:
            # same pieces as a saved track: keep its name, but take the new (better) readings
            track.name, track.created = self.lib.tracks[same].name, self.lib.tracks[same].created
            self.lib.save_track(track, same)
            self.select_track(same)
            self.push_catalog()
            self.notify(f"This is your saved track \"{track.name}\". Updated it with the new scan.", "success")
            return
        key = self.lib.save_track(track)
        self.select_track(key)
        self.push_catalog()
        msg = f"Saved new track \"{track.name}\" ({len(track.pieces)} pieces)"
        if track.needs_review:
            msg += ". Some turns could not be worked out; check them in the track editor."
        self.notify(msg, "success")

    def _auto_recognize(self) -> None:
        """No track selected: if a car's recent pieces fit exactly one saved track, load it."""
        if self.track or not self.lib.tracks or self.scan_job:
            return
        for car in self.cars.values():
            if len(car.loc.recent) < 5:
                continue
            hits = [k for k, t in self.lib.tracks.items() if t.match([r[0] for r in car.loc.recent[-6:]])]
            if len(hits) == 1:
                self.select_track(hits[0])
                self.notify(f"Recognized track \"{self.lib.tracks[hits[0]].name}\". No rescan needed.", "success")
                return

    def edit_track(self, key: str, name: Optional[str] = None, turns: Optional[dict] = None,
                   mirror: bool = False, fix: bool = False) -> None:
        t = self.lib.tracks.get(key)
        if not t:
            return
        if name:
            t.name = name
        if turns:
            for i, turn in turns.items():
                i = int(i)
                if 0 <= i < len(t.pieces) and t.pieces[i].kind == "turn":
                    t.pieces[i].turn = turn
        if mirror:
            t.mirror()
        if fix:
            t.solve_turns()
        t.layout()
        t.needs_review = not t.valid()
        self.lib.save_track(t, key)
        if key == self.track_key:
            self.select_track(key)
        self.push_catalog()

    # ---------------------------------------------------------------- games
    def start_game(self, mode_key: str, addresses: list[str], overrides: Optional[dict] = None) -> None:
        mode = self.lib.modes.get(mode_key)
        addresses = [a for a in addresses if a in self.cars and self.cars[a].connected]
        if not mode or not addresses:
            self.notify("Pick a mode and at least one connected car", "warn")
            return
        if mode["weapons"] and not self.track:
            self.notify("Weapons need a track map. Scan or select a track first.", "warn")
            return
        rules = {**mode, **(overrides or {})}
        for a in addresses:
            self.cars[a].loc.reset_race()
            if self.controllers.get(a, "none") == "none":
                self.set_controller(a, "human")
        self.game = Game(rules, addresses)
        self.notify(f"{rules['name']} starting with {len(addresses)} car(s)")

    async def stop_game(self) -> None:
        self.game = None
        await self.stop_all()

    async def stop_all(self) -> None:
        for d in self.drivers.values():
            if isinstance(d, HumanDriver):
                d.throttle = 0
        await asyncio.gather(*(c.stop() for c in self.cars.values()), return_exceptions=True)

    def _on_lap(self, address: str, lap_s: float) -> None:
        if self.game:
            self.game.on_lap(address, lap_s, self.cars)

    # ---------------------------------------------------------------- loop
    def start(self) -> None:
        if not self._task:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        last_batt = 0.0
        while True:
            t0 = time.monotonic()
            try:
                await self._tick()
                if t0 - last_batt > 20:
                    last_batt = t0
                    for c in self.cars.values():
                        await c.request_battery()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("tick failed")
            await asyncio.sleep(max(0.0, TICK - (time.monotonic() - t0)))

    async def _tick(self) -> None:
        now = time.monotonic()
        dt = min(0.2, now - getattr(self, "_last_tick", now)) if hasattr(self, "_last_tick") else TICK
        self._last_tick = now
        for car in self.cars.values():
            car.update_estimate(dt)
        self._auto_recognize()
        g = self.game
        track_len = self.track.length_mm if self.track else 0
        if self.scan_job:
            car = self.cars.get(self.scan_job["car"])
            if not car or not car.connected:
                self.scan_job = None
            elif car.scanner and car.scanner.done:
                await self._finish_scan(car)
            elif car.scanner:
                car.drive(SCAN_SPEED, 600, 0.0)
                await car.flush()
            if time.monotonic() - (self.scan_job or {}).get("started", time.monotonic()) > 120:
                self.notify("Scan timed out. Is the car on the track?", "error")
                await self.cancel_track_scan()
        if g:
            g.tick(TICK, self.cars)
        for a, car in self.cars.items():
            if not car.connected or car.scanner:
                continue
            driver = self.drivers.get(a)
            playing = g is not None and a in g.players
            if g is not None and not playing:
                continue
            if driver is None or driver.kind == "none":
                continue
            ctx = self._context(car, g, track_len)
            act = driver.decide(ctx)
            speed = act.speed
            # safety: slow until the app knows where the car is, and after it came off the track
            if car.recovering and car.loc.index is not None and car.loc.confidence >= 2:
                car.recovering = False
            if driver.kind == "ai" and (car.loc.index is None or car.recovering):
                speed = min(speed, 450)
            elif car.recovering:
                speed = min(speed, 600)
            if g:
                speed = g.limit(a, speed)
                if act.fire:
                    g.try_fire(a, self.cars, track_len)
                if act.mine:
                    g.try_mine(a, self.cars)
            car.drive(speed, act.accel, act.lane)
            await car.flush()
        if g:
            for ev in g.events:
                self._game_event(ev)
            g.events.clear()
            if g.state == "finished" and g.ended and time.monotonic() - g.ended > 0.1 and not getattr(g, "_stopped", False):
                g._stopped = True
                for a in g.players:
                    if a in self.cars:
                        await self.cars[a].stop()

    def _context(self, car: Car, g: Optional[Game], track_len: float) -> Context:
        loc = car.loc
        cur = loc.current_piece
        flip = loc.direction == -1

        def turn(p):
            if p.turn and flip:
                return "L" if p.turn == "R" else "R"
            return p.turn
        rivals = []
        for b, other in self.cars.items():
            if b == car.address or not other.connected:
                continue
            if g and b not in g.players:
                continue
            gap = forward_gap(car, other, track_len)
            if gap is None:
                continue
            pl = g.players.get(b) if g else None
            rivals.append(Rival(b, gap, other.offset, is_king=bool(g and g.king == b), hp=pl.hp if pl else 100))
        me = g.players.get(car.address) if g else None
        return Context(
            speed=car.speed, lane=car.offset, piece_kind=cur.kind if cur else None,
            this_turn=turn(cur) if cur else None,
            upcoming=[(p.kind, turn(p)) for p in loc.lookahead(3)], rivals=rivals,
            speed_cap=g.rules["speed_cap"] if g else 2000, weapons=bool(g and g.rules["weapons"]),
            energy=me.energy if me else 0, fire_cost=g.rules["fire_cost"] if g else 25,
            am_king=bool(g and g.king == car.address), mode=g.base if g else "free", dt=TICK)

    def _game_event(self, ev: dict) -> None:
        k = ev["kind"]
        car = self.cars.get(ev.get("car", ""))
        loop = asyncio.get_running_loop()
        if k == "hit" and car:
            loop.create_task(car.flash("#ff0000", 0.8, 40))
        elif k == "knockout" and car:
            loop.create_task(car.flash("#ff0000", self.game.rules["knockout_s"] if self.game else 3, 20))
            self.notify(f"{self.name(ev['by'])} knocked out {self.name(ev['car'])}!")
        elif k == "crown" and car:
            loop.create_task(car.flash("#ffd700", 1.5, 30))
            self.notify(f"{self.name(ev['car'])} took the crown")
        elif k == "eliminated":
            self.notify(f"{self.name(ev['car'])} is eliminated")
        elif k == "finish":
            w = ev.get("winner")
            self.notify(f"{self.name(w)} wins!" if w else "Game over", "success")
            if w in self.cars:
                loop.create_task(self.cars[w].flash(self.cars[w].color, 6, 60))
        elif k == "go":
            self.notify("GO!")
        for fn in self.listeners:
            fn({"type": "game_event", **ev})

    # ---------------------------------------------------------------- state for the UI
    def snapshot(self) -> dict:
        names = {a: c.nickname for a, c in self.cars.items()}
        return {
            "type": "state",
            "t": time.time(),
            "cars": [c.to_dict() | {"controller": self.controllers.get(a, "none")} for a, c in self.cars.items()],
            "game": self.game.to_dict(self.cars, names) if self.game else None,
            "track_key": self.track_key,
            "scan": self._scan_view(),
        }

    def _scan_view(self) -> Optional[dict]:
        """Live scan state, including the partial track. Track data is only resent when it changes."""
        if not self.scan_job:
            return None
        car = self.cars.get(self.scan_job["car"])
        if not car or not car.scanner:
            return {"car": self.scan_job["car"], "pieces": 0, "laps": 0, "rev": 0}
        track, index = car.scanner.preview()
        sig = track.signature() + str(len(car.scanner.laps)) if track else ""
        job = self.scan_job
        if sig != job.get("sig"):
            job["sig"], job["rev"] = sig, job.get("rev", 0) + 1
            job["track"] = track.to_dict() if track else None
        frac = 0.0
        if track and track.pieces:
            if not car.scanner.laps and car.scanner.waiting_for_piece:
                # new piece not reported yet: hold the car at the end of the last one (no jump back)
                frac = 0.99
            else:
                L = track.pieces[index].length_mm
                frac = min(0.99, (time.monotonic() - car.loc.piece_t0) * max(0, car.speed) / L)
        return {"car": job["car"], "pieces": len(track.pieces) if track else 0, "laps": len(car.scanner.laps),
                "rev": job.get("rev", 0), "track": job.get("track"), "index": index, "frac": round(frac, 3),
                "closed": bool(track and car.scanner.laps)}

    def catalog(self) -> dict:
        """Slow-changing data, sent on connect and whenever something is saved."""
        return {
            "type": "catalog",
            "simulator": self.sim is not None,
            "max_connected": MAX_CONNECTED,
            "tracks": {k: t.to_dict() for k, t in self.lib.tracks.items()},
            "racers": self.lib.racers,
            "modes": self.lib.modes,
            "garage": self.lib.garage,
            "adverts": [{"address": a.address, "name": a.name, "model": P.model_name(a.model_id),
                         "tag": self.lib.tag(a.address), "charge": self.charge.get(a.address),
                         "rssi": a.rssi, "on_charger": a.on_charger, "simulated": a.simulated}
                        for a in self.adverts.values()],
            "ble_error": self.ble_error,
            "log": self.log[-40:],
        }
