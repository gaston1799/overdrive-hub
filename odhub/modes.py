"""Game modes. Every mode is a rule set; custom modes are saved rule sets (data/modes/*.json).

Bases:
  race         first to N laps
  time_trial   best single lap wins (all cars run at once, or one at a time)
  battle       virtual blasters + mines, HP, knockouts; most knockouts when time runs out
  king         hold the crown to score; hit the king to steal it
  elimination  each lap the last car is knocked out; last car running wins
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

BASE_RULES = {
    "base": "race",
    "laps": 5,
    "time_limit_s": 0,          # 0 = none
    "speed_cap": 1300,          # mm/s for everyone (humans and AI)
    "weapons": False,
    "hp": 100,
    "blaster_damage": 20,
    "blaster_range_mm": 900,
    "fire_cost": 25,
    "energy_regen": 12,         # per second
    "mine_damage": 30,
    "mine_cost": 50,
    "knockout_s": 4,            # stopped time after HP hits 0
    "hit_slow_s": 1.0,          # speed penalty after a hit
    "king_points_per_s": 1,
    "score_to_win": 0,          # knockouts (battle) or points (king); 0 = time limit only
    "countdown_s": 3,
}

BUILTIN_MODES = {
    "race": {**BASE_RULES, "name": "Race", "description": "First to the lap count wins.", "base": "race"},
    "time_trial": {**BASE_RULES, "name": "Time Trial", "description": "Fastest single lap wins.",
                   "base": "time_trial", "laps": 3},
    "battle": {**BASE_RULES, "name": "Battle", "description": "Blasters and mines. Most knockouts wins.",
               "base": "battle", "weapons": True, "time_limit_s": 180, "laps": 0, "speed_cap": 1100},
    "king": {**BASE_RULES, "name": "King of the Hill",
             "description": "Hold the crown to score. Hit the king to take it.",
             "base": "king", "weapons": True, "time_limit_s": 180, "score_to_win": 60, "laps": 0,
             "speed_cap": 1100},
    "elimination": {**BASE_RULES, "name": "Elimination",
                    "description": "Last car over the line each lap is out.", "base": "elimination",
                    "laps": 0},
    "free": {**BASE_RULES, "name": "Free Drive", "description": "No rules. Just drive.", "base": "free",
             "laps": 0, "countdown_s": 0},
}


@dataclass
class Player:
    address: str
    hp: float = 100
    energy: float = 50
    score: float = 0
    knockouts: int = 0
    out: bool = False                 # eliminated / finished
    finished_at: Optional[float] = None
    disabled_until: float = 0.0
    slowed_until: float = 0.0
    place: int = 0


@dataclass
class Mine:
    owner: str
    index: int
    lane: float
    expires: float


class Game:
    def __init__(self, rules: dict, addresses: list[str]):
        self.rules = {**BASE_RULES, **rules}
        self.base = self.rules["base"]
        self.players = {a: Player(a, hp=self.rules["hp"]) for a in addresses}
        self.state = "countdown" if self.rules["countdown_s"] > 0 else "running"
        self.created = time.monotonic()
        self.started: Optional[float] = None if self.state == "countdown" else self.created
        self.ended: Optional[float] = None
        self.king: Optional[str] = random.choice(addresses) if self.base == "king" and addresses else None
        self.mines: list[Mine] = []
        self.events: list[dict] = []       # drained by the engine and pushed to the UI
        self.winner: Optional[str] = None
        self._elim_lap = 0

    # ---------------------------------------------------------------- timing
    @property
    def elapsed(self) -> float:
        return 0.0 if self.started is None else (self.ended or time.monotonic()) - self.started

    @property
    def countdown_left(self) -> float:
        if self.state != "countdown":
            return 0.0
        return max(0.0, self.rules["countdown_s"] - (time.monotonic() - self.created))

    def event(self, kind: str, **data) -> None:
        self.events.append({"kind": kind, **data})

    # ---------------------------------------------------------------- per tick
    def tick(self, dt: float, cars: dict) -> None:
        now = time.monotonic()
        if self.state == "countdown":
            if self.countdown_left <= 0:
                self.state, self.started = "running", now
                self.event("go")
            return
        if self.state != "running":
            return
        r = self.rules
        for p in self.players.values():
            p.energy = min(100.0, p.energy + r["energy_regen"] * dt)
            if p.disabled_until and now >= p.disabled_until:
                p.disabled_until = 0
                p.hp = r["hp"]
                self.event("respawn", car=p.address)
        if self.base == "king" and self.king in self.players:
            k = self.players[self.king]
            if not k.disabled_until:
                k.score += r["king_points_per_s"] * dt
        self.mines = [m for m in self.mines if m.expires > now]
        # mines
        for m in list(self.mines):
            for a, car in cars.items():
                p = self.players.get(a)
                if not p or a == m.owner or p.out or p.disabled_until:
                    continue
                if car.loc.index == m.index and abs(car.offset - m.lane) < 30:
                    self.mines.remove(m)
                    self._damage(m.owner, a, r["mine_damage"], "mine")
                    break
        self._rank(cars)
        self._check_end(cars)

    def _rank(self, cars: dict) -> None:
        if self.base == "time_trial":
            def key(a):
                best = cars[a].loc.best_lap_s if a in cars else None
                return best if best is not None else 1e9
            order = sorted(self.players, key=key)
        elif self.base in ("battle",):
            order = sorted(self.players, key=lambda a: (-self.players[a].knockouts, self.players[a].hp * -1))
        elif self.base == "king":
            order = sorted(self.players, key=lambda a: -self.players[a].score)
        else:
            def key(a):
                p = self.players[a]
                if p.finished_at is not None:
                    return (0, p.finished_at)
                if p.out:
                    return (2, -(p.finished_at or 0))
                return (1, -(cars[a].loc.progress(cars[a].speed) if a in cars else 0))
            order = sorted(self.players, key=key)
        for i, a in enumerate(order):
            self.players[a].place = i + 1

    def _check_end(self, cars: dict) -> None:
        r = self.rules
        if r["time_limit_s"] and self.elapsed >= r["time_limit_s"]:
            self.finish(min(self.players, key=lambda a: self.players[a].place) if self.players else None)
            return
        if self.base == "battle" and r["score_to_win"]:
            for a, p in self.players.items():
                if p.knockouts >= r["score_to_win"]:
                    return self.finish(a)
        if self.base == "king" and r["score_to_win"]:
            for a, p in self.players.items():
                if p.score >= r["score_to_win"]:
                    return self.finish(a)
        if self.base in ("race", "time_trial") and r["laps"]:
            if all(p.finished_at is not None or p.out for p in self.players.values()):
                self.finish(min(self.players, key=lambda a: self.players[a].place))
        if self.base == "elimination":
            alive = [a for a, p in self.players.items() if not p.out]
            if len(alive) <= 1 and len(self.players) > 1:
                self.finish(alive[0] if alive else None)
            elif len(self.players) == 1 and r["laps"] and cars[alive[0]].loc.laps >= r["laps"]:
                self.finish(alive[0])

    def finish(self, winner: Optional[str]) -> None:
        if self.state == "finished":
            return
        self.state, self.ended, self.winner = "finished", time.monotonic(), winner
        self.event("finish", winner=winner)

    # ---------------------------------------------------------------- hooks
    def on_lap(self, address: str, lap_s: float, cars: dict) -> None:
        p = self.players.get(address)
        if not p or self.state != "running":
            return
        laps = cars[address].loc.laps
        self.event("lap", car=address, lap=laps, time=round(lap_s, 3))
        r = self.rules
        if self.base in ("race", "time_trial") and r["laps"] and laps >= r["laps"] and p.finished_at is None:
            p.finished_at = time.monotonic()
            p.out = True
            self.event("finished", car=address)
            if self.base == "race" and self.winner is None:
                self.winner = address
        if self.base == "elimination":
            leader_laps = max((cars[a].loc.laps for a in self.players if a in cars), default=0)
            if leader_laps > self._elim_lap:
                self._elim_lap = leader_laps
                alive = [a for a, q in self.players.items() if not q.out]
                if len(alive) > 1:
                    last = min(alive, key=lambda a: cars[a].loc.progress(cars[a].speed))
                    self.players[last].out = True
                    self.players[last].finished_at = None
                    self.event("eliminated", car=last)

    def try_fire(self, shooter: str, cars: dict, track_len: float) -> None:
        p = self.players.get(shooter)
        r = self.rules
        if not r["weapons"] or not p or p.disabled_until or p.out or p.energy < r["fire_cost"]:
            return
        p.energy -= r["fire_cost"]
        me = cars[shooter]
        best, best_gap = None, None
        for a, car in cars.items():
            q = self.players.get(a)
            if a == shooter or not q or q.out or q.disabled_until:
                continue
            gap = forward_gap(me, car, track_len)
            if gap is not None and 0 < gap <= r["blaster_range_mm"] and abs(car.offset - me.offset) < 30:
                if best_gap is None or gap < best_gap:
                    best, best_gap = a, gap
        self.event("fire", car=shooter, hit=best)
        if best:
            self._damage(shooter, best, r["blaster_damage"], "blaster")

    def try_mine(self, owner: str, cars: dict) -> None:
        p = self.players.get(owner)
        r = self.rules
        car = cars.get(owner)
        if not r["weapons"] or not p or p.out or p.energy < r["mine_cost"] or not car or car.loc.index is None:
            return
        p.energy -= r["mine_cost"]
        self.mines.append(Mine(owner, car.loc.index, car.offset, time.monotonic() + 30))
        self.event("mine", car=owner, index=car.loc.index, lane=car.offset)

    def _damage(self, attacker: str, victim: str, amount: float, how: str) -> None:
        v = self.players[victim]
        now = time.monotonic()
        v.hp = max(0.0, v.hp - amount)
        v.slowed_until = now + self.rules["hit_slow_s"]
        self.event("hit", car=victim, by=attacker, how=how, hp=v.hp)
        if self.base == "king" and victim == self.king:
            self.king = attacker
            self.event("crown", car=attacker)
        if v.hp <= 0:
            v.disabled_until = now + self.rules["knockout_s"]
            if attacker in self.players:
                self.players[attacker].knockouts += 1
            self.event("knockout", car=victim, by=attacker)

    # ---------------------------------------------------------------- speed filter
    def limit(self, address: str, speed: int) -> int:
        p = self.players.get(address)
        if p is None:
            return speed
        now = time.monotonic()
        if self.state == "countdown" or self.state == "finished" or p.out or p.disabled_until:
            return 0
        speed = min(speed, self.rules["speed_cap"])
        if p.slowed_until > now:
            speed = int(speed * 0.55)
        return speed

    def to_dict(self, cars: dict, names: dict) -> dict:
        return {
            "name": self.rules.get("name", self.base), "base": self.base, "state": self.state,
            "elapsed": round(self.elapsed, 2), "countdown": round(self.countdown_left, 2),
            "time_limit": self.rules["time_limit_s"], "laps": self.rules["laps"],
            "weapons": self.rules["weapons"], "king": self.king, "winner": self.winner,
            "mines": [{"index": m.index, "lane": m.lane, "owner": m.owner} for m in self.mines],
            "players": sorted([{
                "address": a, "name": names.get(a, a), "place": p.place, "hp": round(p.hp),
                "max_hp": self.rules["hp"], "energy": round(p.energy), "score": round(p.score, 1),
                "knockouts": p.knockouts, "out": p.out, "finished": p.finished_at is not None,
                "disabled": bool(p.disabled_until),
                "laps": cars[a].loc.laps if a in cars else 0,
                "best_lap": cars[a].loc.best_lap_s if a in cars else None,
                "last_lap": cars[a].loc.last_lap_s if a in cars else None,
            } for a, p in self.players.items()], key=lambda x: x["place"]),
        }


def forward_gap(me, other, track_len: float) -> Optional[float]:
    """Distance (mm) from `me` forward to `other` along the track, using lap progress."""
    if me.loc.index is None or other.loc.index is None or track_len <= 0:
        return None
    a = me.loc.progress(me.speed) % 1.0
    b = other.loc.progress(other.speed) % 1.0
    d = (b - a) % 1.0
    if d > 0.5:
        d -= 1.0
    return d * track_len
