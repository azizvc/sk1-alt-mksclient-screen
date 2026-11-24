"""Translate the LKS panel's on-wire actions to Moonraker operations."""

import asyncio
import logging
import math
from pathlib import Path, PurePosixPath
from time import monotonic

from aiohttp import ClientError

from .moonraker import RpcError
from .operation_events import OPERATION_PAGES, OperationEvents
from .print_events import PRINT_PAGES, PrintEvents
from .protocol import text
from .system_events import HOST_BACK_PAGES, SYSTEM_PAGES, SystemEvents
from .wifi import WifiError

LOG = logging.getLogger(__name__)
HOME, KEYPAD, PRINT, MOVE, FANS = 0, 1, 2, 3, 6
FILES, FILE_DETAIL, SYSTEM, SYSTEM_MORE, NETWORK, PRINT_MENU = 7, 9, 11, 14, 18, 27
SUPPORT, WIFI_LEGACY_EDIT, CALIBRATION, WIFI_SEARCH = 21, 31, 33, 62
WIFI_KEYBOARD, WIFI_CONFIRM, WIFI_CONNECTING, WIFI_CONNECTED, WIFI_FAILED = 22, 30, 59, 60, 61
WIFI_DIALOG_PAGES = {WIFI_KEYBOARD, WIFI_CONFIRM, WIFI_LEGACY_EDIT, WIFI_CONNECTING,
                     WIFI_CONNECTED, WIFI_FAILED, WIFI_SEARCH}
WIFI_STATUS_PAGES = {WIFI_CONNECTING, WIFI_CONNECTED, WIFI_FAILED}
WIFI_STATUS_BACKGROUND = 0x20E3  # RGB565 charcoal, matching the Network background.
WIFI_SUCCESS_SECONDS = 2.5
NAV_PAGES = {HOME, PRINT, MOVE, FANS, NETWORK, 4, 5, 33, 34, 35, 36, 37, 42}
PANEL_PAGES = NAV_PAGES | WIFI_DIALOG_PAGES | SYSTEM_PAGES | OPERATION_PAGES | PRINT_PAGES | {KEYPAD}
FAN_OBJECTS = ("fan", "fan_generic Side_fan", "fan_generic Filter_fan")
JOG = {8: ("Z", 1), 10: ("Z", -1), 11: ("Y", 1), 12: ("Y", -1), 13: ("X", -1), 14: ("X", 1)}
ACTIVE = {"printing", "paused"}
# Move/Temp: with b16/b17 at picc 8 the TFT hides n0/n1, so the idle readout is n3/n2.
MOVE_TEMP_FIELDS = (
    # object, current, target, button, current °C, idle °C, dual (x,y), idle (x,y)
    ("extruder", "n0", "n3", "b16", "t0", "t4", (55, 66), (49, 44)),
    ("heater_bed", "n1", "n2", "b17", "t1", "t5", (55, 129), (49, 111)),
)
# Crop-source pictures for the case-light button: off uses the page artwork, on uses
# picture 5 (orange bulb) which lines up with the Home/Print light slot.
LIGHT_ICONS = {
    HOME: ("b5", 2, 5),
    PRINT: ("b6", 4, 5),
}
NETWORK_LANGUAGE_QUERY = "get var32_FLDEBE29"
NETWORK_LABELS = {
    5: dict(connected="已连接", scanning="正在搜索网络…", empty="未找到网络", unavailable="Wi-Fi 不可用"),
    6: dict(connected="Connected", scanning="Scanning for networks...", empty="No networks found",
            unavailable="Wi-Fi unavailable"),
    7: dict(connected="Verbunden", scanning="Netzwerke werden gesucht...", empty="Keine Netzwerke gefunden",
            unavailable="WLAN nicht verfügbar"),
    8: dict(connected="Conectada", scanning="Buscando redes...", empty="No se encontraron redes",
            unavailable="Wi-Fi no disponible"),
    9: dict(connected="Connecté", scanning="Recherche des réseaux...", empty="Aucun réseau trouvé",
            unavailable="Wi-Fi indisponible"),
    10: dict(connected="Connessa", scanning="Ricerca delle reti...", empty="Nessuna rete trovata",
             unavailable="Wi-Fi non disponibile"),
    11: dict(connected="연결됨", scanning="네트워크 검색 중…", empty="네트워크를 찾을 수 없음", unavailable="Wi-Fi 사용 불가"),
    12: dict(connected="接続済み", scanning="ネットワークを検索中…", empty="ネットワークが見つかりません",
             unavailable="Wi-Fi を利用できません"),
}


