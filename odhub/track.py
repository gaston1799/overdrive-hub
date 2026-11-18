"""Track model, scanner, auto-recognition and per-car localisation.

How a car reports pieces (matches Anki-Partydrive's MessageManager/TrackScanner):
  * position updates (0x27) carry the id of the piece the car is on plus a "reverse parsing" flag;
  * a transition (0x29) marks leaving a piece. The piece just left is the LAST id seen since the
    previous transition (the first position after a transition can still carry the old id);
  * the reverse flag gives a curve's direction (flag set = right turn) and the facing of
    straights / the start line; wheel distances are only a fallback for unrecognised ids.

Pieces sit on a 560 mm grid. Headings: 0=+x, 1=+z, 2=-x, 3=-z; a right turn adds 1.
"""
from __future__ import annotations

import itertools
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional

from . import protocol as P

CELL_MM = 560.0
CURVE_LEN_MM = math.pi / 2 * (CELL_MM / 2)
HEADINGS = ((1, 0), (0, 1), (-1, 0), (0, -1))

START_IDS = (33, 34)
KIND_BY_ID = {
    10: "crisscross", 17: "turn", 18: "turn", 20: "turn", 23: "turn", 24: "turn", 27: "turn",
    33: "start", 34: "start", 36: "straight", 39: "straight", 40: "straight", 48: "straight",
    51: "straight", 43: "jump", 58: "jump", 46: "landing", 63: "landing",
    53: "special", 54: "special", 57: "special",        # Fast & Furious bonus piece
}
CELLS = {"jump": 2, "landing": 0}


def kind_of(piece_id: int) -> str:
    return KIND_BY_ID.get(piece_id, "straight")


def norm_id(piece_id: int) -> int:
    """Both halves of the start piece count as one piece."""
    return 34 if piece_id in START_IDS else piece_id


# a scanned segment: (piece id or 0 if unrecognised, kind, turn 'L'/'R'/None, reversed flag)
Seg = tuple[int, str, Optional[str], bool]


@dataclass
class Piece:
    id: int
    kind: str
    turn: Optional[str] = None          # 'L' / 'R' for turns
    reversed: bool = False              # piece is driven against its printed direction
    x: int = 0                          # grid cell
    z: int = 0
    heading_in: int = 0
    heading_out: int = 0

    @property
    def cells(self) -> int:
        return CELLS.get(self.kind, 1)

    @property
    def length_mm(self) -> float:
        return CURVE_LEN_MM if self.kind == "turn" else max(1.0, CELL_MM * self.cells)


