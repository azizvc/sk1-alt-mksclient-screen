import asyncio
import unittest

from mksclient_alt.print_events import (
    PrintEvents, estimated_time_from_filename, parse_gcode_list_stats,
)
from mksclient_alt.protocol import PanelEvent


class StubMoonraker:
    ready = True
    generation = 1

    def __init__(self):
        self.calls = []
        self.status = {
            "print_stats": {"state": "standby", "filename": ""},
            "virtual_sdcard": {"is_active": False, "progress": 0},
        }
        self.files = ["nested/part.gcode", "plain.gcode", "two.gcode", "z.gcode"]

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "server.files.metadata":
            return {"filament_total": 1234, "estimated_time": 3720}
        if method == "server.history.list":
            return {"jobs": [{"filename": "nested/part.gcode", "total_duration": 3720, "print_duration": 3500,
                              "status": "completed"}, {"filename": "plain.gcode", "status": "error"}]}
        return "ok"


class TestPanel(PrintEvents):
    def __init__(self, controls=True):
        self.moonraker = StubMoonraker()
        self.enable_controls = controls
        self.page = 0
        self.files = []
        self.commands = []
        self.reset_print_events()

    @property
    def status(self):
        return self.moonraker.status

    @property
    def job(self):
        return self.status["print_stats"]["state"]

    async def go(self, page):
        self.print_page_changed(self.page, page)
        self.page = page
        await self.render_print()

    async def paint(self, commands):
        self.commands.extend(commands)

    async def refresh_files(self):
        self.files = list(self.moonraker.files) if self.moonraker.ready else []

    async def fresh_status(self, *names):
        if self.enable_controls and self.moonraker.ready:
            return {name: self.status[name] for name in names}
        return {}

    async def action(self, method, params=None):
        if not self.enable_controls or not self.moonraker.ready:
            return False
        await self.moonraker.call(method, params)
        return True

    async def button(self, action, page=None):
        return await self.handle_print(PanelEvent("button", self.page if page is None else page, action))


