import asyncio
import unittest

from mksclient_alt.operation_events import OperationEvents
from mksclient_alt.protocol import PanelEvent


class OperationPanel(OperationEvents):
    def __init__(self):
        self.init_operation()
        self.page = 3
        self.job = "standby"
        self.enable_controls = True
        self.moonraker = self
        self.ready = True
        self.generation = 1
        self.commands = []
        self.requests = []
        self.gcode_help = {"LOAD_MATERIAL": "Load", "UNLOAD_MATERIAL": "Unload", "Z_TILT_ADJUST": "Tilt",
                           "BED_MESH_CALIBRATE": "Mesh", "PROBE_CALIBRATE": "Probe", "SAVE_CONFIG": "Save"}
        self.status = {
            "toolhead": {"homed_axes": "xyz", "extruder": "extruder", "position": [50, 100, 10, 0],
                         "axis_minimum": [0, 0, -2], "axis_maximum": [252, 252, 252]},
            "extruder": {"target": 0, "temperature": 28, "can_extrude": False},
            "print_stats": {"state": "standby"},
            "virtual_sdcard": {"is_active": False},
            "configfile": {"save_config_pending": False,
                           "settings": {"extruder": {"min_extrude_temp": 170, "max_temp": 300},
                                        "probe": {"x_offset": 24, "y_offset": 5, "z_offset": 2.3},
                                        "bed_mesh": {"horizontal_move_z": 5}}},
            "manual_probe": {"is_active": False},
            "bed_mesh": {"probed_matrix": [[0.11] * 6 for _ in range(6)]},
        }
        self.extrusion_ready = True
        self.hold_tilt = None
        self.hold_material = None

    async def call(self, method, params=None, timeout=8):
        self.requests.append((method, params))
        if method == "printer.gcode.help":
            return self.gcode_help
        if method == "printer.objects.list":
            return {"objects": list(self.status)}
        if method == "printer.objects.query":
            return {"status": {name: self.status[name] for name in params["objects"] if name in self.status}}
        if method == "printer.gcode.script":
            script = params["script"]
            if script.startswith("M109 S"):
                self.status["extruder"].update(target=180, temperature=180, can_extrude=self.extrusion_ready)
            elif script.startswith(("LOAD_MATERIAL", "UNLOAD_MATERIAL")) and self.hold_material:
                await self.hold_material.wait()
            elif script.startswith("G28"):
                self.status["toolhead"]["homed_axes"] = "xyz"
            elif script.endswith("PROBE_CALIBRATE"):
                self.status["manual_probe"]["is_active"] = True
            elif script == "ACCEPT":
                self.status["manual_probe"]["is_active"] = False
                self.status["configfile"]["save_config_pending"] = True
            elif script.startswith("Z_TILT_ADJUST") and self.hold_tilt:
                await self.hold_tilt.wait()
        return "ok"

    async def fresh_status(self, *names):
        return {name: self.status[name] for name in names if name in self.status} if self.enable_controls else {}

    async def action(self, method, params=None):
        if not self.enable_controls or not self.ready:
            return False
        await self.call(method, params)
        return True

    async def gcode(self, script):
        return await self.action("printer.gcode.script", {"script": script})

    async def paint(self, commands):
        self.commands.extend(commands)

    async def go(self, page):
        self.page = page
        await self.render_operation()

    async def press(self, page, action):
        return await self.handle_operation(PanelEvent("button", page, action))

    async def complete(self):
        await self.operation_task
        await self.finish_operation()

    @property
    def scripts(self):
        return [params["script"] for method, params in self.requests if method == "printer.gcode.script"]


