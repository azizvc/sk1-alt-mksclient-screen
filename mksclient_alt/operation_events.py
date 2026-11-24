"""LKS filament, movement and calibration actions backed by Klipper."""

import asyncio
import logging
import math
from time import monotonic

from aiohttp import ClientError

from .moonraker import RpcError
from .protocol import text

LOG = logging.getLogger(__name__)

MOVE, FILAMENT, LOAD, FANS, UNLOAD = 3, 4, 5, 6, 42
CALIBRATION, TILT, PROBE, MESH, SHAPER = 33, 34, 35, 36, 37
STARTING, HOMING_FAILED, HOMING, UPDATE_SUCCESS = 45, 47, 51, 57
OPERATION_PAGES = {3, 4, 5, 6, 33, 34, 35, 36, 37, 42, 45, 46, 47, 48, 49, 50,
                   51, 55, 56, 57, 58}
FILAMENT_PROGRESS = {LOAD, UNLOAD}
CALIBRATION_PROGRESS = {TILT, PROBE, MESH, SHAPER}
STOP_BUTTONS = {TILT: 12, PROBE: 19, MESH: 13, SHAPER: 13, HOMING: 10}
IDLE_JOBS = {"standby", "complete", "cancelled", "error"}
FILAMENT_COMMANDS = {LOAD: "LOAD_MATERIAL", UNLOAD: "UNLOAD_MATERIAL"}
CALIBRATION_COMMANDS = {TILT: "Z_TILT_ADJUST", MESH: "BED_MESH_CALIBRATE", SHAPER: "SHAPER_CALIBRATE"}
PROBE_POINT = (133.9, 99.8)  # Stock LKS probe calibration location.


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