class PrintEventsTests(unittest.TestCase):
    def test_pagination_discards_queued_thumbnails_for_previous_rows(self):
        async def verify():
            panel = TestPanel()
            panel.enable_thumbnails = True

            async def load(filename, size):
                return {"plain.gcode": "0000", "two.gcode": "1111", "z.gcode": "2222"}[filename]

            async def send(command):
                panel.commands.append(command)

            panel._colpic_for_file = load
            panel.send = send
            await panel.open_local_files()
            await panel._thumb_task
            await panel.finish_thumbnails()
            self.assertEqual(panel.commands[-1], "vis cp1,1")
            await panel.button(12)
            self.assertIn("cp1.close()", panel.commands)
            await panel._thumb_task
            await panel.finish_thumbnails()
            self.assertEqual(panel.commands[-1], "vis cp0,1")
            await panel.finish_thumbnails()
            self.assertEqual(panel.commands[-1], 'cp0.write("2222")')
            panel.status["print_stats"].update(state="printing", filename="plain.gcode")
            await panel.go(2)
            await panel._thumb_task
            await panel.finish_thumbnails()
            await panel.finish_thumbnails()
            self.assertEqual(panel.commands[-1], 'cp0.write("0000")')
            panel.status["print_stats"]["state"] = "complete"
            await panel.finish_print_page("printing")
            self.assertEqual(panel.page, 77)
            await panel._thumb_task
            await panel.finish_thumbnails()
            await panel.finish_thumbnails()
            self.assertEqual(panel.commands[-1], 'cp0.write("0000")')
            await panel.finish_thumbnails()
            self.assertNotIn('cp1.write("0000")', panel.commands)
            panel._file_selection = "z.gcode"
            await panel.go(9)
            await panel._thumb_task
            await panel.finish_thumbnails()
            await panel.finish_thumbnails()
            self.assertEqual(panel.commands[-1], 'cp0.write("2222")')

        asyncio.run(verify())

    def test_gcode_comment_and_filename_stats(self):
        estimated, filament = parse_gcode_list_stats(
            "; filament used [mm] = 623.62\n"
            "; estimated printing time (normal mode) = 26m 43s\n"
        )
        self.assertEqual(estimated, 26 * 60 + 43)
        self.assertEqual(filament, 623.62)
        self.assertEqual(
            estimated_time_from_filename("3DBenchy_0.12mm_PLA_Generic Klipper Printer_26m43s.gcode"),
            26 * 60 + 43,
        )
        self.assertEqual(
            estimated_time_from_filename("100x160_closed_0.28mm_PLA_Generic Klipper Printer_2h53m.gcode"),
            2 * 3600 + 53 * 60,
        )

    def test_local_folders_page_and_open_the_corresponding_card_menu(self):
        async def verify():
            panel = TestPanel()
            await panel.open_local_files()
            self.assertIn('o30_BLMLHY6D.txt="nested/"', panel.commands)
            self.assertIn("vis n0,0", panel.commands)
            await panel.button(12)
            self.assertIn('o30_BLMLHY6D.txt="z.gcode"', panel.commands)
            self.assertIn("n0.val=1", panel.commands)
            self.assertIn("n1.val=2", panel.commands)
            self.assertIn('t2.txt="1.23"', panel.commands)
            self.assertIn("vis n0,1", panel.commands)
            await panel.button(11)
            await panel.button(8)
            self.assertIn('o30_BLMLHY6D.txt="part.gcode"', panel.commands)
            await panel.button(13)
            await panel.button(9)
            self.assertEqual(panel.page, 24)
            await panel.button(2)
            self.assertEqual(panel.page, 7)
            self.assertTrue(any(method == "server.files.metadata" for method, _ in panel.moonraker.calls))

        asyncio.run(verify())

    def test_print_uses_current_status_and_explicit_detail_confirmation(self):
        async def verify():
            panel = TestPanel(controls=False)
            await panel.open_local_files()
            await panel.button(8)
            await panel.button(8)
            await panel.button(0)
            self.assertEqual(panel.page, 9)
            self.assertIn('g0.txt="part.gcode"', panel.commands)
            self.assertIn("n4.val=1", panel.commands)
            self.assertIn("n5.val=2", panel.commands)
            self.assertIn('t2.txt="1.23"', panel.commands)
            await panel.button(5)
            self.assertEqual(panel.page, 16)
            await panel.go(9)
            await panel.button(7)
            panel.enable_controls = True
            panel.status["virtual_sdcard"]["is_active"] = True
            panel.moonraker.calls.clear()
            await panel.button(7)
            self.assertEqual(panel.moonraker.calls, [])
            panel.status["virtual_sdcard"]["is_active"] = False
            await panel.button(7)
            self.assertEqual(panel.moonraker.calls[-1], ("printer.print.start", {"filename": "nested/part.gcode"}))
            self.assertEqual(panel.page, 2)

        asyncio.run(verify())

    def test_delete_is_bound_to_a_focused_confirmation_and_connection(self):
        async def verify():
            panel = TestPanel()
            await panel.open_local_files()
            await panel.button(8)
            await panel.button(8)
            await panel.button(1)
            self.assertEqual(panel.page, 8)
            self.assertIn('t0.txt="part.gcode"', panel.commands)
            panel.moonraker.calls.clear()
            await panel.button(0)
            self.assertEqual(panel.moonraker.calls, [])
            await panel.button(8)
            await panel.button(1)
            panel.moonraker.generation += 1
            panel.moonraker.calls.clear()
            await panel.button(1)
            self.assertEqual(panel.moonraker.calls, [])
            await panel.button(8)
            await panel.button(1)
            panel.moonraker.calls.clear()
            await panel.button(1, 24)
            self.assertEqual(panel.moonraker.calls, [])
            await panel.button(1)
            await panel.button(1, 8)
            self.assertIn(("server.files.delete_file", {"path": "gcodes/nested/part.gcode"}), panel.moonraker.calls)

        asyncio.run(verify())

    def test_print_menu_never_reinterprets_pause_as_resume(self):
        async def verify():
            panel = TestPanel()
            stats = panel.status["print_stats"]
            stats.update(filename="plain.gcode", state="printing")
            await panel.go(2)
            await panel.button(5)
            self.assertEqual(panel.page, 27)
            stats["state"] = "paused"
            await panel.button(0)
            self.assertEqual(panel.moonraker.calls, [])
            await panel.button(5)
            self.assertEqual(panel.page, 74)
            await panel.button(0)
            self.assertEqual(panel.moonraker.calls, [("printer.print.resume", None)])
            await panel.button(5)
            action = panel.action

            async def cancel_from_stopping(method, params=None):
                self.assertEqual(panel.page, 73)
                return await action(method, params)

            panel.action = cancel_from_stopping
            await panel.button(1)
            self.assertEqual(panel.moonraker.calls[-1], ("printer.gcode.script", {"script": "CANCEL_PRINT"}))
            self.assertEqual(panel.page, 73)
            await panel.render_print()
            self.assertEqual(panel.page, 73)
            self.assertTrue(await panel.handle_print(PanelEvent("number", 73, 0)))
            stats["state"] = "cancelled"
            await panel.render_print()
            self.assertEqual(panel.page, 0)

        asyncio.run(verify())

    def test_history_and_unavailable_usb_provide_local_navigation(self):
        async def verify():
            panel = TestPanel()
            await panel.open_local_files()
            await panel.button(7)
            self.assertEqual(panel.page, 10)
            self.assertIn('t4.txt="part.gcode"', panel.commands)
            self.assertIn('t6.txt="1h02m"', panel.commands)
            self.assertIn("q0.picc=24", panel.commands)
            self.assertIn("q1.picc=74", panel.commands)
            await panel.button(6)
            self.assertEqual(panel.page, 44)
            self.assertIn('t0.txt="USB indisponivel"', panel.commands)
            await panel.button(0)
            self.assertEqual(panel.page, 10)
            self.assertIn(("server.history.list", {"limit": 5, "order": "desc"}), panel.moonraker.calls)

        asyncio.run(verify())

    def test_completed_print_summary_reprints_the_confirmed_filename(self):
        async def verify():
            panel = TestPanel()
            stats = panel.status["print_stats"]
            stats.update(state="printing", filename="nested/part.gcode", print_duration=3500)
            await panel.go(2)
            stats["state"] = "complete"
            await panel.finish_print_page("printing")
            self.assertEqual(panel.page, 77)
            self.assertIn('g0.txt="part.gcode"', panel.commands)
            self.assertIn('t4.txt="1h02m"', panel.commands)
            await panel.button(5)
            self.assertEqual(panel.moonraker.calls[-1], ("printer.print.start", {"filename": "nested/part.gcode"}))
            self.assertEqual(panel.page, 2)

        asyncio.run(verify())

    def test_reprint_is_blocked_when_job_file_or_connection_changes(self):
        async def verify():
            panel = TestPanel()
            stats = panel.status["print_stats"]
            stats.update(state="printing", filename="nested/part.gcode")
            await panel.go(2)
            stats["state"] = "complete"
            await panel.finish_print_page("printing")
            stats["filename"] = "plain.gcode"
            await panel.button(5)
            panel.moonraker.generation += 1
            stats["filename"] = "nested/part.gcode"
            await panel.button(5)
            self.assertEqual([method for method, _ in panel.moonraker.calls],
                             ["server.history.list", "server.files.metadata"])

        asyncio.run(verify())


if __name__ == "__main__":
    unittest.main()
