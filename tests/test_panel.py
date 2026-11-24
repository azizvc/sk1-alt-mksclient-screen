import asyncio
import unittest
from unittest.mock import patch

from mksclient_alt.panel import (
    FILES, HOME, MOVE, NETWORK, SYSTEM, SYSTEM_MORE, WIFI_CONNECTED, WIFI_CONFIRM,
    WIFI_CONNECTING, WIFI_KEYBOARD, WIFI_STATUS_BACKGROUND, WIFI_SUCCESS_SECONDS, Panel,
)
from mksclient_alt.protocol import PanelDecoder, PanelEvent
from mksclient_alt.wifi import WifiNetwork


class StubMoonraker:
    ready = True

    def __init__(self):
        self.calls = []
        self.status = {
            "toolhead": {"position": [50, 100, 100, 0], "homed_axes": "xyz",
                         "axis_minimum": [0, 0, 0], "axis_maximum": [256, 256, 256]},
            "print_stats": {"state": "standby"},
            "extruder": {"temperature": 28, "target": 0},
            "virtual_sdcard": {"progress": 0, "is_active": False},
        }

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "printer.objects.query":
            return {"status": {key: self.status[key] for key in params["objects"]}}
        if method == "server.files.list":
            return [{"path": "sub/part.gcode", "modified": 10}, {"path": "exclude.txt", "modified": 11}]
        return "ok"


