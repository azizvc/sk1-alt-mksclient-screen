import asyncio
import unittest
from unittest.mock import patch

from mksclient_alt.protocol import PanelDecoder, PanelEvent
from mksclient_alt.system_events import RESTART_TIMEOUT_SECONDS, SystemEvents


class StubMoonraker:
    generation = 1
    ready = True

    def __init__(self):
        self.calls = []
        self.replies = {}

    async def call(self, method, params=None, timeout=8):
        self.calls.append((method, params))
        return self.replies[method]


class SystemHarness(SystemEvents):
    def __init__(self, page, *, controls=False):
        self.page = page
        self.enable_controls = controls
        self.job = "standby"
        self.commands = []
        self.sent_attributes = {}
        self.moonraker = StubMoonraker()
        self.http_calls = []
        self.http_replies = {}

    async def paint(self, commands):
        for command in commands:
            name, _, value = command.partition("=")
            if self.sent_attributes.get(name) != value:
                self.sent_attributes[name] = value
                self.commands.append(command)

    def sync_page(self, page):
        if page != self.page:
            self.page = page
            self.sent_attributes.clear()

    async def go(self, page):
        self.commands.append("page {}".format(page))
        self.sync_page(page)
        await self.render_system()

    async def open_local_files(self):
        await self.go(7)

    async def system_http(self, method, endpoint):
        self.http_calls.append((method, endpoint))
        return self.http_replies[endpoint]

    async def button(self, page, action):
        frames = PanelDecoder().feed(bytes((0x65, page, action, 0xff, 0xff, 0xff)))
        return await self.handle_system(next(frames))


