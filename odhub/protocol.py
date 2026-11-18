"""Anki Overdrive Bluetooth LE protocol.

Message framing (from the public anki/drive-sdk): [size][msg_id][payload...]
where `size` counts every byte after itself.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

SERVICE_UUID = "be15beef-6186-407e-8381-0bd89c4d8df4"
READ_UUID = "be15bee0-6186-407e-8381-0bd89c4d8df4"    # notify: car -> host
WRITE_UUID = "be15bee1-6186-407e-8381-0bd89c4d8df4"   # write:  host -> car

# host -> car
DISCONNECT = 0x0D
PING = 0x16
VERSION_REQ = 0x18
BATTERY_REQ = 0x1A
SET_LIGHTS = 0x1D
SET_SPEED = 0x24
CHANGE_LANE = 0x25
CANCEL_LANE = 0x26
SET_OFFSET = 0x2C
TURN = 0x32
LIGHTS_PATTERN = 0x33
SDK_MODE = 0x90

# car -> host
PING_RSP = 0x17
VERSION_RSP = 0x19
BATTERY_RSP = 0x1B
POSITION = 0x27
TRANSITION = 0x29
INTERSECTION = 0x2A
DELOCALIZED = 0x2B
OFFSET_UPDATE = 0x2D
SPEED_UPDATE = 0x36
STATUS = 0x3F
LANE_CHANGE_UPDATE = 0x41
COLLISION = 0x4D

LANES = (-68.0, -23.0, 23.0, 68.0)   # mm from road centre, 4-lane track
MAX_OFFSET = 68.0

TURN_UTURN = 3

# lights_pattern channels and effects
CH_RED, CH_TAIL, CH_BLUE, CH_GREEN, CH_FRONT_L, CH_FRONT_R = range(6)
FX_STEADY, FX_FADE, FX_THROB, FX_FLASH, FX_RANDOM = range(5)
MAX_INTENSITY = 14

# Model ids per Anki-Partydrive's ModelName enum and overdrive-arena; retail names for the
# Fast & Furious cars (18 "Mammoth" = International MXT, 19 "Dynamo" = Ice Charger).
MODELS = {
    1: "Kourai", 2: "Boson", 3: "Rho", 4: "Katal", 5: "Hadion", 6: "Spektrix", 7: "Corax",
    8: "Groundshock", 9: "Skull", 10: "Thermo", 11: "Nuke", 12: "Guardian", 13: "Ice Wave",
    14: "Big Bang", 15: "Freewheel", 16: "X52", 17: "X52 Ice", 18: "MXT", 19: "Ice Charger",
    20: "Nuke Phantom", 21: "Neon Prime",
}


def model_name(model_id: int) -> str:
    return MODELS.get(model_id, f"Model {model_id}" if model_id else "Car")


def _msg(msg_id: int, payload: bytes = b"") -> bytes:
    return bytes([1 + len(payload), msg_id]) + payload


def sdk_mode(on: bool = True) -> bytes:
    # flag 0x01 = override localization, required for lane changes
    return _msg(SDK_MODE, bytes([1 if on else 0, 0x01]))


V4_FIRMWARE = (0x3061, 0x3062)    # per Anki-Partydrive: needs an extra "drive without localization" byte


def set_speed(speed: int, accel: int = 1000, v4: bool = False) -> bytes:
    speed = max(-32768, min(32767, int(speed)))
    accel = max(0, min(32767, int(accel)))
    payload = struct.pack("<hhB", speed, accel, 0)
    if v4:
        payload += b"\x01"
    return _msg(SET_SPEED, payload)


def set_offset(offset_mm: float) -> bytes:
    return _msg(SET_OFFSET, struct.pack("<f", float(offset_mm)))


def change_lane(offset_mm: float, h_speed: int = 300, h_accel: int = 1000) -> bytes:
    offset_mm = max(-MAX_OFFSET, min(MAX_OFFSET, float(offset_mm)))
    return _msg(CHANGE_LANE, struct.pack("<HHfBB", int(h_speed), int(h_accel), offset_mm, 0, 0))


def uturn() -> bytes:
    return _msg(TURN, bytes([TURN_UTURN, 0]))


def ping() -> bytes:
    return _msg(PING)


def version_request() -> bytes:
    return _msg(VERSION_REQ)


def battery_request() -> bytes:
    return _msg(BATTERY_REQ)


def disconnect() -> bytes:
    return _msg(DISCONNECT)


def lights_pattern(channels: list[tuple[int, int, int, int, int]]) -> bytes:
    """channels: up to 3 of (channel, effect, start, end, cycles_per_10s)."""
    channels = channels[:3]
    payload = bytes([len(channels)])
    for ch, fx, a, b, cyc in channels:
        payload += bytes([ch, fx, min(MAX_INTENSITY, a), min(MAX_INTENSITY, b), min(255, cyc)])
    return _msg(LIGHTS_PATTERN, payload)


def engine_color(r: int, g: int, b: int, fx: int = FX_STEADY, cycles: int = 0) -> bytes:
    """r, g, b in 0..255; scaled to the car's 0..14 range."""
    s = lambda v: round(max(0, min(255, v)) * MAX_INTENSITY / 255)
    return lights_pattern([(CH_RED, fx, s(r), s(r), cycles),
                           (CH_GREEN, fx, s(g), s(g), cycles),
                           (CH_BLUE, fx, s(b), s(b), cycles)])


