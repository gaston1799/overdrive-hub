import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odhub import protocol as P
from odhub.engine import Engine
from odhub.library import Library
from odhub.track import Track, build_track, sample_track


def test_protocol_roundtrip():
    pos = P.Position(5, 36, -23.0, 800, 0)
    mid, val = P.parse(P.encode_position(pos))
    assert mid == P.POSITION and val.piece == 36 and val.speed == 800
    mid, t = P.parse(P.encode_transition(P.Transition(3, 2, 0.0, 47, 41)))
    assert mid == P.TRANSITION and t.turn == "R"
    assert P.set_speed(500)[:2] == bytes([6, P.SET_SPEED])


def test_sample_track_closes():
    t = sample_track()
    assert t.closed and not t.needs_review
    assert t.pieces[0].id == 34


def test_solver_fills_unknown_turns():
    raw = [(34, None), (36, None), (17, None), (18, None), (39, None), (40, None), (20, None), (23, None)]
    t = build_track("x", raw)
    assert t.closed


def test_match_and_same_layout():
    t = sample_track()
    rotated = Track("r", t.pieces[3:] + t.pieces[:3])
    assert t.same_layout(rotated)
    ids = t.ids
    assert t.match([ids[2], ids[3], ids[4], ids[5]]) == (5, 1)
    assert t.match([ids[5], ids[4], ids[3], ids[2]]) == (2, -1)


def _run(coro):
    return asyncio.run(coro)


def test_scan_save_and_recognize(tmp_path):
    async def go():
        lib = Library(tmp_path)
        eng = Engine(lib, sim_cars=2, use_ble=False)
        eng.start()
        await eng.discover(0.1)
        a = eng.sim.cars[0].address
        await eng.connect(a)
        eng.start_track_scan(a)
        t0 = time.monotonic()
        while eng.scan_job and time.monotonic() - t0 < 60:
            await asyncio.sleep(0.1)
        assert eng.track is not None, "scan did not produce a track"
        assert eng.track.closed
        assert eng.track.same_layout(sample_track())
        assert len(lib.tracks) == 1
        # scanning again finds the saved one instead of saving a duplicate
        eng.select_track(None)
        eng.start_track_scan(a)
        t0 = time.monotonic()
        while eng.scan_job and time.monotonic() - t0 < 60:
            await asyncio.sleep(0.1)
        assert len(lib.tracks) == 1
        # fresh engine, no track selected: driving recognises it automatically
        lib.set_setting("last_track", None)
        eng2 = Engine(Library(tmp_path), sim_cars=1, use_ble=False)
        eng2.start()
        await eng2.discover(0.1)
        b = eng2.sim.cars[0].address
        await eng2.connect(b)
        eng2.set_controller(b, "ai:steady:normal")
        t0 = time.monotonic()
        while not eng2.track and time.monotonic() - t0 < 20:
            await asyncio.sleep(0.1)
        assert eng2.track is not None, "track not recognized"
    _run(go())


def test_ai_race_finishes(tmp_path):
    async def go():
        lib = Library(tmp_path)
        key = lib.save_track(sample_track())
        eng = Engine(lib, sim_cars=4, use_ble=False)
        eng.select_track(key)
        eng.start()
        await eng.discover(0.1)
        await eng.connect_all()
        cars = list(eng.cars)
        for a, racer in zip(cars, ["nitro", "steady", "viper", "rookie"]):
            eng.set_controller(a, f"ai:{racer}:hard")
        eng.start_game("race", cars, {"laps": 2, "countdown_s": 1})
        t0 = time.monotonic()
        while eng.game.state != "finished" and time.monotonic() - t0 < 60:
            await asyncio.sleep(0.2)
        assert eng.game.state == "finished", eng.game.to_dict(eng.cars, {})
        assert eng.game.winner in cars
    _run(go())


def test_battle_has_hits(tmp_path):
    async def go():
        lib = Library(tmp_path)
        key = lib.save_track(sample_track())
        eng = Engine(lib, sim_cars=4, use_ble=False)
        eng.select_track(key)
        eng.start()
        await eng.discover(0.1)
        await eng.connect_all()
        cars = list(eng.cars)
        for a in cars:
            eng.set_controller(a, "ai:viper:normal")
        hits = []
        eng.listeners.append(lambda m: m.get("kind") == "hit" and hits.append(m))
        eng.start_game("battle", cars, {"time_limit_s": 15, "countdown_s": 0})
        t0 = time.monotonic()
        while eng.game.state != "finished" and time.monotonic() - t0 < 30:
            await asyncio.sleep(0.2)
        assert eng.game.state == "finished"
        assert hits, "no blaster hits in 15 s of battle"
    _run(go())



def _fig8_segs():
    # crossover, three right turns, crossover again (at right angles), three left turns
    return [(10, "crisscross", None, False), (17, "turn", "R", True), (18, "turn", "R", True), (20, "turn", "R", True),
            (10, "crisscross", None, False), (23, "turn", "L", False), (17, "turn", "L", False), (18, "turn", "L", False)]


def test_figure8_crossover_meets():
    t = build_track("fig8", _fig8_segs())
    assert t.valid()
    cross = [p for p in t.pieces if p.kind == "crisscross"]
    assert len(cross) == 2 and (cross[0].x, cross[0].z) == (cross[1].x, cross[1].z)


def test_solver_repairs_misread_turns():
    segs = _fig8_segs()
    segs[2] = (18, "turn", "L", False)          # two misread directions
    segs[6] = (17, "turn", "R", True)
    t = build_track("fig8", segs)
    assert t.valid()
    cross = [p for p in t.pieces if p.kind == "crisscross"]
    assert (cross[0].x, cross[0].z) == (cross[1].x, cross[1].z)


def _feed(scanner, pieces, laps):
    """Drive the scanner like a real car: positions (sometimes stale) then a transition per piece."""
    from odhub.track import TrackScanner  # noqa: F401
    prev = None
    for _ in range(laps):
        for pid, rev in pieces:
            if prev is not None:                 # first report after a transition is often stale
                scanner.on_position(P.Position(0, prev, 0.0, 400, 0))
            for _ in range(3):
                scanner.on_position(P.Position(0, pid, 0.0, 400, 0x40 if rev else 0))
            scanner.on_transition(P.Transition(0, 0, 0.0, 50, 50))
            prev = pid


def test_scanner_uses_last_id_and_reverse_flag():
    from odhub.track import TrackScanner
    lap = [(34, False), (33, False), (57, False), (36, False), (17, True), (18, True), (39, False),
           (40, False), (48, False), (20, True), (23, True)]
    s = TrackScanner(laps=2)
    _feed(s, lap, 4)
    assert s.done
    t = s.result("x")
    kinds = [p.kind for p in t.pieces]
    assert kinds[0] == "start" and "special" in kinds, kinds        # F&F piece kept, start first
    assert [p.turn for p in t.pieces if p.kind == "turn"] == ["R", "R", "R", "R"]
    assert t.valid() and len(t.pieces) == 10
    assert t.start_reversed is False


def test_live_preview_grows():
    from odhub.track import TrackScanner
    s = TrackScanner()
    _feed(s, [(34, False), (36, False), (17, True), (18, True), (39, False)], 1)
    t, idx = s.preview()
    assert t is not None and idx == len(t.pieces) - 1 and len(t.pieces) >= 4