def percent(value):
    return min(100, max(0, round(float(value) * 100)))


class Panel(SystemEvents, OperationEvents, PrintEvents):
    def __init__(self, moonraker, send, *, enable_controls=False, printer_address="",
                 wifi_scanner=None, wifi_connector=None, enable_thumbnails=False):
        self.moonraker = moonraker
        self.send = send
        self.enable_controls = enable_controls
        self.printer_address = printer_address
        self.network_addresses = None
        self.wifi_icon = 67
        self.wifi_icon_poll_at = 0
        self.wifi_scanner = wifi_scanner
        self.wifi_connector = wifi_connector
        self.enable_thumbnails = enable_thumbnails
        self.wifi_scan = None
        self.wifi_connection = None
        self.wifi_selection = None
        self.wifi_success_until = None
        self.wifi_networks = []
        self.wifi_offset = 0
        self.wifi_status = "unavailable"
        self.network_language = None
        self.language_requested = False
        self.page = None
        self.step = 1
        self.files = []
        self.sent_attributes = {}
        self.resume_action = None
        self.previous_job = None
        self.init_operation()
        self.reset_print_events()

    @property
    def status(self):
        return self.moonraker.status

    @property
    def job(self):
        return self.status.get("print_stats", {}).get("state")

    async def paint(self, commands):
        for command in commands:
            # vis/cls/page-style commands have no '=' and must not be deduped by a stale key.
            if "=" not in command:
                await self.send(command)
                continue
            attr, _, value = command.partition("=")
            if self.sent_attributes.get(attr) != value:
                await self.send(command)
                self.sent_attributes[attr] = value

    async def go(self, page):
        # These LKS pages have only a translucent bitmap over the framebuffer.
        if page in WIFI_STATUS_PAGES:
            await self.send("cls {}".format(WIFI_STATUS_BACKGROUND))
        await self.send("page {}".format(page))
        self.sync_page(page)
        await self.render()

    def sync_page(self, page):
        if self.page != page:
            self.sent_attributes.clear()
            self.print_page_changed(self.page, page)
            self.page = page
            self.wifi_success_until = None
            if page == NETWORK:
                self.network_addresses = None
                self.network_language = None
                self.language_requested = False
                self.start_wifi_scan()
            if page is not None:
                LOG.info("Tela LKS: página %s", page)

    async def render_wifi_icon(self):
        if self.page not in (HOME, PRINT):
            return
        now = monotonic()
        if now >= self.wifi_icon_poll_at:
            self.wifi_icon_poll_at = now + 5
            self.wifi_icon = 67
            try:
                for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
                    interface, _, stats = line.partition(":")
                    if Path("/sys/class/net", interface.strip(), "operstate").read_text().strip() == "up":
                        signal = float(stats.split()[2])
                        self.wifi_icon = 68 + sum(signal >= threshold for threshold in (-88, -77, -55))
                        break
            except (OSError, IndexError, ValueError):
                pass
        await self.paint(["p0.pic={}".format(self.wifi_icon)])

    async def render(self):
        if self.page == NETWORK:
            await self.render_network()
            return
        await self.render_system()
        await self.render_operation()
        await self.render_print()
        if not self.moonraker.ready:
            return
        commands = []
        if self.page == MOVE:
            commands.extend(self.move_temperature_commands())
        for object_name, reading, target in {
            HOME: (("extruder", "n0", "n4"), ("heater_bed", "n1", "n5")),
            PRINT: (("extruder", "n0", "t8"), ("heater_bed", "n1", "t9")),
        }.get(self.page, ()):
            values = self.status.get(object_name)
            if values is None:
                continue
            for field, component in (("temperature", reading), ("target", target)):
                if field in values:
                    value = round(values[field])
                    commands.append(text(component, value) if component.startswith("t") else
                                    "{}.val={}".format(component, value))

        if self.page == PRINT:
            progress = percent(self.status.get("virtual_sdcard", {}).get("progress", 0))
            elapsed = max(0, int(self.status.get("print_stats", {}).get("print_duration", 0)))
            minutes = elapsed // 60
            remaining = round(minutes * (100 - progress) / progress) if progress else 0
            commands.extend([
                "n6.val={}".format(progress),
                "n4.val={}".format(minutes // 60), "n5.val={}".format(minutes % 60),
                "n7.val={}".format(remaining // 60), "n8.val={}".format(remaining % 60),
            ])
        elif self.page == FANS:
            for index, name in enumerate(FAN_OBJECTS):
                if name in self.status and "speed" in self.status[name]:
                    speed = percent(self.status[name]["speed"])
                    commands.extend(("n{}.val={}".format(index, speed), "h{}.val={}".format(index, speed)))
        commands.extend(self.light_icon_commands())
        await self.paint(commands)

    def light_on(self):
        value = self.status.get("output_pin caselight", {}).get("value")
        return isinstance(value, (int, float)) and value > 0.5

    def light_icon_commands(self):
        icon = LIGHT_ICONS.get(self.page)
        if icon is None or "output_pin caselight" not in self.status:
            return []
        component, off_pic, on_pic = icon
        return ["{}.picc={}".format(component, on_pic if self.light_on() else off_pic)]

    def move_temperature_commands(self):
        commands = []
        for object_name, current_c, target_c, button, current_unit, idle_unit, dual_xy, idle_xy in MOVE_TEMP_FIELDS:
            values = self.status.get(object_name)
            if values is None or "temperature" not in values:
                continue
            current = round(values["temperature"])
            target = round(values["target"]) if isinstance(values.get("target"), (int, float)) else 0
            if target > 0:
                # Stock edit layout: current on the left, setpoint beside it.
                commands.extend((
                    "{}.picc=49".format(button),
                    "vis {},1".format(current_c), "vis {},1".format(current_unit),
                    "vis {},0".format(idle_unit),
                    "{}.x={}".format(target_c, dual_xy[0]), "{}.y={}".format(target_c, dual_xy[1]),
                    "{}.val={}".format(current_c, current),
                    "{}.val={}".format(target_c, target),
                ))
            else:
                # Idle layout: only the primary slot is visible; show the reading there.
                commands.extend((
                    "{}.picc=8".format(button),
                    "vis {},0".format(current_c), "vis {},0".format(current_unit),
                    "vis {},1".format(idle_unit),
                    "{}.x={}".format(target_c, idle_xy[0]), "{}.y={}".format(target_c, idle_xy[1]),
                    "{}.val={}".format(target_c, current),
                ))
        return commands

    async def render_network(self):
        if not self.language_requested:
            self.language_requested = True
            await self.send(NETWORK_LANGUAGE_QUERY)
        if self.network_addresses is None:
            try:
                self.network_addresses = await self.moonraker.network_addresses()
            except (ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
                LOG.warning("Rede: consulta ao Moonraker falhou: %s", exc)
                self.network_addresses = []
            LOG.info("Rede LKS: %s", ", ".join("%s %s" % entry for entry in self.network_addresses) or "sem IPv4")

        detected = self.network_addresses[0][1] if self.network_addresses else ""
        commands = [text("t5", "IP: " + (self.printer_address or detected or "--"), 35)]
        visible = self.wifi_networks[self.wifi_offset:self.wifi_offset + 4]
        labels = NETWORK_LABELS.get(self.network_language, {})
        for index in range(4):
            label = ""
            picture = 65535
            if index < len(visible):
                network = visible[index]
                label = network.ssid
                if network.connected:
                    marker = labels.get("connected")
                    label = "{} [{}]".format(label, marker) if marker else "* " + label
                picture = 68 + sum(network.signal >= threshold for threshold in (-80, -70, -55))
            elif index == 0:
                label = labels.get(self.wifi_status, "")
            commands.append(text("b{}".format(7 + index), label, 48))
            commands.append("p{}.pic={}".format(index, picture))
        await self.paint(commands)

    def start_wifi_scan(self):
        if self.wifi_scanner is not None and self.wifi_scan is None:
            self.wifi_offset = 0
            self.wifi_status = "scanning"
            self.wifi_scan = asyncio.create_task(self.wifi_scanner())

    async def finish_wifi_scan(self):
        if self.wifi_scan is None or not self.wifi_scan.done():
            return
        try:
            self.wifi_networks = self.wifi_scan.result()
            self.wifi_status = "empty"
            LOG.info("Wi-Fi LKS: %d redes encontradas", len(self.wifi_networks))
        except WifiError as exc:
            self.wifi_networks = []
            self.wifi_status = "unavailable"
            LOG.warning("Wi-Fi LKS: %s", exc)
        finally:
            self.wifi_scan = None
        self.wifi_offset = min(self.wifi_offset, max(0, (len(self.wifi_networks) - 1) // 4 * 4))
        if self.page == NETWORK:
            await self.render_network()

    async def select_wifi_network(self, action):
        index = self.wifi_offset + action - 7
        if index >= len(self.wifi_networks):
            return
        network = self.wifi_networks[index]
        if network.connected:
            marker = NETWORK_LABELS.get(self.network_language, {}).get("connected")
            label = "{} [{}]".format(network.ssid, marker) if marker else "* " + network.ssid
            await self.go(WIFI_CONNECTED)
            await self.send("fill 0,126,480,30,{}".format(WIFI_STATUS_BACKGROUND))
            await self.paint([text("t0", label, 90)])
            return
        self.wifi_selection = (action, network)
        await self.go(WIFI_CONFIRM)
        await self.paint([text("t0", network.ssid, 90)])

    async def start_wifi_connection(self, password):
        if self.wifi_selection is None or self.wifi_connection is not None or self.wifi_connector is None:
            return
        _, network = self.wifi_selection
        self.wifi_selection = None
        if self.page == WIFI_KEYBOARD:
            await self.send('input.txt=""')
            await self.send('o33_HAODAE1J.txt=""')
        await self.go(WIFI_CONNECTING)
        self.wifi_connection = asyncio.create_task(self.wifi_connector(network, password))

    async def finish_wifi_connection(self):
        if self.wifi_success_until is not None and monotonic() >= self.wifi_success_until:
            await self.go(NETWORK)
            return
        if self.wifi_connection is None or not self.wifi_connection.done():
            return
        page = WIFI_CONNECTED
        try:
            self.wifi_connection.result()
            LOG.info("Wi-Fi LKS: conexao concluida")
        except WifiError as exc:
            LOG.warning("Wi-Fi LKS: %s", exc)
            page = WIFI_FAILED
        finally:
            self.wifi_connection = None
        if self.page == WIFI_CONNECTING:
            await self.go(page)
            if page == WIFI_CONNECTED:
                self.wifi_success_until = monotonic() + WIFI_SUCCESS_SECONDS

    async def wifi_button(self, page, action):
        if page == WIFI_CONFIRM and action == 0 and self.wifi_selection:
            slot, network = self.wifi_selection
            if self.wifi_connector is None or network.security == "unsupported":
                self.wifi_selection = None
                await self.go(WIFI_FAILED)
            elif network.security == "open":
                await self.start_wifi_connection(b"")
            else:
                await self.send("var03_DBCOA05D={}".format(NETWORK))
                await self.send("var04_CGMNA56J={}".format(slot))
                await self.go(WIFI_KEYBOARD)
                await self.send("o35_BAOEAGQX.val=64")
        elif (page, action) in {(WIFI_CONFIRM, 1), (WIFI_KEYBOARD, 0), (WIFI_CONNECTED, 0), (WIFI_FAILED, 0)}:
            self.wifi_selection = None
            await self.go(NETWORK)
        elif (page, action) in {(WIFI_LEGACY_EDIT, 2), (WIFI_SEARCH, 0)}:
            await self.go(NETWORK)
        elif (page, action) == (WIFI_LEGACY_EDIT, 0):
            # Factory b0 (Delete) and b1 (Edit) send the same wire action.
            LOG.debug("Ação Wi-Fi ambígua no TFT LKS: página 31, ação 0")

    async def update(self):
        job = self.job
        previous = self.previous_job
        self.previous_job = job
        if self.page == HOME and job in ACTIVE and previous not in ACTIVE:
            await self.go(PRINT)
        elif self.page == PRINT and previous in ACTIVE and job and job not in ACTIVE:
            await self.finish_print_page(previous)
        else:
            await self.render()

    async def refresh_files(self):
        if not self.moonraker.ready:
            self.files = []
            return
        rows = await self.moonraker.call("server.files.list", {"root": "gcodes"})
        rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get("path"), str)
                and PurePosixPath(row["path"]).suffix.lower() in {".gcode", ".gco", ".g"}]
        rows.sort(key=lambda row: (-float(row.get("modified") or 0), row["path"].casefold()))
        self.files = [row["path"] for row in rows]

    async def fresh_status(self, *names):
        if not self.enable_controls or not self.moonraker.ready:
            return {}
        objects = {name: None for name in names if name in self.status}
        if not objects:
            return {}
        result = await self.moonraker.call("printer.objects.query", {"objects": objects})
        return result["status"] if self.moonraker.ready else {}

    async def action(self, method, params=None):
        if not self.enable_controls or not self.moonraker.ready:
            LOG.info("Ação ignorada em modo de leitura ou sem conexão: %s", method)
            return False
        await self.moonraker.call(method, params)
        return True

    async def gcode(self, script):
        return await self.action("printer.gcode.script", {"script": script})

    async def set_number(self, event):
        if event.value is None:
            return
        if self.page == KEYPAD and self.resume_action == (event.page, event.action):
            source, action = self.resume_action
            self.resume_action = None
            hotend = (source, action) in {(HOME, 7), (PRINT, 9), (MOVE, 16)}
            object_name = "extruder" if hotend else "heater_bed"
            if object_name in self.status and 0 <= event.value <= (260 if hotend else 100):
                try:
                    await self.gcode("{} S{}".format("M104" if hotend else "M140", event.value))
                finally:
                    await self.go(source)
            else:
                LOG.info("Temperatura recusada: %s %s", object_name, event.value)
                await self.go(source)
        elif event.page == self.page == FANS and event.action in (0, 1, 2) and 0 <= event.value <= 100:
            name = FAN_OBJECTS[event.action]
            if name in self.status:
                script = ("M106 S{}".format(round(event.value * 255 / 100)) if event.action == 0 else
                          "SET_FAN_SPEED FAN={} SPEED={:.2f}".format(name.split(" ", 1)[1], event.value / 100))
                await self.gcode(script)

    async def move(self, action):
        if action in (5, 6, 7):
            self.step = (1, 10, 30)[action - 5]
            return
        status = await self.fresh_status("toolhead", "print_stats", "virtual_sdcard")
        tool = status.get("toolhead")
        job = status.get("print_stats", {}).get("state")
        if (not tool or not job or job in ACTIVE
                or status.get("virtual_sdcard", {}).get("is_active")):
            return
        if action in (9, 15):
            await self.gcode("G28 Z" if action == 9 else "G28 X Y")
            return
        axis, sign = JOG[action]
        index = "XYZ".index(axis)
        if axis.lower() not in tool.get("homed_axes", ""):
            return
        try:
            goal = tool["position"][index] + sign * self.step
            in_range = tool["axis_minimum"][index] <= goal <= tool["axis_maximum"][index]
        except (KeyError, IndexError, TypeError):
            return
        if math.isfinite(goal) and in_range:
            await self.gcode(
                "SAVE_GCODE_STATE NAME=MKSCLIENT_ALT_JOG\nG91\nG1 {}{} F{}\nRESTORE_GCODE_STATE NAME=MKSCLIENT_ALT_JOG"
                .format(axis, sign * self.step, 300 if axis == "Z" else 3000)
            )

    async def handle(self, event):
        if event.kind == "boot":
            self.sync_page(None)
            self.resume_action = None
            self.reset_print_events()
            self.wifi_selection = None
        if event.kind in {"boot", "hello"}:
            if self.page is None and self.moonraker.ready:
                await self.go(PRINT if self.job in ACTIVE else HOME)
            return
        if event.kind == "page":
            if event.page in PANEL_PAGES:
                self.sync_page(event.page)
                await self.render()
            return
        if event.kind == "wifi_password":
            if (self.page == WIFI_KEYBOARD and event.page == NETWORK and self.wifi_selection
                    and event.action == self.wifi_selection[0] and event.secret is not None):
                await self.start_wifi_connection(event.secret)
            return
        if event.kind == "number":
            # A standard TJC unsigned 32-bit reply shares opcode 0x71 with
            # LKS numeric events. The language reply is a small value with
            # its other three bytes set to zero and is only expected on Network.
            if (self.page == NETWORK and self.language_requested and self.network_language is None
                    and event.page in NETWORK_LABELS and event.action == 0 and event.value == 0):
                self.network_language = event.page
                await self.render_network()
                return
            if await self.handle_system(event) or await self.handle_print(event):
                return
            await self.set_number(event)
            return
        if event.kind != "button":
            return
        page, action = event.page, event.action
        if page == KEYPAD and self.page == KEYPAD and action in (0, 10, 1) and self.resume_action:
            source, _ = self.resume_action
            self.resume_action = None
            await self.go(source)
            return
        if page != self.page:
            # Export Diary is opened by the TFT itself, but Back only emits UART.
            # If the open frame was missed, adopting the event page unfreezes Exit.
            if page not in HOST_BACK_PAGES:
                return
            LOG.info("Ressincronizando tela LKS: %s → %s", self.page, page)
            self.sync_page(page)
        if page in WIFI_DIALOG_PAGES:
            await self.wifi_button(page, action)
            return
        if await self.handle_system(event) or await self.handle_operation(event) or await self.handle_print(event):
            return
        if (page, action) == (NETWORK, 3):
            return  # The stock handler leaves the current settings tab selected.
        if page in NAV_PAGES and action in range(5):
            target = (PRINT if self.job in ACTIVE else HOME, MOVE, FILES, SYSTEM, SUPPORT)[action]
            if target == FILES:
                await self.open_local_files()
            else:
                await self.go(target)
            return
        if (page, action) in {(HOME, 7), (HOME, 8), (PRINT, 8), (PRINT, 9), (MOVE, 16), (MOVE, 17)}:
            self.resume_action = (page, action)
            if page == HOME:
                # Home runs page keybdB locally after emitting the button frame.
                self.sync_page(KEYPAD)
            else:
                await self.go(KEYPAD)
            return
        if (page, action) in {(HOME, 5), (PRINT, 6)}:
            state = await self.fresh_status("output_pin caselight")
            light = state.get("output_pin caselight", {}).get("value")
            if light is not None:
                new_value = 0 if light else 1
                if await self.gcode("SET_PIN PIN=caselight VALUE={}".format(new_value)):
                    self.status.setdefault("output_pin caselight", {})["value"] = float(new_value)
                    await self.paint(self.light_icon_commands())
        elif (page, action) in {(HOME, 6), (PRINT, 7), (MOVE, 22)}:
            await self.go(FANS)
        elif (page, action) in {(HOME, 9), (PRINT, 10), (SYSTEM, 6), (SYSTEM_MORE, 6)}:
            await self.go(NETWORK)
        elif (page, action) in {(FANS, 5), (MOVE, 20)}:
            await self.go(MOVE)
        elif page == MOVE and action in range(5, 16):
            await self.move(action)
        elif (page, action) == (NETWORK, 5) and self.job not in ACTIVE:
            await self.go(SYSTEM)
        elif (page, action) == (NETWORK, 13) and self.job not in ACTIVE:
            await self.go(CALIBRATION)
        elif page == NETWORK and action in range(7, 11):
            await self.select_wifi_network(action)
        elif page == NETWORK and action in (6, 14):
            self.network_addresses = None
            self.start_wifi_scan()
            await self.render_network()
        elif page == NETWORK and action in (11, 12):
            delta = -4 if action == 11 else 4
            last = max(0, (len(self.wifi_networks) - 1) // 4 * 4)
            self.wifi_offset = min(last, max(0, self.wifi_offset + delta))
            await self.render_network()
        else:
            LOG.debug("Evento LKS sem ação: página %s, botão %s", page, action)

    async def process(self, event):
        try:
            await self.handle(event)
        except (RpcError, asyncio.TimeoutError) as exc:
            LOG.warning("Operação na tela falhou: %s", exc)
