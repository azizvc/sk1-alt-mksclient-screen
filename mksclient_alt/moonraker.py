"""Moonraker JSON-RPC transport, independent of UART and panel mappings."""

import asyncio
from contextlib import suppress
from ipaddress import AddressValueError, IPv4Address
import itertools
import logging

import aiohttp

LOG = logging.getLogger(__name__)
OBJECTS = {
    "extruder": ["temperature", "target", "can_extrude"],
    "heater_bed": ["temperature", "target"],
    "print_stats": ["state", "filename", "print_duration", "message"],
    "virtual_sdcard": ["progress", "is_active"],
    "toolhead": ["position", "homed_axes", "axis_minimum", "axis_maximum"],
    "fan": ["speed"],
    "fan_generic Side_fan": ["speed"],
    "fan_generic Filter_fan": ["speed"],
    "output_pin caselight": ["value"],
    "pause_resume": ["is_paused"],
}


class RpcError(RuntimeError):
    pass


class Moonraker:
    def __init__(self, session, url):
        self.session = session
        self.base_url = url.rstrip("/")
        self.url = self.base_url + "/websocket"
        self.ws = None
        self.status = {}
        self.ready = False
        self.generation = 0
        self.changed = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending = {}
        self._reconnect = asyncio.Event()

    async def call(self, method, params=None, timeout=8):
        if self.ws is None or self.ws.closed:
            raise RpcError("Moonraker desconectado")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.ws.send_json({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        except (aiohttp.ClientError, OSError) as exc:
            raise RpcError("Conexão Moonraker interrompida") from exc
        finally:
            self._pending.pop(request_id, None)

    async def network_addresses(self):
        async with self.session.get(self.base_url + "/machine/system_info", timeout=3) as response:
            response.raise_for_status()
            payload = await response.json()
        interfaces = payload["result"]["system_info"]["network"]
        addresses = []
        for interface, details in interfaces.items():
            for item in details.get("ip_addresses", ()):
                try:
                    address = IPv4Address(item.get("address", ""))
                except AddressValueError:
                    continue
                if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
                    addresses.append((interface, str(address)))
        addresses.sort(key=lambda row: (not row[0].startswith(("wl", "en", "eth")), row[0]))
        return addresses

    def _merge(self, update):
        for name, values in update.items():
            if isinstance(values, dict):
                self.status.setdefault(name, {}).update(values)
        self.changed.set()

    async def _receive(self, ws):
        try:
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                frame = message.json()
                if not isinstance(frame, dict):
                    continue
                if "id" in frame:
                    pending = self._pending.get(frame["id"])
                    if pending is not None and not pending.done():
                        if "error" in frame:
                            pending.set_exception(RpcError(str(frame["error"])))
                        else:
                            pending.set_result(frame.get("result"))
                    continue
                method = frame.get("method")
                params = frame.get("params") or []
                if method == "notify_status_update" and params and isinstance(params[0], dict):
                    self._merge(params[0])
                elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown", "notify_klippy_ready"):
                    self.ready = False
                    self.status.clear()
                    self.changed.set()
                    self._reconnect.set()
        finally:
            self.ready = False
            self.status.clear()
            self.changed.set()
            self._reconnect.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RpcError("Conexão Moonraker encerrada"))

    async def run(self):
        delay = 1
        while True:
            try:
                self._reconnect.clear()
                async with self.session.ws_connect(self.url, heartbeat=15) as ws:
                    self.ws = ws
                    receive = asyncio.create_task(self._receive(ws))
                    try:
                        info = await self.call("printer.info")
                        if info.get("state") != "ready":
                            raise RpcError("Klipper: " + str(info.get("state")))
                        available = (await self.call("printer.objects.list"))["objects"]
                        objects = {name: fields for name, fields in OBJECTS.items() if name in available}
                        initial = await self.call("printer.objects.subscribe", {"objects": objects})
                        # Notifications may already have arrived while awaiting the response.
                        newest = self.status
                        self.status = {}
                        self._merge(initial["status"])
                        self._merge(newest)
                        if not self._reconnect.is_set():
                            self.generation += 1
                            self.ready = True
                            self.changed.set()
                            LOG.info("Klipper %s, %d objetos inscritos", info.get("software_version"), len(objects))
                            delay = 1
                        await self._reconnect.wait()
                    finally:
                        receive.cancel()
                        with suppress(asyncio.CancelledError):
                            await receive
            except (aiohttp.ClientError, asyncio.TimeoutError, RpcError, ValueError, KeyError) as exc:
                LOG.warning("Moonraker: %s", exc)
            finally:
                self.ws = None
                self.ready = False
                self.status.clear()
                self.changed.set()
            await asyncio.sleep(delay)
            delay = min(15, delay * 2)