@dataclass
class Track:
    name: str
    pieces: list[Piece]
    created: float = field(default_factory=time.time)
    closed: bool = False
    needs_review: bool = False

    def __post_init__(self):
        self.layout()

    # ---------------------------------------------------------------- geometry
    def layout(self) -> bool:
        x = z = h = 0
        for p in self.pieces:
            p.x, p.z, p.heading_in = x, z, h
            if p.kind == "turn" and p.turn in ("L", "R"):
                p.reversed = p.turn == "R"          # real cars: reverse flag set <=> right turn
                h = (h + (1 if p.turn == "R" else -1)) % 4
            p.heading_out = h
            dx, dz = HEADINGS[h]
            x, z = x + dx * p.cells, z + dz * p.cells
        self.closed = (x, z, h) == (0, 0, 0)
        return self.closed

    def valid(self) -> bool:
        """Closed loop, and no two pieces share a grid cell, except a crossover driven through
        twice at right angles (the '+' piece)."""
        if not self.layout():
            return False
        cells: dict[tuple[int, int], list[Piece]] = {}
        for p in self.pieces:
            if p.cells:
                cells.setdefault((p.x, p.z), []).append(p)
        for ps in cells.values():
            if len(ps) == 1:
                continue
            if (len(ps) == 2 and all(p.kind == "crisscross" for p in ps)
                    and ps[0].heading_out % 2 != ps[1].heading_out % 2):
                continue
            # a curve only uses one corner of its square: two curves around opposite corners fit
            if len(ps) == 2 and all(p.kind == "turn" for p in ps):
                a, b = (_corner(p) for p in ps)
                if a[0] == -b[0] and a[1] == -b[1]:
                    continue
            return False
        return True

    def solve_turns(self) -> bool:
        """Fallback when the readings do not give a buildable layout: change as few turn directions
        as possible, strongly preferring layouts where the two passes over a '+' piece meet."""
        if self.valid():
            return True
        turns = [p for p in self.pieces if p.kind == "turn"]
        if len(turns) > 18:
            self.layout()
            return False
        measured = [p.turn for p in turns]
        cross = [p for p in self.pieces if p.kind == "crisscross"]
        best, best_cost = None, None
        for combo in itertools.product("LR", repeat=len(turns)):
            cost = sum(1 for m, c in zip(measured, combo) if m is not None and m != c)
            if best_cost is not None and cost >= best_cost:
                continue
            for p, c in zip(turns, combo):
                p.turn = c
            if self.valid():
                shared = len(cross) - len({(p.x, p.z) for p in cross})
                if len(cross) == 2 and shared == 0:
                    continue        # one '+' piece driven twice: the two passes must meet
                if best_cost is None or cost < best_cost:
                    best, best_cost = combo, cost
        for p, c in zip(turns, best or measured):
            p.turn = c
        self.layout()
        return best is not None

    def mirror(self) -> None:
        for p in self.pieces:
            if p.kind == "turn" and p.turn in ("L", "R"):
                p.turn = "L" if p.turn == "R" else "R"
        self.layout()

    @property
    def length_mm(self) -> float:
        return sum(p.length_mm for p in self.pieces)

    @property
    def start_reversed(self) -> Optional[bool]:
        s = next((p for p in self.pieces if p.kind == "start"), None)
        return s.reversed if s else None

    # ---------------------------------------------------------------- identity
    @property
    def ids(self) -> list[int]:
        return [p.id for p in self.pieces]

    def signature(self) -> str:
        return "-".join(f"{p.id}{p.turn or ''}" for p in self.pieces)

    def same_layout(self, other: "Track") -> bool:
        a, b = self.ids, other.ids
        if len(a) != len(b):
            return False
        return any(_cyclic_equal(a, seq) for seq in (b, list(reversed(b))))

    def match(self, recent: list[int]) -> Optional[tuple[int, int]]:
        """Find where a car is from the pieces it recently finished (oldest first; 0 = unknown).
        Returns (index of the newest finished piece, direction +1/-1) when the match is unique."""
        if len([r for r in recent if r]) < 3:
            return None
        ids, n = self.ids, len(self.pieces)
        hits = []
        for d in (1, -1):
            for end in range(n):
                if all(r == 0 or ids[(end - d * k) % n] == r for k, r in enumerate(reversed(recent))):
                    hits.append((end, d))
        return hits[0] if len(hits) == 1 else None

    # ---------------------------------------------------------------- storage
    def to_dict(self) -> dict:
        valid = self.valid()
        return {"name": self.name, "created": self.created, "closed": self.closed, "valid": valid,
                "needs_review": self.needs_review, "length_mm": round(self.length_mm),
                "start_reversed": self.start_reversed,
                "pieces": [asdict(p) | {"cells": p.cells} for p in self.pieces]}

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        pieces = [Piece(id=p["id"], kind=p.get("kind") or kind_of(p["id"]), turn=p.get("turn"),
                        reversed=bool(p.get("reversed", False)))
                  for p in d["pieces"]]
        return cls(name=d.get("name", "Track"), pieces=pieces, created=d.get("created", time.time()),
                   needs_review=d.get("needs_review", False))


def _corner(p: Piece) -> tuple[int, int]:
    """Which corner of its square a curve bends around: entry side + exit side."""
    hin, hout = HEADINGS[p.heading_in], HEADINGS[p.heading_out]
    return (-hin[0] + hout[0], -hin[1] + hout[1])


def _cyclic_equal(a: list[int], b: list[int]) -> bool:
    n = len(a)
    return any(all(a[i] == b[(i + s) % n] for i in range(n)) for s in range(n))


def merge_start(segs: list[Seg]) -> list[Seg]:
    """The start piece reports two halves (34 then 33, or 33 then 34 when driven backwards)."""
    out: list[Seg] = []
    for s in segs:
        if out and s[1] == "start" and out[-1][1] == "start":
            if s[0] == 34:                     # keep the 34 half's orientation
                out[-1] = (34, "start", None, s[3])
            continue
        out.append((34, "start", None, s[3]) if s[1] == "start" else s)
    return out


