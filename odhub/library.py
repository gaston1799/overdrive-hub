"""Everything saved to disk: tracks, the garage (your cars), custom racers and custom modes."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from .drivers import BUILTIN_RACERS, DEFAULT_PROFILE, AIDriver
from .modes import BASE_RULES, BUILTIN_MODES
from .track import Track

log = logging.getLogger("odhub.library")

PALETTE = ["#e53935", "#1e88e5", "#43a047", "#fdd835", "#8e24aa", "#fb8c00", "#00acc1", "#f06292",
           "#7cb342", "#5e35b1", "#ffb300", "#26a69a", "#d81b60", "#3949ab", "#c0ca33", "#6d4c41"]


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or f"item-{int(time.time())}"


class Library:
    def __init__(self, root: Path):
        self.root = root
        for sub in ("tracks", "racers", "modes"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.garage_file = root / "garage.json"
        self.settings_file = root / "settings.json"
        self.garage: dict[str, dict] = self._read(self.garage_file, {})
        # model ids saved before v2 came from the wrong advert byte: forget them until seen again
        for g in self.garage.values():
            if g.get("model_v") != 2:
                if self._is_auto_name(g):
                    g["auto_name"] = True
                g["model"], g["model_v"] = "Needs rescan", 2
                if g.get("auto_name"):
                    g["nickname"] = "Needs rescan"
        self.settings: dict = self._read(self.settings_file, {})
        self.tracks: dict[str, Track] = {}
        self.racers: dict[str, dict] = {}
        self.modes: dict[str, dict] = {}
        self.plugins: dict[str, type] = {}
        self.reload()

    @staticmethod
    def _read(path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(path)

    def reload(self) -> None:
        self.tracks = {}
        for f in sorted((self.root / "tracks").glob("*.json")):
            d = self._read(f, None)
            if d:
                try:
                    self.tracks[f.stem] = Track.from_dict(d)
                except (KeyError, TypeError) as e:
                    log.warning("bad track file %s: %s", f, e)
        self.racers = {k: {**v, "builtin": True} for k, v in BUILTIN_RACERS.items()}
        for f in sorted((self.root / "racers").glob("*.json")):
            d = self._read(f, None)
            if d:
                self.racers[f.stem] = {**DEFAULT_PROFILE, **d, "builtin": False}
        self.plugins = {}
        for f in sorted((self.root / "racers").glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"odhub_racer_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cls = getattr(mod, "Racer")
                if issubclass(cls, AIDriver):
                    self.plugins[f.stem] = cls
                    self.racers.setdefault(f.stem, {**DEFAULT_PROFILE, **getattr(mod, "PROFILE", {}),
                                                    "name": getattr(mod, "NAME", f.stem), "builtin": False,
                                                    "plugin": True})
            except Exception as e:  # noqa: BLE001 - user plugin code
                log.warning("racer plugin %s failed to load: %s", f.name, e)
        self.modes = {k: {**v, "builtin": True} for k, v in BUILTIN_MODES.items()}
        for f in sorted((self.root / "modes").glob("*.json")):
            d = self._read(f, None)
            if d:
                self.modes[f.stem] = {**BASE_RULES, **d, "builtin": False}

    # ---------------------------------------------------------------- garage
    @staticmethod
    def tag(address: str) -> str:
        """Short label for a car: last two bytes of its Bluetooth address (write it on a sticker)."""
        return address[-5:] if len(address) >= 5 else address

    def _is_auto_name(self, g: dict) -> bool:
        if "auto_name" in g:
            return bool(g["auto_name"])
        # profiles from before the flag existed: auto if the name is just "<model>" or "<model> <n>"
        return bool(re.fullmatch(re.escape(g.get("model", "")) + r"( \d+)?", g.get("nickname", "")))

    def car_profile(self, address: str, model: str) -> dict:
        g = self.garage.get(address)
        if g is None:
            used = {x["color"] for x in self.garage.values()}
            color = next((c for c in PALETTE if c not in used), PALETTE[len(self.garage) % len(PALETTE)])
            g = self.garage[address] = {"nickname": model, "color": color, "model": model, "model_v": 2,
                                        "auto_name": True, "added": time.time()}
        else:
            g["auto_name"] = self._is_auto_name(g)
            g["model"] = model
        self._renumber()
        self._write(self.garage_file, self.garage)
        return g

    def _renumber(self) -> None:
        """Auto-named duplicates become 'Skull 1', 'Skull 2'... in a stable order (date added)."""
        groups: dict[str, list[str]] = {}
        for a, g in self.garage.items():
            groups.setdefault(g.get("model", "Car"), []).append(a)
        for model, addrs in groups.items():
            addrs.sort(key=lambda a: (self.garage[a].get("added", 0), a))
            for i, a in enumerate(addrs, 1):
                g = self.garage[a]
                if self._is_auto_name(g):
                    g["nickname"] = f"{model} {i}" if len(addrs) > 1 else model
                    g["auto_name"] = True

    def update_car(self, address: str, **fields) -> dict:
        g = self.garage.setdefault(address, {})
        if fields.get("nickname") is not None:
            name = fields["nickname"].strip()
            g["auto_name"] = not name           # clearing the name goes back to the automatic one
            fields["nickname"] = name or g.get("model", "Car")
            if not name:
                self._renumber()
        g.update({k: v for k, v in fields.items() if k in ("nickname", "color", "notes") and v is not None})
        self._write(self.garage_file, self.garage)
        return g

    def forget_car(self, address: str) -> None:
        self.garage.pop(address, None)
        self._write(self.garage_file, self.garage)

    # ---------------------------------------------------------------- tracks
    def find_same_track(self, track: Track) -> Optional[str]:
        return next((k for k, t in self.tracks.items() if t.same_layout(track)), None)

    def save_track(self, track: Track, key: Optional[str] = None) -> str:
        key = key or self._unique(slug(track.name), self.tracks)
        self.tracks[key] = track
        self._write(self.root / "tracks" / f"{key}.json", track.to_dict())
        return key

    def delete_track(self, key: str) -> None:
        self.tracks.pop(key, None)
        (self.root / "tracks" / f"{key}.json").unlink(missing_ok=True)

    def next_track_name(self) -> str:
        n = len(self.tracks) + 1
        while any(t.name == f"Track {n}" for t in self.tracks.values()):
            n += 1
        return f"Track {n}"

    # ---------------------------------------------------------------- racers / modes
    def save_racer(self, data: dict, key: Optional[str] = None) -> str:
        if key and self.racers.get(key, {}).get("builtin"):
            key = None                                   # editing a built-in saves a copy
        clean = {k: data[k] for k in DEFAULT_PROFILE if k in data}
        key = key or self._unique(slug(clean.get("name", "racer")), self.racers)
        self._write(self.root / "racers" / f"{key}.json", clean)
        self.racers[key] = {**DEFAULT_PROFILE, **clean, "builtin": False}
        return key

    def delete_racer(self, key: str) -> None:
        if not self.racers.get(key, {}).get("builtin"):
            self.racers.pop(key, None)
            (self.root / "racers" / f"{key}.json").unlink(missing_ok=True)

    def save_mode(self, data: dict, key: Optional[str] = None) -> str:
        if key and self.modes.get(key, {}).get("builtin"):
            key = None
        clean = {k: data[k] for k in list(BASE_RULES) + ["name", "description"] if k in data}
        key = key or self._unique(slug(clean.get("name", "mode")), self.modes)
        self._write(self.root / "modes" / f"{key}.json", clean)
        self.modes[key] = {**BASE_RULES, **clean, "builtin": False}
        return key

    def delete_mode(self, key: str) -> None:
        if not self.modes.get(key, {}).get("builtin"):
            self.modes.pop(key, None)
            (self.root / "modes" / f"{key}.json").unlink(missing_ok=True)

    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        self._write(self.settings_file, self.settings)

    @staticmethod
    def _unique(base: str, existing: dict) -> str:
        key, i = base, 2
        while key in existing:
            key, i = f"{base}-{i}", i + 1
        return key
