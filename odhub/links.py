"""Transport layer: real Bluetooth LE cars (bleak) and simulated cars.

Both expose the same interface so the rest of the app cannot tell them apart:
    await link.connect(); await link.send(bytes); await link.close()
    link.on_message = callback(bytes); link.on_disconnect = callback()
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import protocol as P
from .track import Track, CELL_MM

log = logging.getLogger("odhub.links")


@dataclass
class Advert:
    address: str
    name: str
    model_id: int
    rssi: int
    on_charger: bool = False
    simulated: bool = False


class Link:
    on_message: Optional[Callable[[bytes], None]] = None
    on_disconnect: Optional[Callable[[], None]] = None
    connected = False

    async def connect(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------- real cars
async def scan_ble(seconds: float = 4.0) -> list[Advert]:
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=seconds, return_adv=True, service_uuids=[P.SERVICE_UUID])
    out = []
    for dev, adv in found.values():
        raw = adv.local_name or dev.name or ""
        # Anki names are: state byte, version bytes, padding, then e.g. "Drive". Short names have no state.
        state = ord(raw[0]) if len(raw) > 6 else 0
        name = raw[8:].strip("\x00 ") if len(raw) > 8 else raw.strip("\x00 ")
        out.append(Advert(address=dev.address, name=name or "Drive",
                          model_id=P.model_from_manufacturer(adv.manufacturer_data or {}),
                          rssi=adv.rssi, on_charger=False))   # advert flag is cached by Windows: use check_charge()
    return out


class BleLink(Link):
    def __init__(self, address: str):
        self.address = address
        self.client = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        from bleak import BleakClient
        self.client = BleakClient(self.address, disconnected_callback=lambda _c: self._lost(), timeout=15.0)
        await self.client.connect()
        await self.client.start_notify(P.READ_UUID, lambda _h, data: self.on_message and self.on_message(bytes(data)))
        self.connected = True

    def _lost(self) -> None:
        was = self.connected
        self.connected = False
        if was and self.on_disconnect:
            self.on_disconnect()

    async def send(self, data: bytes) -> None:
        if not self.connected or not self.client:
            return
        async with self._lock:
            try:
                await self.client.write_gatt_char(P.WRITE_UUID, data, response=False)
            except Exception as e:  # noqa: BLE001 - bluetooth stacks raise many types
                log.warning("write to %s failed: %s", self.address, e)

    async def close(self) -> None:
        if self.client and self.connected:
            try:
                await self.send(P.disconnect())
                await asyncio.sleep(0.1)
                self.connected = False
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.connected = False


# ---------------------------------------------------------------- simulator
class SimWorld:
    """Virtual cars driving a virtual track, emitting the same bytes real cars do."""

    def __init__(self, track: Track, count: int = 4):
        self.track = track
        self.cars: list[SimCar] = []
        for i in range(count):
            self.cars.append(SimCar(self, f"SIM:{i + 1:02X}", (8, 9, 16, 4, 1, 2, 11, 12)[i % 8], i))
        self._task: Optional[asyncio.Task] = None

    def adverts(self) -> list[Advert]:
        return [Advert(c.address, P.model_name(c.model_id), c.model_id, -40, simulated=True)
                for c in self.cars]

    def get(self, address: str) -> "SimCar":
        return next(c for c in self.cars if c.address == address)

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            dt, last = now - last, now
            for c in self.cars:
                c.step(dt)


class SimCar(Link):
    def __init__(self, world: SimWorld, address: str, model_id: int, slot: int):
        self.world, self.address, self.model_id = world, address, model_id
        n = len(world.track.pieces)
        self.index = (n - 1 - slot // 2) % n           # line up just behind the start
        self.dist = 0.0
        self.offset = P.LANES[slot % 4]
        self.target_offset = self.offset
        self.speed = 0.0
        self.target = 0.0
        self.accel = 1000.0
        self.battery_mv = 3900 - slot * 40
        self.sdk = False
        self._loc_t = 0.0

    async def connect(self) -> None:
        await asyncio.sleep(0.2)
        self.connected = True
        self.world.start()
        self._emit(bytes([5, P.STATUS, 1, 0, 0, 0]))

    async def close(self) -> None:
        self.connected = False
        self.target = self.speed = 0

    async def send(self, data: bytes) -> None:
        if not self.connected or len(data) < 2:
            return
        import struct
        mid, body = data[1], data[2:]
        if mid == P.SDK_MODE:
            self.sdk = bool(body[0])
        elif mid == P.SET_SPEED:
            self.target, self.accel = struct.unpack_from("<hh", body)
            self.accel = max(100, self.accel)
        elif mid == P.CHANGE_LANE:
            self.target_offset = struct.unpack_from("<HHf", body)[2]
        elif mid == P.SET_OFFSET:
            pass
        elif mid == P.BATTERY_REQ:
            self._emit(bytes([3, P.BATTERY_RSP]) + int(self.battery_mv).to_bytes(2, "little"))
        elif mid == P.VERSION_REQ:
            self._emit(bytes([3, P.VERSION_RSP]) + (0x2E6A).to_bytes(2, "little"))
        elif mid == P.PING:
            self._emit(bytes([1, P.PING_RSP]))
        elif mid == P.TURN:
            self.target = 0

    def _emit(self, data: bytes) -> None:
        if self.connected and self.on_message:
            self.on_message(data)

    def step(self, dt: float) -> None:
        if not self.connected:
            return
        diff = self.target - self.speed
        self.speed += max(-self.accel * dt * 1.5, min(self.accel * dt, diff))
        piece = self.world.track.pieces[self.index]
        if piece.kind == "turn" and self.speed > 1250 and random.random() < 0.002:
            self.speed = self.target = 0          # flew off the curve
            self._emit(bytes([1, P.DELOCALIZED]))
            return
        self.offset += max(-300 * dt, min(300 * dt, self.target_offset - self.offset))
        self.dist += self.speed * dt
        self.battery_mv -= dt * self.speed * 0.00002
        if self.dist >= piece.length_mm:
            self.dist -= piece.length_mm
            n = len(self.world.track.pieces)
            prev = piece
            self.index = (self.index + 1) % n
            wheel = round(prev.length_mm / 10)
            left = wheel + (3 if prev.turn == "R" else -3 if prev.turn == "L" else 0)
            right = wheel + (3 if prev.turn == "L" else -3 if prev.turn == "R" else 0)
            self._emit(P.encode_transition(P.Transition(self.index, (self.index - 1) % n, self.offset, left, right)))
            new = self.world.track.pieces[self.index]
            if new.id == 34:
                self._emit_pos(34)
                self._emit_pos(33)
            else:
                self._emit_pos(new.id)
        self._loc_t += dt
        if self._loc_t > 0.25 and self.speed > 0:
            self._loc_t = 0
            self._emit_pos(self.world.track.pieces[self.index].id)

    def _emit_pos(self, piece_id: int) -> None:
        piece = self.world.track.pieces[self.index]
        flags = 0x40 if (piece.turn == "R" or piece.reversed) else 0     # real cars: reverse flag = right turn
        self._emit(P.encode_position(P.Position(random.randint(0, 40), piece_id, self.offset, int(self.speed), flags)))