class OperationEvents:
    def init_operation(self):
        self.filament_temperature = 180
        self.probe_step = 0.02
        self.operation_task = None
        self.operation_page = None
        self.operation_generation = None
        self.operation_message = None
        self.homing_notice_until = None
        self.probe_generation = None
        self.calibration_summary = None

    async def operation_idle(self, *objects):
        state = await self.fresh_status("print_stats", "virtual_sdcard", "pause_resume", *objects)
        if (state.get("print_stats", {}).get("state") not in IDLE_JOBS
                or state.get("virtual_sdcard", {}).get("is_active") is not False
                or state.get("pause_resume", {}).get("is_paused") is True):
            return {}
        return state

    async def operation_query(self, objects):
        if not self.moonraker.ready:
            return {}
        result = await self.moonraker.call("printer.objects.query", {"objects": objects})
        return result.get("status", {}) if self.moonraker.ready and isinstance(result, dict) else {}

    async def operation_has_gcode(self, command):
        if not self.enable_controls or not self.moonraker.ready:
            return False
        commands = await self.moonraker.call("printer.gcode.help")
        return isinstance(commands, dict) and command in commands

    async def long_operation_gcode(self, script):
        if not self.enable_controls or not self.moonraker.ready:
            return False
        if self.operation_generation != self.moonraker.generation:
            return False
        await self.moonraker.call("printer.gcode.script", {"script": script}, timeout=600)
        return True

    async def start_operation(self, page, run, message):
        self.operation_page = page
        self.operation_generation = self.moonraker.generation
        self.operation_message = message
        self.operation_task = asyncio.create_task(run)
        await self.go(page)

    async def finish_operation(self):
        if self.page == HOMING_FAILED and self.homing_notice_until is not None:
            if monotonic() >= self.homing_notice_until:
                self.homing_notice_until = None
                await self.go(MOVE)
        if self.operation_task is None:
            return
        if not self.operation_task.done():
            if self.page == self.operation_page and self.page in FILAMENT_PROGRESS:
                await self.render_operation()
            return
        try:
            self.operation_message = self.operation_task.result()
        except (RpcError, ClientError, OSError, asyncio.TimeoutError) as exc:
            LOG.warning("Operação LKS na página %s falhou: %s", self.operation_page, exc)
            self.operation_message = "Check printer" if isinstance(exc, asyncio.TimeoutError) else "Operation failed"
        finally:
            self.operation_task = None
        if self.page != self.operation_page:
            return
        if self.page == HOMING:
            if self.operation_message == "Complete":
                await self.go(MOVE)
            else:
                self.homing_notice_until = monotonic() + 2
                await self.go(HOMING_FAILED)
        else:
            await self.render_operation()

    async def stop_operation(self):
        running = (self.operation_task is not None and not self.operation_task.done()
                   and self.operation_page == self.page
                   and self.operation_generation == self.moonraker.generation)
        if running or (self.page == PROBE and await self.active_probe()):
            await self.action("printer.emergency_stop")
            self.probe_generation = None

    async def start_homing(self, action):
        state = await self.operation_idle("toolhead")
        tool = state.get("toolhead", {})
        if not tool or (action == 9 and not set("xy") <= set(tool.get("homed_axes", ""))):
            return
        axes = "z" if action == 9 else "xy"
        script = "G28 Z" if action == 9 else "G28 X Y"
        await self.start_operation(HOMING, self.run_homing(script, axes), "Homing...")

    async def run_homing(self, script, axes):
        if await self.long_operation_gcode(script + "\nM400"):
            state = await self.fresh_status("toolhead")
            if set(axes) <= set(state.get("toolhead", {}).get("homed_axes", "")):
                return "Complete"
        return "Operation failed"

    async def start_filament(self, page):
        command = FILAMENT_COMMANDS[page]
        if not await self.operation_has_gcode(command):
            if self.enable_controls and self.moonraker.ready:
                await self.paint([text("t2", "Filament macro unavailable", 60)])
            return
        config = await self.operation_query({"configfile": ["settings"]})
        settings = config.get("configfile", {}).get("settings", {}).get("extruder", {})
        minimum, maximum = settings.get("min_extrude_temp"), settings.get("max_temp")
        target = self.filament_temperature
        if not (finite_number(minimum) and finite_number(maximum) and minimum <= target < maximum):
            await self.paint([text("t2", "Check nozzle temperature", 60)])
            return
        state = await self.operation_idle("extruder", "toolhead")
        extruder = state.get("extruder", {})
        if (state.get("toolhead", {}).get("extruder") != "extruder"
                or not finite_number(extruder.get("target")) or not finite_number(extruder.get("temperature"))):
            return
        await self.start_operation(page, self.run_filament(command, target, extruder["target"]), "Heating...")

    async def run_filament(self, command, target, previous_target):
        try:
            if not await self.long_operation_gcode("M109 S{}".format(target)):
                return "Operation failed"
            state = await self.operation_idle("extruder", "toolhead")
            extruder = state.get("extruder", {})
            if (state.get("toolhead", {}).get("extruder") != "extruder"
                    or extruder.get("target") != target or extruder.get("can_extrude") is not True):
                return "Extrusion unavailable"
            self.operation_message = "Loading..." if command == FILAMENT_COMMANDS[LOAD] else "Unloading..."
            if await self.long_operation_gcode(command + "\nM400"):
                return "Complete"
            return "Operation failed"
        finally:
            # Preserve an independently changed target and avoid writes after reconnection.
            if (previous_target != target and self.operation_generation == self.moonraker.generation):
                state = await self.operation_idle("extruder", "toolhead")
                if (state.get("toolhead", {}).get("extruder") == "extruder"
                        and state.get("extruder", {}).get("target") == target):
                    await self.gcode("M104 S{}".format(previous_target))

    async def start_calibration(self, page):
        self.operation_page = page
        self.operation_message = "Unavailable"
        if page == PROBE:
            await self.start_probe()
            return
        command = CALIBRATION_COMMANDS.get(page)
        if command and await self.operation_has_gcode(command):
            if page == MESH and await self.operation_has_gcode("Z_TILT_ADJUST"):
                status = await self.operation_query({"z_tilt": ["applied"]})
                if status.get("z_tilt", {}).get("applied") is not True:
                    self.operation_message = "Run Z tilt first"
                    await self.go(page)
                    return
            state = await self.operation_idle("toolhead")
            axes = state.get("toolhead", {}).get("homed_axes", "")
            if set("xyz") <= set(axes):
                await self.start_operation(page, self.run_calibration(command), "Calibrating...")
                return
            if state and page == TILT:
                # Stock Z_Tilt UI includes a Homing step; M84 clears homed_axes.
                await self.start_operation(page, self.run_tilt_with_home(), "Homing...")
                return
            if state:
                self.operation_message = "Home all axes first"
        await self.go(page)

    async def run_tilt_with_home(self):
        if not await self.long_operation_gcode("G28\nM400"):
            return "Operation failed"
        state = await self.fresh_status("toolhead")
        if not set("xyz") <= set(state.get("toolhead", {}).get("homed_axes", "")):
            return "Home all axes first"
        self.operation_message = "Calibrating..."
        if self.page == TILT:
            await self.render_operation()
        return await self.run_calibration("Z_TILT_ADJUST")

    async def run_calibration(self, command):
        if not await self.long_operation_gcode(command + "\nM400"):
            return "Operation failed"
        self.calibration_summary = None
        if command == "BED_MESH_CALIBRATE" and await self.operation_has_gcode("SAVE_CONFIG"):
            self.operation_message = "Saving..."
            if self.page == MESH:
                await self.render_operation()
            try:
                if await self.long_operation_gcode("SAVE_CONFIG"):
                    return "Saved"
            except RpcError:
                # SAVE_CONFIG restarts Klipper; the RPC often drops after the write succeeds.
                if not self.moonraker.ready:
                    return "Saved"
                raise
            return "Operation failed"
        return "Complete"

    async def start_probe(self):
        if await self.operation_has_gcode("PROBE_CALIBRATE") and await self.operation_has_gcode("SAVE_CONFIG"):
            state = await self.operation_idle("toolhead")
            tool = state.get("toolhead", {})
            self.operation_message = "Home all axes first"
            if set("xyz") <= set(tool.get("homed_axes", "")):
                self.operation_message = "Check probe position"
                objects = await self.operation_query({"manual_probe": ["is_active"],
                                                      "configfile": ["settings", "save_config_pending"]})
                config = objects.get("configfile", {})
                settings = config.get("settings", {})
                probe = settings.get("probe") or settings.get("bltouch") or {}
                clearance = settings.get("bed_mesh", {}).get("horizontal_move_z")
                if (objects.get("manual_probe", {}).get("is_active") is False
                        and config.get("save_config_pending") is False
                        and finite_number(clearance) and self.probe_point_in_range(tool, probe, clearance)):
                    x, y, z = tool["position"][:3]
                    script = (
                        "SAVE_GCODE_STATE NAME=MKSCLIENT_ALT_PROBE\nG91\n"
                        "G1 Z{:.4f} F300\nG1 X{:.4f} Y{:.4f} F1000\n"
                        "RESTORE_GCODE_STATE NAME=MKSCLIENT_ALT_PROBE\nPROBE_CALIBRATE"
                    ).format(max(0, clearance - z), PROBE_POINT[0] - x, PROBE_POINT[1] - y)
                    await self.start_operation(PROBE, self.run_probe(script), "Starting probe...")
                    return
        await self.go(PROBE)

    @staticmethod
    def probe_point_in_range(tool, probe, clearance):
        try:
            position = tool["position"][:3]
            offset = [probe["x_offset"], probe["y_offset"]]
            minimum, maximum = tool["axis_minimum"][:3], tool["axis_maximum"][:3]
            for point in (position, (*PROBE_POINT, clearance),
                          (PROBE_POINT[0] + offset[0], PROBE_POINT[1] + offset[1], clearance)):
                if len(point) != 3 or len(minimum) != 3 or len(maximum) != 3:
                    return False
                if not all(finite_number(v) and lo <= v <= hi for v, lo, hi in zip(point, minimum, maximum)):
                    return False
        except (KeyError, TypeError):
            return False
        return clearance > 0

    async def run_probe(self, script):
        if await self.long_operation_gcode(script):
            state = await self.operation_query({"manual_probe": ["is_active"]})
            if state.get("manual_probe", {}).get("is_active") is True:
                self.probe_generation = self.operation_generation
                return "Paper test"
        return "Probe unavailable"

    async def active_probe(self):
        if not self.enable_controls or self.probe_generation != self.moonraker.generation:
            return False
        status = await self.operation_query({"manual_probe": ["is_active"]})
        if status.get("manual_probe", {}).get("is_active") is True:
            return True
        self.probe_generation = None
        return False

    async def probe_button(self, action):
        if not await self.active_probe():
            return
        state = await self.operation_idle("toolhead")
        tool = state.get("toolhead", {})
        if not set("xyz") <= set(tool.get("homed_axes", "")):
            return
        if action == 16:
            config = await self.operation_query({"configfile": ["save_config_pending"]})
            if config.get("configfile", {}).get("save_config_pending") is not False:
                return
            await self.start_operation(PROBE, self.accept_probe(), "Saving...")
            return
        delta = self.probe_step if action == 15 else -self.probe_step
        try:
            goal = tool["position"][2] + delta
            if (finite_number(goal) and tool["axis_minimum"][2] <= goal
                    and goal + 0.5 <= tool["axis_maximum"][2]):
                await self.gcode("TESTZ Z={:.2f}".format(delta))
        except (KeyError, TypeError, IndexError):
            return

    async def accept_probe(self):
        if not await self.long_operation_gcode("ACCEPT"):
            return "Operation failed"
        self.probe_generation = None
        config = await self.operation_query({"configfile": ["save_config_pending"]})
        if config.get("configfile", {}).get("save_config_pending") is not True:
            return "Probe not accepted"
        try:
            if await self.long_operation_gcode("SAVE_CONFIG"):
                return "Complete"
        except RpcError:
            if not self.moonraker.ready:
                return "Check saved offset"
            raise
        return "Operation failed"

    async def calibration_readings(self):
        if self.calibration_summary is None or self.calibration_summary[0] != self.moonraker.generation:
            try:
                available = await self.moonraker.call("printer.objects.list")
                objects = {"configfile": ["settings"]}
                if isinstance(available, dict) and "bed_mesh" in available.get("objects", ()):
                    objects["bed_mesh"] = ["probed_matrix"]
                self.calibration_summary = (self.moonraker.generation, await self.operation_query(objects))
            except (RpcError, asyncio.TimeoutError, ClientError, OSError) as exc:
                LOG.warning("Leituras de calibração LKS indisponíveis: %s", exc)
                return []
        status = self.calibration_summary[1]
        settings = status.get("configfile", {}).get("settings", {})
        matrix = status.get("bed_mesh", {}).get("probed_matrix") or []
        if not isinstance(matrix, list) or not all(isinstance(row, list) for row in matrix):
            matrix = []
        values = [value for row in matrix for value in row if finite_number(value)]
        commands = []
        if self.page == CALIBRATION:
            probe = settings.get("probe") or settings.get("bltouch") or {}
            shaper = settings.get("input_shaper", {})
            measurements = {
                "t0": settings.get("z_tilt", {}).get("retry_tolerance"),
                "t1": max(values) if values else None,
                "t2": min(values) if values else None,
                "t3": probe.get("z_offset"),
                "t4": shaper.get("shaper_freq_x"),
                "t5": shaper.get("shaper_freq_y"),
            }
            commands = [text(name, "{:.2f}".format(value) if finite_number(value) else "--", 8)
                        for name, value in measurements.items()]
        if len(matrix) == 6 and all(len(row) == 6 for row in matrix):
            commands.extend(text("t{}".format(6 + index), "{:.2f}".format(value), 6)
                            for index, value in enumerate(value for row in matrix for value in row)
                            if finite_number(value))
        return commands

    async def render_operation(self):
        page = self.page
        commands = []
        if page == FILAMENT:
            commands.append("n1.val={}".format(self.filament_temperature))
        elif page == CALIBRATION and self.moonraker.ready:
            commands = await self.calibration_readings()
        elif page in FILAMENT_PROGRESS:
            reading = self.status.get("extruder", {}).get("temperature")
            if finite_number(reading):
                commands.append("n0.val={}".format(round(reading)))
            commands.append("n{}.val={}".format(1 if page == LOAD else 2, self.filament_temperature))
            message = self.operation_message if self.operation_page == page else ""
            status_field = {
                "Heating...": "t1",
                "Loading...": "t2",
                "Unloading...": "t2",
            }.get(message, "t3")
            for field in ("t1", "t2", "t3"):
                value = message if field == status_field and message else ""
                commands.extend((text(field, value, 30), "{}.aph={}".format(field, 127 if value else 0)))
            commands.extend("{}.aph={}".format(component, 127 if self.operation_task else 0)
                            for component in ("q3", "t6" if page == LOAD else "t4"))
        elif page in CALIBRATION_PROGRESS:
            # Stock page load hides t2–t5; progress/errors must be revealed explicitly.
            message = self.operation_message if self.operation_page == page else ""
            commands.extend((
                text("t1", ""), text("t2", ""), text("t3", ""), text("t4", ""),
                "vis t2,0", "vis t3,0", "vis t4,0", "vis t5,0",
            ))
            if message == "Homing...":
                commands.extend(("vis t2,1", text("t2", message, 50)))
            elif message:
                commands.extend(("vis t3,1", text("t3", message, 50)))
            if message == "Complete" and page == SHAPER:
                commands.extend(("vis t4,1", text("t4", "Save via web UI", 30)))
            if message == "Saved" and page == MESH:
                commands.extend(("vis t4,1", text("t4", "Saved", 30)))
            if message in {"Complete", "Saved"} and page == MESH:
                commands.extend(await self.calibration_readings())
            component = "b{}".format(STOP_BUTTONS[page])
            active_probe = page == PROBE and self.probe_generation == self.moonraker.generation
            commands.append("{}.aph={}".format(component, 127 if self.operation_task or active_probe else 0))
            if page == PROBE:
                for index, step in enumerate((0.2, 0.1, 0.02)):
                    color = 0xFCC0 if self.probe_step == step else 0xFFFF
                    commands.append("b{}.pco={}".format(12 + index, color))
        elif page == HOMING:
            commands.append("b0.aph={}".format(127 if self.operation_task else 0))
        if commands:
            await self.paint(commands)

    async def handle_operation(self, event):
        if event.kind != "button" or event.page != self.page or event.page not in OPERATION_PAGES:
            return False
        page, action = event.page, event.action
        if page in STOP_BUTTONS and action == STOP_BUTTONS[page]:
            await self.stop_operation()
            return True
        if self.operation_task is not None:
            return True
        if page == PROBE and action not in range(12, 18) and await self.active_probe():
            return True
        if (page, action) == (STARTING, 0):
            if self.moonraker.ready:
                await self.go(2 if self.job in {"printing", "paused"} else 0)
        elif (page, action) == (MOVE, 21) or (page, action) == (FANS, 6):
            await self.go(FILAMENT)
        elif (page, action) in {(MOVE, 9), (MOVE, 15)}:
            await self.start_homing(action)
        elif (page, action) == (MOVE, 18):
            state = await self.operation_idle("toolhead")
            if state.get("toolhead"):
                await self.gcode("M84")
        elif (page, action) == (FANS, 7) or (page, action) == (FILAMENT, 10):
            return True  # Selected tab.
        elif page == FILAMENT and action in (7, 8):
            self.filament_temperature = min(260, max(180, self.filament_temperature + (-10 if action == 7 else 10)))
            await self.render_operation()
        elif page == FILAMENT and action in (5, 6):
            await self.start_filament(LOAD if action == 5 else UNLOAD)
        elif (page, action) in {(FILAMENT, 9), (LOAD, 7), (UNLOAD, 9)}:
            await self.go(MOVE)
        elif (page, action) in {(FILAMENT, 11), (LOAD, 9), (UNLOAD, 11)}:
            await self.go(FANS)
        elif page in FILAMENT_PROGRESS and action == (8 if page == LOAD else 10):
            await self.go(FILAMENT)
        elif page in CALIBRATION_PROGRESS | {CALIBRATION} and action in (3, 7):
            if page != CALIBRATION:
                await self.go(CALIBRATION)
        elif page in CALIBRATION_PROGRESS | {CALIBRATION} and action in (5, 6):
            await self.go(11 if action == 5 else 18)
        elif page in CALIBRATION_PROGRESS | {CALIBRATION} and action in range(8, 12):
            await self.start_calibration((TILT, PROBE, MESH, SHAPER)[action - 8])
        elif page == PROBE and action in (12, 13, 14):
            self.probe_step = (0.20, 0.10, 0.02)[action - 12]
            await self.render_operation()
        elif page == PROBE and action in (15, 16, 17):
            await self.probe_button(action)
        elif page in FILAMENT_PROGRESS and action in (5, 6):
            return True  # Progress page buttons have no operation in the stock client.
        elif (page, action) == (UPDATE_SUCCESS, 0):
            LOG.info("Reinicialização pós-atualização requer o atualizador original TwoTrees")
        elif (page, action) == (HOMING, 0):
            return True  # Backdrop touch.
        else:
            return False
        return True