def build_track(name: str, segs: list) -> Track:
    """segs: one lap in driving order, as Seg tuples or legacy (id, turn) pairs."""
    norm: list[Seg] = []
    for s in segs:
        if len(s) == 2:
            pid, turn = s
            s = (pid, kind_of(pid), turn, False)
        norm.append(s)
    merged = merge_start(norm)
    if len(merged) > 1 and merged[0][1] == "start" and merged[-1][1] == "start":
        merged.pop()
    i = next((i for i, s in enumerate(merged) if s[1] == "start"), 0)
    merged = merged[i:] + merged[:i]
    pieces = [Piece(id=pid, kind=kind, turn=turn if kind == "turn" else None, reversed=rev)
              for pid, kind, turn, rev in merged]
    track = Track(name=name, pieces=pieces)
    if not track.solve_turns():
        track.needs_review = True
    return track


# ---------------------------------------------------------------- scanning
def segment_from(last_id: Optional[int], last_rev: bool, t: P.Transition) -> Seg:
    """The piece just left. Known id: its type + reverse flag. Unknown: guess from the wheels."""
    if last_id and last_id in KIND_BY_ID:
        kind = kind_of(last_id)
        return (last_id, kind, ("R" if last_rev else "L") if kind == "turn" else None, last_rev)
    left, right = t.left_wheel_cm or 0, t.right_wheel_cm or 0
    if abs(left - right) < 4:
        start = 32 < left < 36 and 32 < right < 36       # the start piece is shorter
        return (34 if start else 0, "start" if start else "straight", None, False)
    return (0, "turn", "R" if left > right else "L", False)


class TrackScanner:
    """Builds the lap from one car. Needs `laps_wanted` matching laps (up to 5 tries)."""

    def __init__(self, laps: int = 2):
        self.laps_wanted = laps
        self.segs: list[Seg] = []            # since the last completed lap
        self.started = False
        self.laps: list[list[Seg]] = []
        self.done = False
        self._last_id: Optional[int] = None
        self._last_rev = False
        self._stub: Optional[tuple[int, bool]] = None
        self._t_transition = 0.0
        self.raw: list[tuple[float, str]] = []      # (t, hex) of every message, saved for debugging

    def on_position(self, p: P.Position) -> None:
        self._last_id, self._last_rev = p.piece, p.reversed
        # for the live view, skip the stale "still the old piece" report right after a transition
        prev = self.segs[-1][0] if self.segs else None
        if not (p.piece == prev and time.monotonic() - self._t_transition < 0.5):
            self._stub = (p.piece, p.reversed)

    def on_transition(self, t: P.Transition) -> None:
        if self.started:                     # the piece the car started on was only partly driven
            self.segs.append(segment_from(self._last_id, self._last_rev, t))
            self._check_lap()
        self.started = True
        self._last_id = None
        self._stub = None
        self._t_transition = time.monotonic()

    @property
    def waiting_for_piece(self) -> bool:
        """Just left a piece and the new one has not reported yet."""
        return self.started and self._stub is None

    def _merged(self) -> list[Seg]:
        return merge_start(self.segs)

    def _check_lap(self) -> None:
        merged = self._merged()
        starts = [i for i, s in enumerate(merged) if s[1] == "start"]
        if len(starts) >= 2:
            a, b = starts[0], starts[1]
            lap = merged[a:b]
            if len(lap) >= 4:
                self.laps.append(lap)
                # keep the closing start piece's raw halves as the beginning of the next lap
                keep = merged[b:]
                self.segs = [(34, "start", None, keep[0][3])] + keep[1:]
                self._decide()
        elif not starts and len(merged) > 40:
            cyc = _find_cycle(merged)
            if cyc:
                self.laps.append(cyc)
                self.segs = []
                self._decide()

    def _decide(self) -> None:
        sigs = Counter(_lap_sig(l) for l in self.laps)
        top, count = sigs.most_common(1)[0]
        if count >= self.laps_wanted or len(self.laps) >= 5:
            self.done = True

    def best_lap(self) -> Optional[list[Seg]]:
        if not self.laps:
            return None
        sigs = Counter(_lap_sig(l) for l in self.laps)
        ranked = sorted(self.laps, key=lambda l: (-sigs[_lap_sig(l)], -self.laps.index(l)))
        for lap in ranked:                    # prefer the most repeated lap that builds cleanly
            if build_track("x", lap).valid():
                return lap
        return ranked[0]

    def preview(self) -> tuple[Optional[Track], int]:
        """Live view while scanning: (track so far, index of the piece the car is on)."""
        current = self._merged()
        if self.laps:
            if getattr(self, "_preview_laps", None) != len(self.laps):
                self._preview_laps = len(self.laps)
                self._preview_track = build_track("Scanning…", self.best_lap())
            t = self._preview_track
            return t, len(current) % max(1, len(t.pieces))
        if not current:
            return None, 0
        pieces = [Piece(id=pid, kind=kind, turn=turn if kind == "turn" else None, reversed=rev)
                  for pid, kind, turn, rev in current]
        # the piece the car is on right now: its id + reverse flag already say what it is
        # (the first report after entering can be stale; it corrects itself on the next report)
        if self._stub:
            sid, srev = self._stub
            kind = kind_of(sid)
            pieces.append(Piece(id=sid, kind=kind, turn=("R" if srev else "L") if kind == "turn" else None,
                                reversed=srev))
            if pieces[-1].kind == "start" and len(pieces) > 1 and pieces[-2].kind == "start":
                pieces.pop()
        return Track(name="Scanning…", pieces=pieces), len(pieces) - 1

    def result(self, name: str) -> Optional[Track]:
        lap = self.best_lap()
        return build_track(name, lap) if lap else None


