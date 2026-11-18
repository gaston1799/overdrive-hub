"""One connected car: live telemetry, throttled commands, lights, identity."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Callable, Optional

from . import protocol as P
from .links import Link
from .track import Localizer, TrackScanner

log = logging.getLogger("odhub.car")


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


class Car:
    def __init__(self, address: str, link: Link, model_id: int, nickname: str, color: str,
                 simulated: bool = False):
        self.address = address
        self.link = link
        self.model_id = model_id
        self.model = P.model_name(model_id)
        self.nickname = nickname
        self.color = color
        self.simulated = simulated

        self.connected = False
        self.battery_mv: Optional[int] = None
        self.on_charger = False
        self.battery_full = False
        self.battery_low = False
        self.on_track = True
        self.version: Optional[int] = None
        self.v4 = False
        self.speed = 0                 # last reported mm/s
        self.offset = 0.0              # mm from road centre
        self.piece_id: Optional[int] = None
        self.delocalized = False
        self.recovering = False          # came off the track: drive slowly until re-located
        self.last_msg = 0.0
        self.msg_count = 0

        self.loc = Localizer()
        self.scanner: Optional[TrackScanner] = None
        self.piece_reversed = False
        self.raw_log: deque = deque(maxlen=30000)        # recent raw messages, for replay/debugging

        self._want_speed = 0
        self._want_accel = 800
        self._sent_speed: Optional[int] = None
        self._sent_speed_t = 0.0
        self._want_lane: Optional[float] = None
        self._sent_lane: Optional[float] = None
        self._sent_lane_t = 0.0
        self._lights_override_until = 0.0

        # speed estimate between the car's (about 1/s) speed reports, from what we commanded
        self.speed_est = 0.0
        self._cmd_speed = 0
        self._cmd_accel = 800
        self._cmd_known = False          # True once we have commanded a speed ourselves

        self.on_event: Optional[Callable[[str, dict], None]] = None
        link.on_message = self._on_message
        link.on_disconnect = self._on_disconnect
        # log what we send too ('>' prefix), so recorded sessions can be replayed exactly
        raw_send = link.send

        async def logged_send(data: bytes) -> None:
            self.raw_log.append((round(time.monotonic(), 3), ">" + bytes(data).hex()))
            if len(data) >= 6 and data[1] == P.SET_SPEED:
                import struct
                self._cmd_speed, self._cmd_accel = struct.unpack_from("<hh", data, 2)
                self._cmd_known = True
            await raw_send(data)
        link.send = logged_send

    def update_estimate(self, dt: float) -> None:
        """Called every engine tick: move the speed estimate toward the commanded speed at the
        commanded acceleration, then advance the car along its current piece."""
        if self._cmd_known:
            target, acc = float(self._cmd_speed), max(100.0, float(self._cmd_accel))
            step = acc * dt
            self.speed_est += max(-step * 1.5, min(step, target - self.speed_est))
        self.loc.advance(dt, self.speed_est)

    # ---------------------------------------------------------------- connection
    async def connect(self) -> None:
        await self.link.connect()
        self.connected = True
        await self.link.send(P.sdk_mode(True))
        await asyncio.sleep(0.05)
        await self.link.send(P.version_request())
        await self.link.send(P.battery_request())
        await self.apply_color()

    async def disconnect(self) -> None:
        await self.stop()
        await self.link.close()
        self.connected = False

    def _on_disconnect(self) -> None:
        self.connected = False
        self._emit("disconnected", {})

    def _emit(self, kind: str, data: dict) -> None:
        if self.on_event:
            self.on_event(kind, {"car": self.address, **data})

    # ---------------------------------------------------------------- incoming
    def _blend_speed(self, reported: int) -> None:
        # reports lag a little; trust them more when we are not commanding the car ourselves
        w = 0.5 if self._cmd_known else 1.0
        self.speed_est = (1 - w) * self.speed_est + w * reported

    def _on_message(self, data: bytes) -> None:
        self.last_msg = time.monotonic()
        self.msg_count += 1
        mid, val = P.parse(data)
        self.raw_log.append((round(self.last_msg, 3), bytes(data).hex()))
        if self.scanner:
            self.scanner.raw.append((round(self.last_msg, 3), bytes(data).hex()))
        if mid == P.POSITION:
            self.speed = val.speed
            self._blend_speed(val.speed)
            self.offset = val.offset_mm
            self.piece_id = val.piece
            self.delocalized = False
            self.piece_reversed = val.reversed
            if self.scanner:
                self.scanner.on_position(val)
            self.loc.on_position(val.piece, val.reversed, val.location)
        elif mid == P.TRANSITION:
            self.offset = val.offset_mm
            if self.scanner:
                self.scanner.on_transition(val)
            self.loc.on_transition(val.left_wheel_cm, val.right_wheel_cm, val.offset_mm)
        elif mid == P.BATTERY_RSP:
            self.battery_mv = val
        elif mid == P.VERSION_RSP:
            self.version = val
            self.v4 = val in P.V4_FIRMWARE
        elif mid == P.SPEED_UPDATE:
            self.speed = val[2]
            self._blend_speed(val[2])
        elif mid == P.OFFSET_UPDATE:
            self.offset = val
        elif mid == P.STATUS:
            self.on_track, self.on_charger = val.on_track, val.on_charger
            self.battery_full, self.battery_low = val.battery_full, val.battery_low
        elif mid == P.DELOCALIZED:
            self.delocalized = True
            self.loc.index = None
            self._emit("delocalized", {})
        elif mid == P.COLLISION:
            self._emit("collision", {})

    # ---------------------------------------------------------------- commands
    def drive(self, speed: int, accel: int = 800, lane: Optional[float] = None) -> None:
        """Set desired speed/lane. Actually sent by `flush()` with rate limiting."""
        self._want_speed = int(max(0, min(2000, speed)))
        self._want_accel = int(accel)
        if lane is not None:
            self._want_lane = max(-P.MAX_OFFSET, min(P.MAX_OFFSET, lane))

    async def flush(self) -> None:
        if not self.connected:
            return
        now = time.monotonic()
        if (self._sent_speed is None or abs(self._want_speed - self._sent_speed) >= 20
                or (now - self._sent_speed_t > 1.5 and self._want_speed != self._sent_speed)):
            await self.link.send(P.set_speed(self._want_speed, self._want_accel, v4=self.v4))
            self._sent_speed, self._sent_speed_t = self._want_speed, now
        if (self._want_lane is not None and self.speed > 80 and now - self._sent_lane_t > 0.6
                and (self._sent_lane is None or abs(self._want_lane - self._sent_lane) > 8)):
            await self.link.send(P.set_offset(self.offset))
            await self.link.send(P.change_lane(self._want_lane, 350, 1500))
            self._sent_lane, self._sent_lane_t = self._want_lane, now

    async def stop(self) -> None:
        self._want_speed = 0
        self._want_lane = None
        self._sent_lane = None
        if self.connected:
            await self.link.send(P.set_speed(0, 1500, v4=self.v4))
            self._sent_speed = 0

    async def uturn(self) -> None:
        await self.link.send(P.uturn())

    async def request_battery(self) -> None:
        if self.connected:
            await self.link.send(P.battery_request())

    async def apply_color(self) -> None:
        if self.connected:
            r, g, b = hex_to_rgb(self.color)
            await self.link.send(P.engine_color(r, g, b))

    async def flash(self, color: str = "#ffffff", seconds: float = 1.2, cycles: int = 30) -> None:
        """Temporary light effect (identify, hits, warnings), then back to the car's colour."""
        if not self.connected:
            return
        r, g, b = hex_to_rgb(color)
        self._lights_override_until = time.monotonic() + seconds
        await self.link.send(P.engine_color(r, g, b, P.FX_FLASH, cycles))

        async def restore():
            await asyncio.sleep(seconds)
            if time.monotonic() >= self._lights_override_until:
                await self.apply_color()
        asyncio.get_running_loop().create_task(restore())

    # ---------------------------------------------------------------- view
    @property
    def battery_pct(self) -> Optional[int]:
        return P.battery_percent(self.battery_mv) if self.battery_mv else None

    def to_dict(self) -> dict:
        loc = self.loc
        return {
            "address": self.address, "nickname": self.nickname, "model": self.model,
            "tag": self.address[-5:], "speed_est": round(self.speed_est), "firmware": f"{self.version:#06x}" if self.version else None,
            "color": self.color, "simulated": self.simulated, "connected": self.connected,
            "battery": self.battery_pct, "battery_mv": self.battery_mv, "on_charger": self.on_charger, "battery_full": self.battery_full, "battery_low": self.battery_low,
            "speed": self.speed, "target_speed": self._want_speed, "offset": round(self.offset, 1),
            "piece_id": self.piece_id, "delocalized": self.delocalized,
            "index": loc.index, "direction": loc.direction,
            "frac": round(loc.fraction(self.speed, self.offset), 3), "confidence": loc.confidence,
            "laps": loc.laps, "last_lap": loc.last_lap_s, "best_lap": loc.best_lap_s,
            "progress": round(loc.progress(self.speed, self.offset), 4), "loc_stats": loc.stats,
            "scanning": self.scanner is not None,
        }