class PanelTests(unittest.TestCase):
    def test_system_arrows_follow_the_stock_two_page_order(self):
        async def verify():
            commands = []

            async def send(command):
                commands.append(command)

            panel = Panel(StubMoonraker(), send)
            await panel.go(SYSTEM)
            await panel.handle(PanelEvent("button", SYSTEM, 12))
            await panel.handle(PanelEvent("button", SYSTEM, 10))
            await panel.handle(PanelEvent("button", SYSTEM_MORE, 12))
            await panel.handle(PanelEvent("button", SYSTEM_MORE, 11))
            self.assertEqual(commands, ["page 11", "page 14", "page 11"])

        asyncio.run(verify())

    def test_wifi_tap_asks_for_confirmation_before_accepting_password(self):
        async def verify():
            moonraker = StubMoonraker()
            attempts = []
            commands = []

            async def addresses():
                return []

            async def send(command):
                commands.append(command)

            async def join(network, password):
                attempts.append((network.ssid, password))

            moonraker.network_addresses = addresses
            panel = Panel(moonraker, send, wifi_connector=join)
            panel.wifi_networks = [WifiNetwork("Current", -61, True), WifiNetwork("Guest", -70, False, "psk")]
            await panel.go(NETWORK)
            await panel.handle(PanelEvent("button", NETWORK, 7))
            self.assertEqual(panel.page, WIFI_CONNECTED)
            await panel.handle(PanelEvent("button", WIFI_CONNECTED, 0))
            await panel.handle(PanelEvent("button", NETWORK, 8))
            self.assertEqual(panel.page, WIFI_CONFIRM)
            self.assertIn('t0.txt="Guest"', commands)
            await panel.handle(PanelEvent("button", WIFI_CONFIRM, 0))
            self.assertEqual(panel.page, WIFI_KEYBOARD)
            await panel.handle(PanelEvent("wifi_password", NETWORK, 7, secret=b"wrong-row"))
            self.assertEqual(attempts, [])
            await panel.handle(PanelEvent("wifi_password", NETWORK, 8, secret=b"test-password"))
            self.assertEqual(panel.page, WIFI_CONNECTING)
            clear = "cls {}".format(WIFI_STATUS_BACKGROUND)
            self.assertEqual(commands[-2:], [clear, "page 59"])
            await panel.wifi_connection
            with patch("mksclient_alt.panel.monotonic", return_value=100):
                await panel.finish_wifi_connection()
            self.assertEqual(attempts, [("Guest", b"test-password")])
            self.assertEqual(panel.page, WIFI_CONNECTED)
            self.assertEqual(commands[-2:], [clear, "page 60"])
            with patch("mksclient_alt.panel.monotonic", return_value=100 + WIFI_SUCCESS_SECONDS):
                await panel.finish_wifi_connection()
            self.assertEqual(panel.page, NETWORK)

        asyncio.run(verify())

    def test_network_displays_current_ipv4_and_refreshes(self):
        async def verify():
            moonraker = StubMoonraker()
            moonraker.ready = False
            rendered = []
            addresses = [("wlan0", "192.168.0.17")]
            networks = [WifiNetwork("Rede {}".format(i), -60, i == 1) for i in range(1, 6)]

            async def network_addresses():
                return list(addresses)

            async def send(command):
                rendered.append(command)

            async def scan():
                return list(networks)

            moonraker.network_addresses = network_addresses
            panel = Panel(moonraker, send, wifi_scanner=scan)
            panel.sync_page(SYSTEM)
            await panel.handle(PanelEvent("button", SYSTEM, 6))
            self.assertEqual(panel.page, NETWORK)
            self.assertIn('t5.txt="IP: 192.168.0.17"', rendered)
            self.assertIn("get var32_FLDEBE29", rendered)
            for reply in PanelDecoder().feed(bytes.fromhex("71 06 00 00 00 ff ff ff")):
                await panel.handle(reply)
            self.assertIn('b7.txt="Scanning for networks..."', rendered)
            await panel.wifi_scan
            await panel.finish_wifi_scan()
            self.assertIn('b7.txt="Rede 1 [Connected]"', rendered)
            self.assertIn('b10.txt="Rede 4"', rendered)
            await panel.handle(PanelEvent("button", NETWORK, 12))
            self.assertIn('b7.txt="Rede 5"', rendered)
            self.assertEqual(panel.sent_attributes["b8.txt"], '""')
            addresses[0] = ("eth0", "192.168.0.23")
            networks[:] = [WifiNetwork("Nova rede", -75, False)]
            await panel.handle(PanelEvent("button", NETWORK, 14))
            await panel.wifi_scan
            await panel.finish_wifi_scan()
            self.assertIn('t5.txt="IP: 192.168.0.23"', rendered)
            self.assertIn('b7.txt="Nova rede"', rendered)

        asyncio.run(verify())

    def test_network_rereads_the_language_on_entry(self):
        async def verify():
            moonraker = StubMoonraker()

            async def addresses():
                return []

            async def send(command):
                pass

            moonraker.network_addresses = addresses
            panel = Panel(moonraker, send)
            panel.wifi_networks = [WifiNetwork("Test", -70, True)]
            for language, marker in ((6, "Connected"), (7, "Verbunden")):
                await panel.go(NETWORK)
                self.assertEqual(panel.sent_attributes["b7.txt"], '"* Test"')
                await panel.handle(PanelEvent("number", language, 0, 0))
                self.assertEqual(panel.sent_attributes["b7.txt"], '"Test [{}]"'.format(marker))
                await panel.go(SYSTEM)

        asyncio.run(verify())

    def test_view_follows_a_print_started_from_another_client(self):
        async def verify():
            moonraker = StubMoonraker()

            async def send(command):
                pass

            panel = Panel(moonraker, send)
            await panel.handle(PanelEvent("hello"))
            await panel.update()
            self.assertEqual(panel.page, HOME)
            moonraker.status["print_stats"]["state"] = "printing"
            await panel.update()
            self.assertEqual(panel.page, 2)
            moonraker.status["print_stats"]["state"] = "complete"
            await panel.update()
            self.assertEqual(panel.page, HOME)

        asyncio.run(verify())

    def test_completed_visible_job_opens_the_stock_print_summary(self):
        async def verify():
            moonraker = StubMoonraker()

            async def send(command):
                pass

            panel = Panel(moonraker, send)
            panel.sync_page(HOME)
            moonraker.status["print_stats"].update(state="printing", filename="part.gcode", print_duration=100)
            await panel.update()
            moonraker.status["print_stats"]["state"] = "complete"
            await panel.update()
            self.assertEqual(panel.page, 77)
            await panel.handle(PanelEvent("button", 77, 6))
            self.assertEqual(panel.page, HOME)

        asyncio.run(verify())

    def test_print_selection_and_explicit_confirmation(self):
        async def verify():
            moonraker = StubMoonraker()
            rendered = []

            async def send(command):
                rendered.append(command)

            panel = Panel(moonraker, send, enable_controls=True)
            await panel.handle(PanelEvent("hello"))
            self.assertEqual(panel.page, HOME)
            self.assertIn("n0.val=28", rendered)
            await panel.handle(PanelEvent("button", HOME, 2))
            self.assertEqual(panel.page, FILES)
            self.assertIn('o30_BLMLHY6D.txt="sub/"', rendered)
            await panel.handle(PanelEvent("button", FILES, 8))
            self.assertIn('o30_BLMLHY6D.txt="part.gcode"', rendered)
            await panel.handle(PanelEvent("button", FILES, 8))
            self.assertEqual(panel.page, 23)
            await panel.handle(PanelEvent("button", 23, 0))
            self.assertEqual(panel.page, 9)
            self.assertTrue(all(x[0].startswith("server.files.") for x in moonraker.calls))
            await panel.handle(PanelEvent("button", 9, 7))
            self.assertEqual(moonraker.calls[-1], ("printer.print.start", {"filename": "sub/part.gcode"}))

        asyncio.run(verify())

    def test_home_light_icon_follows_caselight_state(self):
        async def verify():
            moonraker = StubMoonraker()
            moonraker.status["output_pin caselight"] = {"value": 0.0}
            commands = []

            async def send(command):
                commands.append(command)

            panel = Panel(moonraker, send, enable_controls=True)
            await panel.go(HOME)
            self.assertIn("b5.picc=2", commands)
            commands.clear()
            panel.sent_attributes.clear()
            await panel.handle(PanelEvent("button", HOME, 5))
            self.assertEqual(moonraker.calls[-1],
                             ("printer.gcode.script", {"script": "SET_PIN PIN=caselight VALUE=1"}))
            self.assertIn("b5.picc=5", commands)
            commands.clear()
            panel.sent_attributes.clear()
            moonraker.status["output_pin caselight"]["value"] = 1.0
            await panel.handle(PanelEvent("button", HOME, 5))
            self.assertIn("b5.picc=2", commands)

        asyncio.run(verify())

    def test_move_temp_shows_reading_and_only_active_targets(self):
        async def verify():
            moonraker = StubMoonraker()
            moonraker.status["heater_bed"] = {"temperature": 31.2, "target": 0}
            commands = []

            async def send(command):
                commands.append(command)

            panel = Panel(moonraker, send)
            await panel.go(MOVE)
            self.assertIn("b16.picc=8", commands)
            self.assertIn("vis n0,0", commands)
            self.assertIn("n3.val=28", commands)
            self.assertIn("n2.val=31", commands)
            self.assertNotIn("n0.val=28", commands)
            self.assertNotIn("n3.val=0", commands)

            commands.clear()
            panel.sent_attributes.clear()
            moonraker.status["extruder"] = {"temperature": 28, "target": 200}
            moonraker.status["heater_bed"] = {"temperature": 31.2, "target": 60}
            await panel.render()
            self.assertIn("b16.picc=49", commands)
            self.assertIn("vis n0,1", commands)
            self.assertIn("n0.val=28", commands)
            self.assertIn("n3.val=200", commands)
            self.assertIn("n1.val=31", commands)
            self.assertIn("n2.val=60", commands)

        asyncio.run(verify())

    def test_jog_checks_live_print_state_and_axis_limits(self):
        async def verify():
            moonraker = StubMoonraker()

            async def send(command):
                pass

            panel = Panel(moonraker, send, enable_controls=True)
            panel.sync_page(MOVE)
            touch = PanelEvent("button", MOVE, 14)
            moonraker.status["toolhead"]["position"][0] = 256
            await panel.handle(touch)
            self.assertEqual(moonraker.calls[-1][0], "printer.objects.query")
            moonraker.status["toolhead"]["position"][0] = 50
            moonraker.status["print_stats"]["state"] = "paused"
            await panel.handle(touch)
            self.assertEqual(moonraker.calls[-1][0], "printer.objects.query")
            moonraker.status["print_stats"]["state"] = "standby"
            await panel.handle(touch)
            self.assertIn("G1 X1 F3000", moonraker.calls[-1][1]["script"])

        asyncio.run(verify())

    def test_keypad_requires_the_matching_navigation_and_opt_in(self):
        async def verify():
            moonraker = StubMoonraker()

            async def send(command):
                pass

            panel = Panel(moonraker, send)
            panel.sync_page(HOME)
            await panel.handle(PanelEvent("button", HOME, 7))
            await panel.handle(PanelEvent("number", HOME, 8, 100))
            self.assertEqual(panel.page, 1)
            await panel.handle(PanelEvent("number", HOME, 7, 215))
            self.assertEqual(panel.page, HOME)
            self.assertEqual(moonraker.calls, [])
            panel.enable_controls = True
            await panel.handle(PanelEvent("button", HOME, 7))
            await panel.handle(PanelEvent("number", HOME, 7, 215))
            self.assertEqual(moonraker.calls, [("printer.gcode.script", {"script": "M104 S215"})])

        asyncio.run(verify())
