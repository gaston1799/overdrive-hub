"""Live hardware test through the running app: scan with one car, then let an AI drive it and
measure how steady the app's idea of the car's position is.

    python tools/live_test.py PORT [MODEL] [--drive-seconds 25] [--no-scan] [--racer steady:easy]
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

import aiohttp

OUT = Path(__file__).resolve().parent.parent / "data" / "test-logs"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", type=int)
    ap.add_argument("model", nargs="?", default="Thermo")
    ap.add_argument("--drive-seconds", type=float, default=25)
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--racer", default="steady:easy")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    rec = []
    async with aiohttp.ClientSession() as http, http.ws_connect(f"http://127.0.0.1:{a.port}/ws") as ws:
        latest = {"catalog": None, "state": None}

        async def reader():
            async for msg in ws:
                m = json.loads(msg.data)
                if m["type"] == "catalog":
                    latest["catalog"] = m
                elif m["type"] == "state":
                    latest["state"] = m
                    rec.append({"t": time.monotonic(), "cars": m["cars"], "scan": m.get("scan") and
                                {k: v for k, v in m["scan"].items() if k != "track"}, "track_key": m["track_key"]})
                elif m["type"] == "notice":
                    print(f"  [app] {m['text']}", flush=True)
        task = asyncio.create_task(reader())
        send = lambda **o: ws.send_str(json.dumps(o))
        await asyncio.sleep(1)
        await send(op="discover", seconds=6)
        await asyncio.sleep(7)
        ad = next((x for x in latest["catalog"]["adverts"] if x["model"] == a.model), None)
        if not ad:
            print("car not found"); task.cancel(); return
        addr = ad["address"]
        await send(op="connect", car=addr)
        for _ in range(40):
            await asyncio.sleep(0.5)
            c = next((c for c in latest["state"]["cars"] if c["address"] == addr), None)
            if c and c["connected"]:
                break
        print(f"connected {a.model} {addr[-5:]}  battery {c['battery']}%")
        try:
            if not a.no_scan:
                await send(op="select_track", key=None)
                await send(op="scan_track", car=addr)
                t0 = time.monotonic()
                await asyncio.sleep(1)
                while latest["state"].get("scan") and time.monotonic() - t0 < 150:
                    await asyncio.sleep(0.5)
                print(f"scan done in {time.monotonic() - t0:.0f}s, track {latest['state']['track_key']}")
                await asyncio.sleep(2)
            drive_from = len(rec)
            await send(op="set_controller", car=addr, spec=f"ai:{a.racer}")
            await asyncio.sleep(a.drive_seconds)
        finally:
            try:
                await send(op="set_controller", car=addr, spec="none")
                await send(op="stop_all")
                await asyncio.sleep(1)
                await send(op="dump_log", car=addr)
                await asyncio.sleep(0.5)
                await send(op="disconnect", car=addr)
                await asyncio.sleep(1)
            except Exception as e:  # noqa: BLE001
                print("app went away:", e)
        c = next((c for c in latest["state"]["cars"] if c["address"] == addr), {})
        print(f"loc stats {c.get('loc_stats')}  laps {c.get('laps')}  best {c.get('best_lap')}")
        track = latest["catalog"]["tracks"].get(latest["state"]["track_key"])
        task.cancel()
    name = OUT / f"live-{time.strftime('%H%M%S')}.json"
    name.write_text(json.dumps({"addr": addr, "track": track, "drive_from": drive_from, "rec": rec}))
    analyse(name)


def analyse(path):
    d = json.loads(Path(path).read_text())
    track, addr = d["track"], d["addr"]
    if not track:
        print("no track"); return
    import math
    pieces = track["pieces"]
    L = [math.pi / 2 * 280 if p["kind"] == "turn" else 560 * p.get("cells", 1) for p in pieces]
    cum = [sum(L[:i]) for i in range(len(L))]
    total = sum(L)
    rows = []
    for r in d["rec"][d["drive_from"]:]:
        c = next((c for c in r["cars"] if c["address"] == addr), None)
        if c:
            rows.append((r["t"], c))
    known = [(t, c) for t, c in rows if c["index"] is not None]
    agree = sum(1 for t, c in known if c["piece_id"] in (None, pieces[c["index"]]["id"]) or
                (c["piece_id"] in (33, 34) and pieces[c["index"]]["kind"] == "start"))
    print(f"drive samples {len(rows)}  localised {len(known)} ({100 * len(known) / max(1, len(rows)):.0f}%)  "
          f"piece agrees with car's last report {agree}/{len(known)} ({100 * agree / max(1, len(known)):.0f}%)")
    # smoothness: change in track distance between 20 Hz samples vs. what the speed allows
    jumps = back = 0
    prev = None
    for t, c in known:
        f = c["frac"] if c["direction"] == 1 else 1 - c["frac"]
        s = cum[c["index"]] + L[c["index"]] * f
        if prev:
            dt = t - prev[0]
            ds = ((s - prev[1]) * c["direction"]) % total
            if ds > total / 2:
                ds -= total
            allowed = max(c["speed"], prev[2]) * dt + 60
            if ds < -40:
                back += 1
            elif ds > allowed + 150:
                jumps += 1
        prev = (t, s, c["speed"])
    print(f"position jumps forward {jumps}, backward {back} (of {len(known)} samples)")


if __name__ == "__main__":
    asyncio.run(main())
