"""Drive a track scan through the running app (so it shows live in the window) and report.

    python tools/live_scan.py PORT [MODEL] [--find-only]
"""
import asyncio
import json
import sys
import time

import aiohttp


async def main():
    port = int(sys.argv[1])
    model = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "Thermo"
    find_only = "--find-only" in sys.argv
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
            latest = {"catalog": None, "state": None}

            async def reader():
                async for msg in ws:
                    m = json.loads(msg.data)
                    if m["type"] == "catalog":
                        latest["catalog"] = m
                    elif m["type"] == "state":
                        latest["state"] = m
                    elif m["type"] == "notice":
                        print(f"  [app] {m['text']}", flush=True)
            task = asyncio.create_task(reader())
            send = lambda **o: ws.send_str(json.dumps(o))

            await send(op="discover", seconds=8)
            await asyncio.sleep(9.5)
            cat = latest["catalog"]
            ads = cat["adverts"]
            print(f"nearby: {len(ads)}")
            for a in sorted(ads, key=lambda a: a["model"]):
                g = cat["garage"].get(a["address"], {})
                print(f"  {a['tag']}  {a['model']:<12} {g.get('nickname', ''):<16} rssi {a['rssi']}{'  charging' if a['on_charger'] else ''}")
            connected = [c for c in (latest["state"] or {}).get("cars", [])]
            print(f"already connected: {[c['nickname'] for c in connected]}")
            target = next((c for c in connected if c["model"] == model), None) or \
                next((a for a in ads if a["model"] == model), None)
            if not target:
                print(f"NO {model} FOUND")
                task.cancel()
                return
            addr = target["address"]
            print(f"target: {model} {addr[-5:]}")
            if find_only:
                task.cancel()
                return
            if not any(c["address"] == addr for c in connected):
                await send(op="connect", car=addr)
                for _ in range(40):
                    await asyncio.sleep(0.5)
                    if any(c["address"] == addr and c["connected"] for c in latest["state"]["cars"]):
                        break
            car = next((c for c in latest["state"]["cars"] if c["address"] == addr), None)
            if not car or not car["connected"]:
                print("could not connect")
                task.cancel()
                return
            await asyncio.sleep(1.5)
            car = next(c for c in latest["state"]["cars"] if c["address"] == addr)
            print(f"connected: battery {car['battery']}%  firmware {car['firmware']}  on_charger {car['on_charger']}")
            await send(op="select_track", key=None)
            await send(op="scan_track", car=addr)
            t0 = time.monotonic()
            last = None
            await asyncio.sleep(1)
            while time.monotonic() - t0 < 150:
                await asyncio.sleep(1)
                s = latest["state"]
                scan = s.get("scan")
                c = next((c for c in s["cars"] if c["address"] == addr), {})
                line = (f"{time.monotonic() - t0:5.0f}s speed {c.get('speed')} piece_id {c.get('piece_id')} "
                        + (f"pieces {scan['pieces']} laps {scan['laps']}" if scan else "scan finished"))
                if line != last:
                    print(line, flush=True)
                    last = line
                if not scan:
                    break
            s = latest["state"]
            if s.get("scan"):
                print("TIMEOUT: cancelling")
                await send(op="cancel_scan")
                await asyncio.sleep(1)
            await asyncio.sleep(1.5)
            key = latest["state"]["track_key"]
            t = latest["catalog"]["tracks"].get(key) if key else None
            if t:
                print(f"RESULT: {t['name']}  pieces {len(t['pieces'])}  valid {t['valid']}  start_reversed {t['start_reversed']}")
                print("  " + " ".join(f"{p['id']}{p['turn'] or ''}{'r' if p['reversed'] else ''}" for p in t["pieces"]))
            else:
                print("RESULT: no track selected")
            task.cancel()


asyncio.run(main())
