"""LKS settings, support, device information and power dialogs."""

import asyncio
import logging
import math
import re
from time import monotonic

from aiohttp import ClientError

from .moonraker import RpcError
from .protocol import text

LOG = logging.getLogger(__name__)
RESTART_TIMEOUT_SECONDS = 30
SYSTEM, SCREEN_OFF, LANGUAGE, SYSTEM_MORE, ABOUT, SELF_CHECK = range(11, 17)
LOG_EXPORT, RESET, FAQ = 19, 20, 21
CHECKING, TRANSFER = 29, 32
FIRMWARE_DIALOG, DOWNLOADING, INSTALLING = 39, 40, 41
MANUAL, CONTACT = 52, 53
CONFIG_EXPORT, POWER, SHUTTING_DOWN, POWERED_OFF, RESTARTING = range(63, 68)
EMERGENCY, PRINT_ERROR, STOPPING, MACHINE_ERROR, DEVICE_TEST = range(68, 73)
CONFIG_IMPORT, OBICO, VERSION_MENU = 75, 78, 79

SUPPORT_TABS = {5: FAQ, 6: MANUAL, 7: CONTACT}
FOOTER_PAGES = (
    {SYSTEM, LANGUAGE, SYSTEM_MORE, ABOUT, SELF_CHECK, LOG_EXPORT, RESET,
     CONFIG_EXPORT, CONFIG_IMPORT, OBICO, VERSION_MENU}
    | set(SUPPORT_TABS.values())
)
SYSTEM_PAGES = FOOTER_PAGES | {
    SCREEN_OFF, 17, CHECKING, TRANSFER, FIRMWARE_DIALOG, DOWNLOADING, INSTALLING,
    POWER, SHUTTING_DOWN, POWERED_OFF, RESTARTING, EMERGENCY, PRINT_ERROR,
    STOPPING, MACHINE_ERROR, DEVICE_TEST,
}
STOCK_LOCAL_PAGE_CHANGES = {
    (SYSTEM, 7): SCREEN_OFF,
    (SYSTEM, 8): LANGUAGE,
    (SYSTEM_MORE, 7): LOG_EXPORT,
    (VERSION_MENU, 7): LOG_EXPORT,
}
# Destinations the TFT opens itself; Back (and similar exits) need a host page command.
HOST_BACK_PAGES = {
    LOG_EXPORT: "log_export_return",
}
BUTTON_PAGES = {
    (SYSTEM, 6): 18,
    (SYSTEM, 9): SELF_CHECK,
    (SYSTEM, 10): SYSTEM_MORE,
    (SYSTEM, 11): 33,
    (SYSTEM, 14): CONFIG_EXPORT,
    (SCREEN_OFF, 10): SYSTEM,
    (SYSTEM_MORE, 6): 18,
    (SYSTEM_MORE, 8): ABOUT,
    (SYSTEM_MORE, 9): OBICO,
    (SYSTEM_MORE, 10): RESET,
    (SYSTEM_MORE, 11): SYSTEM,
    (SYSTEM_MORE, 13): SYSTEM,
    (ABOUT, 5): SYSTEM_MORE,
    (SELF_CHECK, 7): SYSTEM,
    (RESET, 5): SYSTEM_MORE,
    (FIRMWARE_DIALOG, 0): SYSTEM,
    (CONFIG_EXPORT, 5): SYSTEM,
    (CONFIG_IMPORT, 5): SYSTEM_MORE,
    (OBICO, 5): SYSTEM_MORE,
    (VERSION_MENU, 6): 18,
    (VERSION_MENU, 8): ABOUT,
    (VERSION_MENU, 10): RESET,
    (VERSION_MENU, 11): SYSTEM,
    (VERSION_MENU, 13): 33,
}
UNAVAILABLE_BUTTONS = {
    (SYSTEM, 13): ("b13", "Update unavailable"),
    (SELF_CHECK, 5): ("t3", "Input shaper option unavailable"),
    (SELF_CHECK, 6): ("t3", "Bed mesh option unavailable"),
    (SELF_CHECK, 8): ("t3", "Time-lapse option unavailable"),
    (SELF_CHECK, 9): ("t3", "Self-check unavailable"),
    (LOG_EXPORT, 6): ("t1", "USB log export unavailable"),
    (RESET, 6): ("t1", "Factory reset unavailable"),
    (RESET, 7): ("t1", "Factory reset unavailable"),
    (RESET, 8): ("t1", "Factory reset unavailable"),
    (RESET, 9): ("t1", "Factory reset unavailable"),
    (RESET, 10): ("t1", "Factory reset unavailable"),
    (FIRMWARE_DIALOG, 1): ("t0", "Update unavailable"),
    (CONFIG_EXPORT, 6): ("t1", "USB config export unavailable"),
    (CONFIG_IMPORT, 6): ("t1", "USB restore unavailable"),
    (VERSION_MENU, 9): ("b9", "Update unavailable"),
}
STOCK_NOOP_BUTTONS = {
    OBICO: {6},
    SYSTEM: {5, 12},
    SYSTEM_MORE: {3, 5, 12},
    VERSION_MENU: {3, 5, 12},
    17: set(range(13)),
    CHECKING: {0},
    TRANSFER: {0},
    DOWNLOADING: set(range(7, 12)),
    INSTALLING: set(range(7, 12)),
    POWER: {0, 1},
    PRINT_ERROR: {0, 1},
    STOPPING: {0, 1},
}