class OperationTests(unittest.TestCase):
    def test_filament_routes_local_temperature_and_stale_frames(self):
        async def verify():
            panel = OperationPanel()
            panel.enable_controls = False
            self.assertFalse(await panel.press(4, 8))
            self.assertTrue(await panel.press(3, 21))
            self.assertEqual(panel.page, 4)
            for _ in range(12):
                await panel.press(4, 8)
            self.assertEqual(panel.filament_temperature, 260)
            self.assertIn("n1.val=260", panel.commands)
            await panel.press(4, 7)
            self.assertEqual(panel.filament_temperature, 250)
            await panel.press(4, 5)
            self.assertEqual(panel.page, 4)
            self.assertEqual(panel.requests, [])
            await panel.press(4, 11)
            self.assertEqual(panel.page, 6)
            await panel.press(6, 6)
            self.assertEqual(panel.page, 4)
        asyncio.run(verify())

    def test_material_macros_run_after_heat_and_restore_target(self):
        async def verify():
            for action, page, back, command in (
                (5, 5, 8, "LOAD_MATERIAL"),
                (6, 42, 10, "UNLOAD_MATERIAL"),
            ):
                with self.subTest(command=command):
                    panel = OperationPanel()
                    panel.hold_material = asyncio.Event()
                    await panel.press(3, 21)
                    await panel.press(4, action)
                    self.assertEqual(panel.page, page)
                    self.assertIn('t1.txt="Heating..."', panel.commands)
                    await asyncio.sleep(0)
                    await panel.finish_operation()
                    expected = "Loading..." if action == 5 else "Unloading..."
                    self.assertIn('t2.txt="{}"'.format(expected), panel.commands)
                    self.assertIn("t2.aph=127", panel.commands)
                    panel.hold_material.set()
                    await panel.complete()
                    self.assertEqual(panel.scripts, ["M109 S180", command + "\nM400", "M104 S0"])
                    self.assertIn('t3.txt="Complete"', panel.commands)
                    await panel.press(page, back)
                    self.assertEqual(panel.page, 4)
        asyncio.run(verify())

    def test_unload_refuses_unavailable_extrusion_and_missing_macro(self):
        async def verify():
            panel = OperationPanel()
            panel.extrusion_ready = False
            await panel.go(4)
            del panel.gcode_help["UNLOAD_MATERIAL"]
            await panel.press(4, 6)
            self.assertEqual(panel.scripts, [])
            self.assertEqual(panel.page, 4)
            panel.gcode_help["UNLOAD_MATERIAL"] = "Unload"
            await panel.press(4, 6)
            await panel.complete()
            self.assertEqual(panel.scripts, ["M109 S180", "M104 S0"])
            self.assertIn('t3.txt="Extrusion unavailable"', panel.commands)
        asyncio.run(verify())

    def test_motor_release_and_home_require_idle_and_xy_for_z(self):
        async def verify():
            panel = OperationPanel()
            panel.status["print_stats"]["state"] = "paused"
            await panel.press(3, 18)
            await panel.press(3, 15)
            self.assertEqual(panel.scripts, [])
            panel.status["print_stats"]["state"] = "standby"
            panel.status["toolhead"]["homed_axes"] = "xz"
            await panel.press(3, 9)
            self.assertIsNone(panel.operation_task)
            panel.status["toolhead"]["homed_axes"] = "xyz"
            await panel.press(3, 18)
            await panel.press(3, 9)
            self.assertEqual(panel.page, 51)
            await panel.complete()
            self.assertEqual(panel.page, 3)
            self.assertEqual(panel.scripts, ["M84", "G28 Z\nM400"])
        asyncio.run(verify())

    def test_calibration_and_emergency_stop_use_matching_operation(self):
        async def verify():
            panel = OperationPanel()
            panel.hold_tilt = asyncio.Event()
            await panel.go(33)
            panel.status["toolhead"]["homed_axes"] = ""
            await panel.press(33, 8)
            self.assertEqual(panel.page, 34)
            self.assertIn('t2.txt="Homing..."', panel.commands)
            self.assertIn("vis t2,1", panel.commands)
            await asyncio.sleep(0)
            self.assertIn("G28\nM400", panel.scripts)
            await panel.press(34, 12)
            self.assertIn(("printer.emergency_stop", None), panel.requests)
            panel.hold_tilt.set()
            await panel.complete()
            self.assertTrue(any(script.startswith("Z_TILT_ADJUST") for script in panel.scripts))
            await panel.press(34, 7)
            self.assertEqual(panel.page, 33)

            panel = OperationPanel()
            panel.status["toolhead"]["homed_axes"] = "xy"
            panel.status["z_tilt"] = {"applied": True}
            await panel.go(33)
            await panel.press(33, 10)
            self.assertIn('t3.txt="Home all axes first"', panel.commands)
            self.assertIn("vis t3,1", panel.commands)
        asyncio.run(verify())

    def test_probe_moves_and_saves_only_in_its_own_active_session(self):
        async def verify():
            panel = OperationPanel()
            await panel.go(33)
            self.assertIn('t1.txt="0.11"', panel.commands)
            self.assertIn('t41.txt="0.11"', panel.commands)
            await panel.press(33, 9)
            await panel.complete()
            self.assertEqual(panel.page, 35)
            self.assertEqual(len(panel.scripts), 1)
            self.assertIn("G1 X83.9000 Y-0.2000 F1000", panel.scripts[0])
            self.assertTrue(panel.scripts[0].endswith("PROBE_CALIBRATE"))
            await panel.press(35, 14)
            await panel.press(35, 17)
            self.assertEqual(panel.scripts[-1], "TESTZ Z=-0.02")
            panel.status["toolhead"]["position"][2] = -2
            await panel.press(35, 17)
            self.assertEqual(len(panel.scripts), 2)
            await panel.press(35, 16)
            await panel.complete()
            self.assertEqual(panel.scripts[-2:], ["ACCEPT", "SAVE_CONFIG"])
            await panel.press(35, 16)
            self.assertEqual(len(panel.scripts), 4)
        asyncio.run(verify())

    def test_mesh_requires_z_tilt_and_probe_rejects_out_of_bounds(self):
        async def verify():
            panel = OperationPanel()
            panel.status["z_tilt"] = {"applied": False}
            await panel.go(33)
            await panel.press(33, 10)
            self.assertIn('t3.txt="Run Z tilt first"', panel.commands)
            self.assertEqual(panel.scripts, [])
            panel.status["z_tilt"]["applied"] = True
            await panel.press(36, 10)
            await panel.complete()
            self.assertEqual(panel.scripts, ["BED_MESH_CALIBRATE\nM400", "SAVE_CONFIG"])
            self.assertIn('t3.txt="Saved"', panel.commands)
            self.assertIn('t4.txt="Saved"', panel.commands)
            panel.status["toolhead"]["axis_maximum"][0] = 151
            await panel.press(36, 9)
            self.assertIn('t3.txt="Check probe position"', panel.commands)
            self.assertEqual(panel.scripts, ["BED_MESH_CALIBRATE\nM400", "SAVE_CONFIG"])
        asyncio.run(verify())
