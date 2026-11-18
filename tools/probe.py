"""Connect to one real car, read battery + firmware, flash its light, disconnect. The car does not move.

    python tools/probe.py [ADDRESS]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odhub import protocol as P
from odhub.links import BleLink, scan_ble


async def main():
    addr = sys.argv[1] if len(sys.argv) > 1 else None
    if not addr:
        cars = sorted(await scan_ble(5), key=lambda a: -a.rssi)
        if not cars:
            print("no cars found")
            return
        addr = cars[0].address
        print(f"closest car: {addr} ({P.model_name(cars[0].model_id)})")
    link = BleLink(addr)
    got = {}
    link.on_message = lambda d: got.setdefault(P.parse(d)[0], P.parse(d)[1])
    await link.connect()
    print("connected")
    await link.send(P.sdk_mode(True))
    await link.send(P.version_request())
    await link.send(P.battery_request())
    await link.send(P.ping())
    await link.send(P.engine_color(255, 255, 255, P.FX_FLASH, 60))
    await asyncio.sleep(2.0)
    print("firmware:", hex(got[P.VERSION_RSP]) if P.VERSION_RSP in got else "no reply")
    if P.BATTERY_RSP in got:
        print(f"battery: {got[P.BATTERY_RSP]} mV (~{P.battery_percent(got[P.BATTERY_RSP])}%)")
    print("ping:", "ok" if P.PING_RSP in got else "no reply")
    print("status:", got.get(P.STATUS))
    await link.close()
    print("disconnected")


asyncio.run(main())
