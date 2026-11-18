"""Who drives a car: a human (input from the UI) or an AI racer.

AI racers are data: a profile of numbers (speeds, aggression, lane habits...). The built-in
roster is defined below; your own racers are saved as JSON in data/racers and built in the app.
Advanced: drop a .py file in data/racers that defines `class Racer(AIDriver)` to write custom logic.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from . import protocol as P

DIFFICULTY = {"easy": 0.72, "normal": 0.88, "hard": 1.0, "extreme": 1.12}

DEFAULT_PROFILE = {
    "name": "Racer", "description": "", "color": "#cccccc",
    "top_speed": 1100,      # mm/s on straights
    "curve_speed": 750,     # mm/s through turns
    "accel": 900,           # mm/s^2
    "lane": "center",       # inside | outside | center | left | right | any
    "aggression": 0.5,      # 0..1  chase/fire/overtake eagerness
    "defense": 0.3,         # 0..1  block cars behind
    "consistency": 0.85,    # 0..1  1 = robotic, 0 = erratic
    "trigger_happy": 0.5,   # 0..1  how freely it spends weapon energy
}

BUILTIN_RACERS = {
    "steady": {**DEFAULT_PROFILE, "name": "Steady", "color": "#4caf50",
               "description": "Smooth and predictable. Never crashes, rarely wins big.",
               "top_speed": 950, "curve_speed": 700, "aggression": 0.2, "defense": 0.2, "consistency": 0.97,
               "trigger_happy": 0.3},
    "nitro": {**DEFAULT_PROFILE, "name": "Nitro", "color": "#ff9800",
              "description": "Flat out on straights, brakes late into turns.",
              "top_speed": 1350, "curve_speed": 820, "accel": 1400, "lane": "inside", "aggression": 0.6,
              "consistency": 0.8},
    "wall": {**DEFAULT_PROFILE, "name": "The Wall", "color": "#9e9e9e",
             "description": "Defensive. Sits in your lane when you try to pass.",
             "top_speed": 1000, "curve_speed": 720, "defense": 0.95, "aggression": 0.3},
    "viper": {**DEFAULT_PROFILE, "name": "Viper", "color": "#e91e63",
              "description": "Hunter. Lines up behind the car ahead and opens fire.",
              "top_speed": 1150, "curve_speed": 760, "lane": "any", "aggression": 0.95, "trigger_happy": 0.9},
    "rookie": {**DEFAULT_PROFILE, "name": "Rookie", "color": "#03a9f4",
               "description": "Just learning. Slow and wobbly. Good first opponent.",
               "top_speed": 750, "curve_speed": 600, "accel": 600, "aggression": 0.1, "consistency": 0.55,
               "trigger_happy": 0.2},
    "phantom": {**DEFAULT_PROFILE, "name": "Phantom", "color": "#7c4dff",
                "description": "Fast, tidy racing lines and well-timed overtakes.",
                "top_speed": 1300, "curve_speed": 850, "accel": 1200, "lane": "inside", "aggression": 0.7,
                "defense": 0.5, "consistency": 0.95, "trigger_happy": 0.6},
}


@dataclass
class Action:
    speed: int = 0
    accel: int = 900
    lane: Optional[float] = None
    fire: bool = False
    mine: bool = False


@dataclass
class Rival:
    address: str
    gap_mm: float          # + ahead of me, - behind me (along the track)
    lane: float
    is_king: bool = False
    hp: float = 100


@dataclass
class Context:
    speed: int
    lane: float
    piece_kind: Optional[str]
    upcoming: list[tuple[str, Optional[str]]]      # (kind, turn L/R in my driving direction)
    this_turn: Optional[str] = None                # turn direction of the piece I am on
    rivals: list[Rival] = field(default_factory=list)
    speed_cap: int = 2000
    weapons: bool = False
    energy: float = 0
    fire_cost: float = 25
    am_king: bool = False
    mode: str = "race"
    dt: float = 0.05


class Driver:
    kind = "none"

    def decide(self, ctx: Context) -> Action:
        return Action(speed=0)


class HumanDriver(Driver):
    kind = "human"

    def __init__(self):
        self.throttle = 0.0          # 0..1
        self.lane_index: Optional[int] = None
        self.fire = False
        self.mine = False
        self.max_speed = 1300

    def decide(self, ctx: Context) -> Action:
        a = Action(speed=int(self.throttle * self.max_speed) if self.throttle > 0.02 else 0, accel=1200)
        if self.lane_index is not None:
            a.lane = P.LANES[max(0, min(3, self.lane_index))]
        a.fire, a.mine = self.fire, self.mine
        self.fire = self.mine = False
        return a


class AIDriver(Driver):
    kind = "ai"

    def __init__(self, profile: dict, difficulty: str = "normal"):
        self.p = {**DEFAULT_PROFILE, **profile}
        self.scale = DIFFICULTY.get(difficulty, 0.88)
        self.difficulty = difficulty
        self._noise = 0.0
        self._lane_hold = 0.0
        self._lane: Optional[float] = None
        self._fire_cool = 0.0
        self._mine_cool = 5.0

    def _preferred_lane(self, ctx: Context) -> float:
        pref = self.p["lane"]
        next_turn = next((t for k, t in ctx.upcoming if k == "turn" and t), None)
        if pref == "inside" and next_turn:
            return P.LANES[3] if next_turn == "R" else P.LANES[0]
        if pref == "outside" and next_turn:
            return P.LANES[0] if next_turn == "R" else P.LANES[3]
        if pref == "left":
            return P.LANES[0]
        if pref == "right":
            return P.LANES[3]
        if pref == "any":
            return self._lane if self._lane is not None else ctx.lane
        return P.LANES[1] if ctx.lane < 0 else P.LANES[2]

    def decide(self, ctx: Context) -> Action:
        p, s = self.p, self.scale
        # --- speed: brake for turns ahead, push on straights
        top, curve = p["top_speed"] * s, p["curve_speed"] * s
        in_turn = ctx.piece_kind == "turn"
        turn_next = bool(ctx.upcoming) and ctx.upcoming[0][0] == "turn"
        if in_turn:
            target = curve
            # S-bends (a turn straight into the opposite turn) throw cars off: ease off a bit more
            if ctx.upcoming and ctx.upcoming[0][0] == "turn" and ctx.upcoming[0][1] != ctx.this_turn:
                target *= 0.9
        elif turn_next:
            target = curve + (top - curve) * 0.15
        else:
            target = top
        if not ctx.upcoming and ctx.piece_kind is None:      # not located yet: play safe
            target = min(top, curve * 0.8)
        # wobble for less consistent racers
        if random.random() < 0.05:
            self._noise = random.uniform(-1, 1) * (1 - p["consistency"]) * 350
        target += self._noise

        lane = self._preferred_lane(ctx)
        ahead = sorted([r for r in ctx.rivals if 0 < r.gap_mm < 900], key=lambda r: r.gap_mm)
        behind = sorted([r for r in ctx.rivals if -700 < r.gap_mm < 0], key=lambda r: -r.gap_mm)

        # --- traffic: overtake or follow
        if ahead:
            r = ahead[0]
            same_lane = abs(r.lane - ctx.lane) < 30
            if ctx.weapons and p["aggression"] > 0.6:
                lane = r.lane                                # line up to shoot
            elif same_lane and r.gap_mm < 450:
                if random.random() < p["aggression"]:
                    lane = P.LANES[1] if r.lane > 0 else P.LANES[2]   # dive to the other side
                    if abs(lane - r.lane) < 40:
                        lane = P.LANES[0] if r.lane > 0 else P.LANES[3]
                else:
                    target = min(target, ctx.speed * 0.95)          # sit behind
        elif behind and random.random() < p["defense"]:
            lane = behind[0].lane                                   # block

        # don't spam lane changes
        self._lane_hold -= ctx.dt
        if self._lane is None or (abs(lane - self._lane) > 10 and self._lane_hold <= 0):
            self._lane, self._lane_hold = lane, 1.5
        a = Action(speed=int(max(0, min(ctx.speed_cap, target))), accel=int(p["accel"]), lane=self._lane)

        # --- weapons
        if ctx.weapons:
            self._fire_cool -= ctx.dt
            self._mine_cool -= ctx.dt
            want_target = [r for r in ahead if abs(r.lane - ctx.lane) < 30]
            if ctx.mode == "king":
                want_target = [r for r in want_target if r.is_king] or want_target
            reserve = ctx.fire_cost * (2.5 - 2 * p["trigger_happy"])
            if want_target and ctx.energy >= max(ctx.fire_cost, reserve) and self._fire_cool <= 0:
                a.fire, self._fire_cool = True, 0.6
            if behind and ctx.energy > 60 and self._mine_cool <= 0 and random.random() < p["aggression"] * 0.3:
                a.mine, self._mine_cool = True, 8.0
        return a


def make_driver(spec: str, racers: dict[str, dict], plugins: dict[str, type] | None = None) -> Driver:
    """spec: 'none' | 'human' | 'ai:<racer_id>:<difficulty>'"""
    if spec == "human":
        return HumanDriver()
    if spec.startswith("ai:"):
        _, rid, *rest = spec.split(":")
        diff = rest[0] if rest else "normal"
        if plugins and rid in plugins:
            return plugins[rid](racers.get(rid, {}), diff)
        return AIDriver(racers.get(rid, BUILTIN_RACERS["steady"]), diff)
    return Driver()
