"""Replay a recorded car log through the app's Car + Localizer and measure position accuracy.

    python tools/replay_loc.py [log.json] [track-key]

Logs: data/scan-logs/*.json (scans) or data/test-logs/raw-*.json (any session; includes what the
app sent, prefixed '>', so commanded speeds are replayed too).

Metrics
  * piece agreement: at every position report (except the first 0.4 s on a piece, where cars
    still report the previous id) is the localiser on the piece the car says it is on?
  * in-piece estimate at piece end: the fraction the app shows just before each transition.
    1.00 is perfect; below = car drawn behind reality, above = drawn ahead (then held at 0.99).
"""
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odhub.car as C
import odhub.track as T
from odhub import protocol as P
from odhub.library import Library
from odhub.links import Link

ROOT = Path(__file__).resolve().parent.parent
TICK = 0.05


class NullLink(Link):
    async def send(self, data: bytes) -> None: ...


def replay(log: Path, key: str, quiet: bool = False):
    track = Library(ROOT / "data").tracks[key]
    d = json.loads(log.read_text())
    clock = {"t": 0.0}
    T.time.monotonic = C.time.monotonic = lambda: clock["t"]
    car = C.Car("replay", NullLink(), 0, "replay", "#ffffff")
    car.connected = True
    car.loc.set_track(track)
    msgs = d["messages"]
    t = msgs[0][0]
    total = agree = unknown = 0
    end_fracs, by_kind = [], {}
    first_fix = None
    import asyncio
    loop = asyncio.new_event_loop()
    for ts, hx in msgs:
        while t + TICK <= ts:                    # engine ticks between messages
            t += TICK
            clock["t"] = t
            car.update_estimate(TICK)
        clock["t"] = ts
        if hx.startswith(">"):
            loop.run_until_complete(car.link.send(bytes.fromhex(hx[1:])))
            continue
        b = bytes.fromhex(hx)
        mid, val = P.parse(b)
        if mid == P.TRANSITION and car.loc.index is not None and car.speed_est > 150 and not car.loc._in_start:
            f = car.loc.dist * car.loc.factor / car.loc.piece_length(car.offset)
            end_fracs.append(f)
            by_kind.setdefault(track.pieces[car.loc.index].kind, []).append(f)
        car._on_message(b)
        if mid == P.POSITION:
            if ts - car.loc.piece_t0 < 0.4:
                continue
            total += 1
            if car.loc.index is None:
                unknown += 1
            else:
                first_fix = first_fix or ts
                agree += T.norm_id(val.piece) == track.pieces[car.loc.index].id
    if not quiet:
        print(f"log {log.name}  track {track.name} ({len(track.pieces)} pieces)  {msgs[-1][0] - msgs[0][0]:.0f}s")
        print(f"piece agreement: {agree}/{total - unknown} = {100 * agree / max(1, total - unknown):.1f}%  "
              f"(unknown {unknown})  first fix after {(first_fix or msgs[0][0]) - msgs[0][0]:.1f}s")
        print(f"localiser stats {car.loc.stats}  speed factor {car.loc.factor:.2f}  learned lengths {len(car.loc.learned)}")
        if end_fracs:
            e = sorted(end_fracs)
            late = e[len(e) // 2:]
            print(f"in-piece estimate at piece end: median {st.median(e):.2f}  10%..90% "
                  f"{e[len(e) // 10]:.2f}..{e[9 * len(e) // 10]:.2f}  n={len(e)}  "
                  f"| 2nd half of session median {st.median(end_fracs[len(end_fracs) // 2:]):.2f}")
            print("  per kind:", ", ".join(f"{k} {st.median(v):.2f} (n={len(v)})" for k, v in by_kind.items()))
    return end_fracs


if __name__ == "__main__":
    logs = sorted((ROOT / "data" / "test-logs").glob("raw-*.json")) or sorted((ROOT / "data" / "scan-logs").glob("*.json"))
    log = Path(sys.argv[1]) if len(sys.argv) > 1 else logs[-1]
    replay(log, sys.argv[2] if len(sys.argv) > 2 else "track-1")