class SystemTests(unittest.TestCase):
    def test_screen_and_language_changes_are_applied_locally_on_the_tft(self):
        async def verify():
            panel = SystemHarness(11)
            for action in range(4):
                await panel.button(11, 7)
                self.assertEqual(panel.page, 12)
                await panel.button(12, action)
                self.assertEqual(panel.page, 11)
            for action in (*range(5, 13), 16):
                await panel.button(11, 8)
                self.assertEqual(panel.page, 13)
                await panel.button(13, action)
                self.assertEqual(panel.page, 11)
            self.assertEqual(panel.commands, [])
            await panel.button(11, 7)
            await panel.button(12, 10)
            self.assertEqual(panel.commands, ["page 11"])

        asyncio.run(verify())

    def test_support_views_and_footer_have_distinct_routes(self):
        async def verify():
            panel = SystemHarness(11)
            await panel.button(11, 4)
            await panel.button(21, 6)
            await panel.button(52, 7)
            await panel.button(53, 5)
            await panel.button(21, 3)
            await panel.button(11, 2)
            self.assertEqual(panel.commands, ["page 21", "page 52", "page 53", "page 21", "page 11", "page 7"])
            self.assertFalse(await panel.button(11, 14))

        asyncio.run(verify())

    def test_about_uses_read_only_fields_and_clears_vendor_placeholders(self):
        async def verify():
            panel = SystemHarness(14)
            panel.moonraker.replies = {
                "printer.info": {"software_version": "v0.12.0"},
                "machine.system_info": {"system_info": {"network": {
                    "lo": {"mac_address": "loopback"},
                    "wlan0": {"mac_address": "12:34:56:78:9a:bc"},
                }}},
                "server.history.totals": {"job_totals": {"total_print_time": 24 * 3600 + 3720}},
                "server.files.get_directory": {"disk_usage": {"used": 2000000000, "total": 8000000000}},
            }
            await panel.button(14, 8)
            await panel.render_system()
            self.assertEqual(len(panel.moonraker.calls), 4)
            self.assertEqual(panel.commands, [
                "page 15", 't14.txt="Klipper"', 't9.txt="v0.12.0"', 't10.txt="12:34:56:78:9A:BC"',
                't11.txt="01d01h02m"', 't12.txt="2.0/8.0GB"', 't15.txt="v0.12.0"',
            ])
            panel.moonraker.ready = False
            await panel.render_system()
            self.assertEqual(panel.sent_attributes["t9.txt"], '"--"')
            panel.moonraker.ready = True
            await panel.render_system()
            self.assertEqual(panel.sent_attributes["t9.txt"], '"v0.12.0"')

        asyncio.run(verify())

    def test_unavailable_operations_leave_printer_untouched(self):
        async def verify():
            panel = SystemHarness(11, controls=True)
            await panel.button(11, 13)
            await panel.button(11, 14)
            await panel.button(63, 6)
            await panel.button(63, 5)
            await panel.button(11, 10)
            await panel.button(14, 10)
            await panel.button(20, 9)
            await panel.button(20, 6)
            self.assertIn('t1.txt="Factory reset unavailable"', panel.commands)
            await panel.button(20, 5)
            macro = "gcode_macro OBICO_LINK_STATUS"
            status = {"is_linked": False, "one_time_passlink": "https://example.com/link"}
            panel.moonraker.replies["printer.objects.query"] = {"status": {macro: status}}
            await panel.button(14, 9)
            self.assertIn('qr0.txt="https://example.com/link"', panel.commands)
            self.assertIn("qr0.aph=127", panel.commands)
            await panel.button(78, 6)
            self.assertEqual(panel.moonraker.calls, [("printer.objects.query", {"objects": {macro: None}})])
            status["is_linked"] = True
            panel.obico_poll_at = 0
            await panel.render_system()
            self.assertIn('qr0.txt=""', panel.commands)
            self.assertIn('t2.txt="Obico linked"', panel.commands)
            self.assertEqual(panel.http_calls, [])

        asyncio.run(verify())

    def test_export_diary_back_returns_to_source_and_survives_desync(self):
        async def verify():
            panel = SystemHarness(14)
            self.assertTrue(await panel.button(14, 7))
            self.assertEqual(panel.page, 19)
            self.assertEqual(panel.commands, [])
            self.assertTrue(await panel.button(19, 6))
            self.assertIn('t1.txt="USB log export unavailable"', panel.commands)
            self.assertTrue(await panel.button(19, 5))
            self.assertEqual(panel.page, 14)
            self.assertIn("page 14", panel.commands)

            from_version = SystemHarness(79)
            self.assertTrue(await from_version.button(79, 7))
            self.assertEqual(from_version.page, 19)
            self.assertTrue(await from_version.button(19, 5))
            self.assertEqual(from_version.page, 79)
            self.assertEqual(from_version.commands, ["page 79"])

            # TFT already on Export Diary while the host still thinks it is on System.
            from mksclient_alt.panel import Panel

            class MR:
                ready = False
                generation = 0
                status = {}

            sent = []

            async def send(command):
                sent.append(command)

            panel_ui = Panel(MR(), send)
            panel_ui.sync_page(14)
            await panel_ui.handle(PanelEvent("button", 19, 5))
            self.assertEqual(panel_ui.page, 14)
            self.assertEqual(sent, ["page 14"])

        asyncio.run(verify())

    def test_emergency_stop_and_restart_are_separate_active_page_actions(self):
        async def verify():
            panel = SystemHarness(2)
            panel.job = "printing"
            await panel.button(2, 11)
            self.assertEqual(panel.http_calls, [])
            panel.enable_controls = True
            panel.http_replies = {
                "/printer/emergency_stop": "ok",
                "/printer/info": {"state": "shutdown"},
                "/printer/firmware_restart": "ok",
            }
            await panel.button(2, 11)
            self.assertEqual(panel.page, 68)
            panel.moonraker.ready = False
            self.assertTrue(await panel.handle_system(PanelEvent("number", 68, 0)))
            self.assertFalse(await panel.button(2, 11))
            await panel.button(68, 1)
            self.assertEqual(panel.page, 67)
            self.assertEqual(panel.http_calls, [
                ("POST", "/printer/emergency_stop"),
                ("GET", "/printer/info"),
                ("POST", "/printer/firmware_restart"),
            ])
            panel.moonraker.ready = True
            await panel.finish_system()
            self.assertEqual(panel.page, 67)
            panel.moonraker.generation += 1
            await panel.finish_system()
            self.assertEqual(panel.page, 0)

        asyncio.run(verify())

    def test_restart_timeout_restores_the_retry_dialog_without_reissuing_commands(self):
        async def verify():
            panel = SystemHarness(71, controls=True)
            panel.moonraker.ready = False
            panel.http_replies = {
                "/printer/info": {"state": "error"},
                "/printer/firmware_restart": "ok",
            }
            with patch("mksclient_alt.system_events.monotonic", return_value=10):
                await panel.button(71, 1)
            self.assertEqual(panel.page, 67)
            with patch("mksclient_alt.system_events.monotonic", return_value=10 + RESTART_TIMEOUT_SECONDS):
                await panel.finish_system()
            self.assertEqual(panel.page, 71)
            self.assertIn('t3.txt="Klipper restart unconfirmed"', panel.commands)
            self.assertEqual(len(panel.http_calls), 2)

        asyncio.run(verify())

    def test_host_shutdown_rechecks_klipper_state_on_confirmation(self):
        async def verify():
            panel = SystemHarness(71, controls=True)
            panel.http_replies["/printer/info"] = {"state": "ready"}
            await panel.button(71, 0)
            self.assertEqual(panel.page, 71)
            self.assertEqual(panel.http_calls, [("GET", "/printer/info")])
            panel.http_replies["/printer/info"] = {"state": "shutdown"}
            panel.http_replies["/machine/shutdown"] = "ok"
            await panel.button(71, 0)
            self.assertEqual(panel.page, 65)
            self.assertEqual(panel.http_calls[-2:], [("GET", "/printer/info"), ("POST", "/machine/shutdown")])

        asyncio.run(verify())