class SystemEvents:
    system_restart = None
    log_export_return = SYSTEM_MORE
    obico_poll_at = 0

    async def handle_system(self, event):
        page, action = event.page, event.action
        if page != self.page:
            return False
        if event.kind == "number":
            # Transparent backdrop messages carry no value and performed no
            # operation in the stock client.
            return action == 0 and event.value is None and page in range(POWER, MACHINE_ERROR + 1)
        if event.kind != "button":
            return False

        route = (page, action)
        local_page = STOCK_LOCAL_PAGE_CHANGES.get(route)
        if ((page == SCREEN_OFF and action in range(4))
                or (page == LANGUAGE and (action in range(5, 13) or action == 16))):
            local_page = SYSTEM
        if local_page is not None:
            # The TFT changes page itself, also saving sleep/language to EEPROM.
            # Export Diary has no local Back; remember the source so the host can leave.
            if local_page in HOST_BACK_PAGES:
                setattr(self, HOST_BACK_PAGES[local_page], page)
            self.sync_page(local_page)
            return True

        if page == LOG_EXPORT and action == 5:
            await self.go(self.log_export_return or SYSTEM_MORE)
            return True

        target = BUTTON_PAGES.get(route)
        if page in SUPPORT_TABS.values() and action in SUPPORT_TABS:
            target = SUPPORT_TABS[action]
        if target is not None:
            if target != page:
                await self.go(target)
            return True
        if action in STOCK_NOOP_BUTTONS.get(page, ()):
            return True
        if route in UNAVAILABLE_BUTTONS:
            await self.paint([text(*UNAVAILABLE_BUTTONS[route])])
            return True
        if route in {(0, 10), (2, 11)}:
            await self.emergency_stop()
            return True
        if page in {EMERGENCY, MACHINE_ERROR} and action in (0, 1):
            await self.recover_printer(action)
            return True

        if page == FIRMWARE_DIALOG and action in range(7, 12):
            action -= 7
        elif page not in FOOTER_PAGES or action not in range(5):
            return False
        target = (2 if self.job in {"printing", "paused"} else 0, 3, 7, SYSTEM, FAQ)[action]
        if target == 7:
            await self.open_local_files()
        elif target != page:
            await self.go(target)
        return True

    async def system_http(self, method, endpoint):
        # Klippy shutdown also tears down this client's state websocket.
        # These documented HTTP endpoints remain usable during recovery.
        try:
            async with self.moonraker.session.request(
                method, self.moonraker.base_url + endpoint, timeout=3, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise RpcError("HTTP {}".format(response.status))
                return (await response.json())["result"]
        except (ClientError, KeyError, ValueError, TypeError) as exc:
            raise RpcError("Falha em {}".format(endpoint)) from exc

    async def emergency_stop(self):
        if not self.enable_controls:
            LOG.info("Parada de emergência ignorada em modo de leitura")
            return
        try:
            result = await self.system_http("POST", "/printer/emergency_stop")
            if result == "ok":
                await self.go(EMERGENCY)
        except (RpcError, asyncio.TimeoutError) as exc:
            LOG.warning("Parada de emergência não confirmada: %s", exc)

    async def recover_printer(self, action):
        if not self.enable_controls:
            await self.paint([text("t3", "Read-only mode")])
            return
        generation = self.moonraker.generation
        method, target = (("/machine/shutdown", SHUTTING_DOWN) if action == 0 else
                          ("/printer/firmware_restart", RESTARTING))
        try:
            info = await self.system_http("GET", "/printer/info")
            if not isinstance(info, dict) or info.get("state") not in {"shutdown", "error"}:
                await self.paint([text("t3", "Klipper is no longer stopped")])
                return
            if await self.system_http("POST", method) != "ok":
                raise RpcError("Resposta de confirmação ausente")
            if target == RESTARTING:
                self.system_restart = (generation, monotonic() + RESTART_TIMEOUT_SECONDS)
            await self.go(target)
        except (RpcError, asyncio.TimeoutError) as exc:
            LOG.warning("Operação de energia não confirmada: %s", exc)
            await self.paint([text("t3", "Power command unconfirmed")])

    async def finish_system(self):
        if self.system_restart is None:
            return
        generation, deadline = self.system_restart
        if self.page != RESTARTING:
            self.system_restart = None
        elif self.moonraker.ready and self.moonraker.generation != generation:
            self.system_restart = None
            await self.go(0)
        elif monotonic() >= deadline:
            self.system_restart = None
            await self.go(MACHINE_ERROR)
            await self.paint([text("t3", "Klipper restart unconfirmed")])

    async def render_system(self):
        if self.page == ABOUT:
            await self.render_about()
        elif self.page == OBICO:
            await self.render_obico()
        elif self.page == RESET:
            await self.paint([text("t1", "Factory reset unavailable")])
        elif self.page in {FIRMWARE_DIALOG, DOWNLOADING, INSTALLING}:
            commands = [text("t0", "Update unavailable"), text("t3", "--"), text("t4", "--")]
            if self.page in {DOWNLOADING, INSTALLING}:
                commands.append(text("t8", ""))
            await self.paint(commands)

    async def render_obico(self):
        now = monotonic()
        if now < self.obico_poll_at and "qr0.txt" in self.sent_attributes:
            return
        self.obico_poll_at = now + 5
        macro = "gcode_macro OBICO_LINK_STATUS"
        result = await self.about_query("printer.objects.query", {"objects": {macro: None}}) if self.moonraker.ready else {}
        status = result.get("status", {}).get(macro, {})
        link = status.get("one_time_passlink")
        valid = (isinstance(link, str) and len(link) <= 192 and link.startswith(("https://", "http://"))
                 and all(33 <= ord(char) <= 126 and char not in '"\\' for char in link))
        if status.get("is_linked") is True:
            message, link = "Obico linked", ""
        elif valid:
            message = "Scan the QR code to link"
        else:
            message, link = "Waiting for Obico pairing link" if status else "Obico status unavailable", ""
        await self.paint([
            "vis b6,0", "q0.picc={}".format(131 if link else 132),
            text("qr0", link, 192), "qr0.aph={}".format(127 if link else 0), text("t2", message),
        ])

    async def about_query(self, method, params=None):
        try:
            result = await self.moonraker.call(method, params, timeout=3)
            return result if isinstance(result, dict) else {}
        except (RpcError, asyncio.TimeoutError) as exc:
            LOG.debug("Dados do painel Sobre indisponíveis em %s: %s", method, exc)
            return {}

    async def render_about(self):
        await self.paint([text("t14", "Klipper")])
        if not self.moonraker.ready:
            self.system_about_generation = None
            await self.paint([text(field, "--") for field in ("t9", "t10", "t11", "t12", "t15")])
            return
        if "t9.txt" in self.sent_attributes and getattr(self, "system_about_generation", None) == self.moonraker.generation:
            return
        self.system_about_generation = self.moonraker.generation
        printer, machine, history, files = await asyncio.gather(
            self.about_query("printer.info"),
            self.about_query("machine.system_info"),
            self.about_query("server.history.totals"),
            self.about_query("server.files.get_directory", {"path": "gcodes"}),
        )
        network = (machine.get("system_info") or {}).get("network") or {}
        mac = "--"
        for interface in sorted(network, key=lambda name: (not name.startswith("wl"), name)):
            candidate = (network[interface] or {}).get("mac_address")
            if isinstance(candidate, str) and re.fullmatch(r"(?:[\da-fA-F]{2}:){5}[\da-fA-F]{2}", candidate):
                mac = candidate.upper()
                break
        duration = (history.get("job_totals") or {}).get("total_print_time")
        total = "--"
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
            hours, minutes = divmod(int(duration) // 60, 60)
            days, hours = divmod(hours, 24)
            total = "{:02d}d{:02d}h{:02d}m".format(days, hours, minutes)
        disk = files.get("disk_usage") or {}
        used, size = disk.get("used"), disk.get("total")
        storage = "--"
        if isinstance(used, int) and isinstance(size, int) and 0 <= used <= size:
            storage = "{:.1f}/{:.1f}GB".format(used / 1e9, size / 1e9)
        klipper_version = printer.get("software_version") or "--"
        # The former ISO value is only 77 pixels wide.
        release = re.match(r"^v?\d+\.\d+\.\d+", klipper_version)
        await self.paint([
            text("t9", klipper_version, 32),
            text("t10", mac, 17),
            text("t11", total, 18),
            text("t12", storage, 15),
            text("t15", release[0] if release else klipper_version),
        ])
