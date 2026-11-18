"""HTTP + WebSocket server. Serves the UI and relays commands to the engine."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import WSMsgType, web

from .engine import Engine

log = logging.getLogger("odhub.server")
WEB = Path(__file__).resolve().parent.parent / "web"
THREE = Path(__file__).resolve().parent.parent / "app" / "node_modules" / "three"
STATE_HZ = 20


class Server:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.clients: set[web.WebSocketResponse] = set()
        engine.listeners.append(self._push)
        self.app = web.Application()
        routes = [web.get("/ws", self.ws), web.get("/health", self.health),
                  web.post("/shutdown", self.shutdown), web.get("/", self.index)]
        if THREE.exists():
            routes.append(web.static("/vendor/three", THREE))
        routes.append(web.static("/", WEB))
        self.app.add_routes(routes)
        self.app.on_startup.append(self._startup)

    async def _startup(self, _app) -> None:
        self.engine.start()
        asyncio.get_running_loop().create_task(self._broadcast_loop())

    async def index(self, _req):
        return web.FileResponse(WEB / "index.html")

    async def health(self, _req):
        return web.json_response({"ok": True})

    async def shutdown(self, _req):
        """Stop every car, disconnect cleanly, then exit the process."""
        try:
            self.engine.game = None
            await asyncio.wait_for(self.engine.stop_all(), 1.0)
            await asyncio.wait_for(asyncio.gather(*(self.engine.disconnect(a) for a in list(self.engine.cars)),
                                                  return_exceptions=True), 2.0)
        except Exception:  # noqa: BLE001
            pass
        asyncio.get_running_loop().call_later(0.2, lambda: os._exit(0))
        return web.json_response({"ok": True})

    def _push(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.get_running_loop().create_task(ws.send_str(data))

    def _push_catalog(self) -> None:
        self._push(self.engine.catalog())

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(1 / STATE_HZ)
            if self.clients:
                self._push(self.engine.snapshot())

    async def ws(self, req):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        self.clients.add(ws)
        await ws.send_str(json.dumps(self.engine.catalog()))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    cmd = json.loads(msg.data)
                    await self.handle(cmd)
                except Exception as e:  # noqa: BLE001 - report to the UI, keep the socket
                    log.exception("command failed")
                    await ws.send_str(json.dumps({"type": "notice", "level": "error", "text": f"Error: {e}"}))
        finally:
            self.clients.discard(ws)
        return ws

    async def handle(self, c: dict) -> None:
        e, op = self.engine, c.get("op")
        lib = e.lib
        if op == "input":                      # hot path, no catalog refresh
            e.human_input(c["car"], c.get("throttle"), c.get("lane"), c.get("lane_step"),
                          c.get("fire", False), c.get("mine", False))
            return
        if op == "discover":
            await e.discover(c.get("seconds", 4))
        elif op == "connect":
            await e.connect(c["car"])
        elif op == "check_charge":
            if not getattr(e, "_charge_task", None) or e._charge_task.done():
                e._charge_task = asyncio.get_running_loop().create_task(e.check_charge())
        elif op == "connect_all":
            await e.connect_all()
        elif op == "disconnect":
            await e.disconnect(c["car"])
        elif op == "blink":
            await e.blink(c["car"])
        elif op == "update_car":
            await e.update_car(c["car"], c.get("nickname"), c.get("color"))
        elif op == "forget_car":
            lib.forget_car(c["car"])
        elif op == "set_controller":
            e.set_controller(c["car"], c["spec"])
        elif op == "stop_all":
            await e.stop_all()
        elif op == "uturn":
            car = e.cars.get(c["car"])
            if car:
                await car.uturn()
        elif op == "scan_track":
            e.start_track_scan(c["car"])
        elif op == "cancel_scan":
            await e.cancel_track_scan()
        elif op == "select_track":
            e.select_track(c.get("key"))
        elif op == "edit_track":
            e.edit_track(c["key"], c.get("name"), c.get("turns"), c.get("mirror", False), c.get("fix", False))
        elif op == "delete_track":
            if e.track_key == c["key"]:
                e.select_track(None)
            lib.delete_track(c["key"])
        elif op == "save_racer":
            lib.save_racer(c["data"], c.get("key"))
        elif op == "delete_racer":
            lib.delete_racer(c["key"])
        elif op == "save_mode":
            lib.save_mode(c["data"], c.get("key"))
        elif op == "delete_mode":
            lib.delete_mode(c["key"])
        elif op == "start_game":
            e.start_game(c["mode"], c["cars"], c.get("overrides"))
        elif op == "stop_game":
            await e.stop_game()
        elif op == "reload":
            lib.reload()
        elif op == "dump_log":                 # save a car's recent raw messages for offline replay
            car = e.cars.get(c["car"])
            if car:
                import time as _t
                out = lib.root / "test-logs"
                out.mkdir(exist_ok=True)
                path = out / f"raw-{car.address[-5:].replace(':', '')}-{_t.strftime('%H%M%S')}.json"
                path.write_text(json.dumps({"car": car.address, "model": car.model, "firmware": car.version,
                                            "track_key": e.track_key, "messages": list(car.raw_log)}))
                e.notify(f"Saved raw log {path.name}")
        self._push_catalog()


def run(engine: Engine, host: str = "127.0.0.1", port: int = 8765) -> None:
    web.run_app(Server(engine).app, host=host, port=port, print=lambda *_: None)