# ---------------------------------------------------------------- parsing
@dataclass
class Position:
    location: int
    piece: int
    offset_mm: float
    speed: int
    flags: int

    @property
    def reversed(self) -> bool:
        return bool(self.flags & 0x40)


@dataclass
class Transition:
    piece_idx: int
    prev_piece_idx: int
    offset_mm: float
    left_wheel_cm: int | None = None
    right_wheel_cm: int | None = None

    @property
    def turn(self) -> str | None:
        """'L', 'R' or None for the piece just left (None if unknown or straight)."""
        if self.left_wheel_cm is None or self.right_wheel_cm is None:
            return None
        d = self.left_wheel_cm - self.right_wheel_cm
        return "R" if d >= 2 else "L" if d <= -2 else None


@dataclass
class Status:
    on_track: bool
    on_charger: bool
    battery_low: bool
    battery_full: bool


def parse(data: bytes) -> tuple[int, Any]:
    """Decode one notification into (msg_id, value). Unknown messages return raw bytes."""
    if len(data) < 2:
        return -1, data
    mid, body = data[1], bytes(data[2:])
    try:
        if mid == POSITION and len(body) >= 9:
            loc, piece, off, spd, flags = struct.unpack_from("<BBfHB", body)
            return mid, Position(loc, piece, off, spd, flags)
        if mid == TRANSITION and len(body) >= 6:
            piece, prev, off = struct.unpack_from("<bbf", body)
            left = right = None
            if len(body) >= 16:
                left, right = body[14], body[15]
            return mid, Transition(piece, prev, off, left, right)
        if mid == BATTERY_RSP and len(body) >= 2:
            return mid, struct.unpack_from("<H", body)[0]
        if mid == VERSION_RSP and len(body) >= 2:
            return mid, struct.unpack_from("<H", body)[0]
        if mid == OFFSET_UPDATE and len(body) >= 4:
            return mid, struct.unpack_from("<f", body)[0]
        if mid == SPEED_UPDATE and len(body) >= 6:
            return mid, struct.unpack_from("<HHH", body)   # desired, accel, actual
        if mid == STATUS and len(body) >= 4:
            return mid, Status(*(bool(b & 1) for b in body[:4]))
        if mid in (PING_RSP, DELOCALIZED, COLLISION, INTERSECTION):
            return mid, None
    except struct.error:
        pass
    return mid, body


def model_from_manufacturer(mfg: dict[int, bytes]) -> int:
    """On air the record is BE EF | reserved | model_id | 4-byte identifier (drive-sdk's
    anki_vehicle_adv_mfg_t, big-endian). bleak strips BE EF as the company id, so model is data[1]."""
    for _company, data in mfg.items():
        if len(data) >= 2:
            return data[1]
    return 0


def battery_percent(mv: int) -> int:
    return max(0, min(100, round((mv - 3300) / (4200 - 3300) * 100)))


# --- encoders for car -> host messages (used by the simulator and tests)
def encode_position(p: Position) -> bytes:
    return _msg(POSITION, struct.pack("<BBfHB", p.location, p.piece, p.offset_mm, p.speed, p.flags))


def encode_transition(t: Transition) -> bytes:
    body = struct.pack("<bbf", t.piece_idx, t.prev_piece_idx, t.offset_mm)
    body += bytes(8) + bytes([t.left_wheel_cm or 0, t.right_wheel_cm or 0])
    return _msg(TRANSITION, body)
