import asyncio
from contextlib import suppress
import unittest

from aiohttp import ClientSession, web

from mksclient_alt.moonraker import Moonraker


class MoonrakerTests(unittest.TestCase):
    def test_discovers_capabilities_and_resubscribes(self):
        async def verify():
            subscriptions = []
            sockets = []

            async def endpoint(request):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                sockets.append(ws)
                async for message in ws:
                    call = message.json()
                    method = call["method"]
                    if method == "printer.info":
                        result = {"state": "ready", "software_version": "test"}
                    elif method == "printer.objects.list":
                        result = {"objects": ["extruder", "print_stats", "future_object"]}
                    elif method == "printer.objects.subscribe":
                        subscriptions.append(call["params"]["objects"])
                        result = {"status": {"extruder": {"temperature": 26, "target": 0}}}
                    else:
                        result = "ok"
                    await ws.send_json({"jsonrpc": "2.0", "id": call["id"], "result": result})
                return ws

            app = web.Application()
            app.router.add_get("/websocket", endpoint)
            runner = web.AppRunner(app)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", 0).start()
                host, port = runner.addresses[0]
                async with ClientSession() as session:
                    client = Moonraker(session, "http://{}:{}".format(host, port))
                    task = asyncio.create_task(client.run())
                    try:
                        async def wait_for(check):
                            while not check():
                                client.changed.clear()
                                await client.changed.wait()

                        await asyncio.wait_for(wait_for(lambda: client.ready), 3)
                        self.assertEqual(set(subscriptions[0]), {"extruder", "print_stats"})
                        await sockets[0].send_json({
                            "jsonrpc": "2.0", "method": "notify_status_update",
                            "params": [{"extruder": {"temperature": 29}}, 1.0],
                        })
                        await asyncio.wait_for(wait_for(lambda: client.status["extruder"]["temperature"] == 29), 3)
                        self.assertEqual(client.status["extruder"]["target"], 0)
                        await sockets[0].close()
                        await asyncio.wait_for(wait_for(lambda: client.ready and client.generation == 2), 5)
                        self.assertEqual(subscriptions[0], subscriptions[1])
                    finally:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
            finally:
                await runner.cleanup()

        asyncio.run(verify())