def _lap_sig(lap: list[Seg]) -> str:
    return "|".join(f"{k}{t or ''}" for _, k, t, _ in lap)


def _find_cycle(segs: list[Seg]) -> Optional[list[Seg]]:
    for n in range(4, len(segs) // 2 + 1):
        if [_lap_sig([s]) for s in segs[-n:]] == [_lap_sig([s]) for s in segs[-2 * n:-n]]:
            return segs[-n:]
    return None


# ---------------------------------------------------------------- localisation
def _expected(p: Piece, d: int) -> tuple[int, Optional[bool]]:
    """What a car driving direction d reports on piece p: (id, reverse flag or None if unknown)."""
    rev = p.reversed if d == 1 else (not p.reversed)
    return p.id, rev


class Localizer:
    """Tracks one car on a known track.

    Every position report is checked against the piece we think the car is on. Pieces are
    identified by id AND the car's reverse flag (which tells e.g. a left 17 from a right 17 and
    which way round the car is going). Lost cars are re-found by scoring the recent pieces
    against every place on the track, both directions.
    """

    def __init__(self):
        self.track: Optional[Track] = None
        self.recent: list[tuple[int, Optional[bool]]] = []   # finished pieces (id, rev), oldest first
        self.index: Optional[int] = None       # piece the car is on now
        self.direction = 1
        self.confidence = 0                     # pieces confirmed in a row
        self.piece_t0 = time.monotonic()
        self.laps = 0
        self.lap_started: Optional[float] = None
        self.last_lap_s: Optional[float] = None
        self.best_lap_s: Optional[float] = None
        self.lap_times: list[float] = []
        self.pieces_since_lap = 0
        self.last_start_seen = 0.0
        self.on_lap = None                     # callback(lap_seconds)
        self._last_id: Optional[int] = None
        self._last_raw_id: Optional[int] = None
        self._last_rev: Optional[bool] = None
        self.stats = {"confirmed": 0, "corrected": 0, "lost": 0}
        # distance along the current piece, integrated from the speed estimate every tick
        self.dist = 0.0
        self._tracking = False
        self.factor = 1.0                       # learned: real distance / integrated distance
        self.learned: dict[tuple[int, int], float] = {}   # (piece index, lane bucket) -> mm
        self._in_start = False                  # between the two halves of the start piece
        self._start_wheels = 0.0

    def set_track(self, track: Optional[Track]) -> None:
        self.track = track
        self.index = None
        self.confidence = 0
        self.relocalize()

    def reset_race(self) -> None:
        self.laps = 0
        self.lap_started = None
        self.last_lap_s = None
        self.best_lap_s = None
        self.lap_times = []
        self.pieces_since_lap = 0

    def _fits(self, i: int, d: int, obs: tuple) -> int:
        """+2 id and flag match, +1 id match (flag unknown), -3 mismatch.
        Pieces the car did not name are judged from the wheels: +1 same shape, -2 different."""
        oid, orev = obs[0], obs[1]
        p = self.track.pieces[i % len(self.track.pieces)]
        if not oid:
            shape = obs[2] if len(obs) > 2 else None
            if not shape:
                return 0
            turn = p.turn if d == 1 or not p.turn else ("L" if p.turn == "R" else "R")
            mine = f"turn{turn}" if p.kind == "turn" else "straight"
            return 1 if shape == mine else -2
        eid, erev = _expected(p, d)
        if oid != eid:
            return -3
        if orev is None or self.track.pieces[i % len(self.track.pieces)].kind == "start":
            return 1
        return 2 if orev == erev else -2

    def relocalize(self) -> None:
        """Score the recent pieces against every (end piece, direction). Accept a clear winner."""
        if not self.track or len([r for r in self.recent if r[0] or len(r) > 2]) < 3:
            return
        n = len(self.track.pieces)
        window = self.recent[-min(len(self.recent), n, 10):]
        scores = []
        for d in (1, -1):
            for end in range(n):
                s = sum(self._fits(end - d * k, d, obs) for k, obs in enumerate(reversed(window)))
                scores.append((s, end, d))
        scores.sort(reverse=True)
        best = scores[0]
        runner = scores[1][0] if len(scores) > 1 else -99
        if best[0] >= 4 and best[0] - runner >= 2:
            _, end, d = best
            self.index, self.direction = (end + d) % n, d
            self.confidence = 1

    def advance(self, dt: float, speed_mm_s: float) -> None:
        self.dist += max(0.0, speed_mm_s) * dt
        self._tracking = True

    def _new_piece(self) -> None:
        self.piece_t0 = time.monotonic()
        self.dist = 0.0

    def on_position(self, piece_id: int, reversed_flag: Optional[bool] = None, location: Optional[int] = None) -> None:
        pid = norm_id(piece_id)
        self._last_id, self._last_rev, self._last_raw_id = pid, reversed_flag, piece_id
        if not self.track or self.index is None:
            return
        n = len(self.track.pieces)
        here = self.track.pieces[self.index]
        if pid == here.id:
            return
        prev = self.track.pieces[(self.index - self.direction) % n]
        if pid == prev.id and time.monotonic() - self.piece_t0 < 0.4:
            return                              # stale report right after a transition
        nxt = self.track.pieces[(self.index + self.direction) % n]
        if pid == nxt.id:                       # we missed a transition: catch up
            self.index = (self.index + self.direction) % n
            self._new_piece()
            self.stats["corrected"] += 1
            return
        nxt2 = self.track.pieces[(self.index + 2 * self.direction) % n]
        if pid == nxt2.id:
            self.index = (self.index + 2 * self.direction) % n
            self._new_piece()
            self.stats["corrected"] += 1
            return
        # somewhere unexpected: forget the position and re-find it from the recent pieces
        self.stats["lost"] += 1
        self.index = None
        self.confidence = 0
        self.relocalize()

    def _bucket(self, lane_mm: float) -> int:
        return max(-3, min(3, round(lane_mm / 22.5)))

    def _learn(self, wheels_mm: float, lane_mm: float) -> None:
        """Real distance driven on the piece just finished (wheel odometry) -> calibrate."""
        if self.index is None or not self.track or wheels_mm <= 50:
            return
        if self._tracking and self.dist > 50:
            sample = max(0.6, min(1.6, wheels_mm / self.dist))
            self.factor += 0.2 * (sample - self.factor)
        key = (self.index, self._bucket(lane_mm))
        old = self.learned.get(key)
        self.learned[key] = wheels_mm if old is None else old + 0.5 * (wheels_mm - old)

    def on_transition(self, left_cm: Optional[int] = None, right_cm: Optional[int] = None,
                      lane_mm: float = 0.0) -> None:
        obs = (self._last_id or 0, self._last_rev)
        self._last_id = self._last_rev = self._last_raw_id = None
        wheels = ((left_cm or 0) + (right_cm or 0)) * 5.0 if left_cm is not None and right_cm is not None else 0.0
        if not 150 <= wheels <= 800:
            wheels = 0.0                        # glitchy odometry (e.g. while airborne / sliding)
        if not obs[0] and wheels:
            # unnamed piece: classify it from the wheels (inner wheel travels less on a curve)
            diff = (left_cm or 0) - (right_cm or 0)
            if abs(diff) * 10 >= 0.15 * wheels:   # clear left/right difference (>15%)
                obs = (0, obs[1], "turnR" if diff > 0 else "turnL")
            elif abs(diff) * 10 > 0.06 * wheels:
                pass                            # ambiguous: leave it unclassified
            elif wheels < 450:
                obs = (34, obs[1])              # 22 / 34 cm straight: only the start piece's halves
            else:
                obs = (0, obs[1], "straight")
        on_start = bool(self.index is not None and self.track and self.track.pieces[self.index].kind == "start")
        if obs[0] == 34 or (on_start and obs[0] == 0):
            # the start piece reports two halves (34 then 33, or 33 then 34 driven backwards):
            # only the second half finishes the piece. Halves are short (34 / 22 cm), so the car
            # often sends no position report on one: an unreported half on the start piece counts.
            obs = (34, obs[1])
            first_half = not self._in_start
            if first_half:
                self._in_start = True
                self._start_wheels = wheels
                self.recent = (self.recent + [obs])[-16:]
                self._crossed_line()
                return
            self._in_start = False
            wheels += self._start_wheels
            if self.index is not None and self.track and self.track.pieces[self.index].kind == "start":
                self._learn(wheels, lane_mm)
            self._new_piece()
            self._advance(obs)
            return
        self._in_start = False
        if self.index is not None and self.track and self._fits(self.index, self.direction, obs) >= 0:
            self._learn(wheels, lane_mm)
        self._new_piece()
        self.recent = (self.recent + [obs])[-16:]
        self._advance(obs)

    def _advance(self, obs: tuple[int, Optional[bool]]) -> None:
        """The car has just finished the piece `obs`: move to the next one (or re-find the car)."""
        self.pieces_since_lap += 1
        if not self.track:
            return
        n = len(self.track.pieces)
        named = bool(obs[0])
        # only a piece the car NAMED can prove us wrong; wheel-only guesses are too noisy for that
        if self.index is not None and (not named or self._fits(self.index, self.direction, obs) >= 0):
            if obs[0]:
                self.confidence += 1
                self.stats["confirmed"] += 1
            self.index = (self.index + self.direction) % n
        else:
            if self.index is not None:
                self.stats["lost"] += 1
            self.index = None
            self.confidence = 0
            self.relocalize()

    def _crossed_line(self) -> None:
        now = time.monotonic()
        min_pieces = max(3, len(self.track.pieces) // 2) if self.track else 3
        if self.lap_started is None:
            self.lap_started = now
            self.pieces_since_lap = 0
        elif self.pieces_since_lap >= min_pieces and now - self.last_start_seen > 2.0:
            lap = now - self.lap_started
            self.laps += 1
            self.last_lap_s = lap
            self.lap_times.append(lap)
            self.best_lap_s = lap if self.best_lap_s is None else min(self.best_lap_s, lap)
            self.lap_started = now
            self.pieces_since_lap = 0
            if self.on_lap:
                self.on_lap(lap)
        self.last_start_seen = now

    def piece_length(self, lane_mm: float = 0.0) -> float:
        """Length of the current piece along the car's lane (inside of a curve is shorter)."""
        learned = self.learned.get((self.index, self._bucket(lane_mm)))
        if learned:
            return learned                      # measured by the wheels on an earlier lap
        p = self.track.pieces[self.index]
        if p.kind != "turn" or p.turn not in ("L", "R"):
            return p.length_mm
        turn = p.turn if self.direction == 1 else ("L" if p.turn == "R" else "R")
        r = CELL_MM / 2 - lane_mm if turn == "R" else CELL_MM / 2 + lane_mm   # lane < 0 = left
        return math.pi / 2 * max(120.0, r)

    def fraction(self, speed_mm_s: float, lane_mm: float = 0.0) -> float:
        if not self.track or self.index is None:
            return 0.0
        L = self.piece_length(lane_mm)
        if self._tracking:                      # integrated speed estimate x learned calibration
            return min(0.99, self.dist * self.factor / L)
        return min(0.99, (time.monotonic() - self.piece_t0) * max(0.0, speed_mm_s) / L)

    def progress(self, speed_mm_s: float, lane_mm: float = 0.0) -> float:
        """Laps completed plus fraction of the current lap, for ranking."""
        if self.track and self.index is not None:
            n = len(self.track.pieces)
            pos = self.index if self.direction == 1 else (n - self.index) % n
            return self.laps + (pos + self.fraction(speed_mm_s, lane_mm)) / n
        return self.laps + min(0.99, self.pieces_since_lap / 20)

    def lookahead(self, count: int = 3) -> list[Piece]:
        if not self.track or self.index is None:
            return []
        n = len(self.track.pieces)
        return [self.track.pieces[(self.index + self.direction * k) % n] for k in range(1, count + 1)]

    @property
    def current_piece(self) -> Optional[Piece]:
        if not self.track or self.index is None:
            return None
        return self.track.pieces[self.index]


def sample_track(name: str = "Starter Oval") -> Track:
    """A simple oval: start, straights, two right turns, straights, two right turns."""
    raw = [(34, None), (57, None), (39, None), (17, "R"), (18, "R"), (40, None), (48, None),
           (36, None), (20, "R"), (23, "R")]
    return build_track(name, raw)
