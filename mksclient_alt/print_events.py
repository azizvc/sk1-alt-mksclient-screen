"""Eventos LKS de arquivos, histórico e impressão."""

import asyncio
from collections import deque
from datetime import datetime
import logging
import math
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from aiohttp import ClientError

from .moonraker import RpcError
from .protocol import text
from .thumbnails import DETAIL_PREVIEW_SIZE, FILE_PREVIEW_SIZE, extract_thumbnail, image_to_colpic, preview_commands

LOG = logging.getLogger(__name__)

HOME, PRINT, MOVE, FILES, FILE_DELETE, FILE_DETAIL, HISTORY = 0, 2, 3, 7, 8, 9, 10
SYSTEM, PRINT_SETTINGS, SUPPORT = 11, 16, 21
FILE_MENUS = (23, 24, 25)
EMERGENCY, PAUSE_MENU, FILE_MENU = 26, 27, 28
USB_FILES, USB_NOTICE, USB_FILES_ALT = 43, 44, 54
STOPPING, RESUME_MENU, FILAMENT_NOTICE, HISTORY_DETAIL = 73, 74, 76, 77
PRINT_PAGES = frozenset({
    FILES, FILE_DELETE, FILE_DETAIL, HISTORY, *FILE_MENUS, EMERGENCY, PAUSE_MENU,
    FILE_MENU, USB_FILES, USB_NOTICE, USB_FILES_ALT, STOPPING, RESUME_MENU,
    FILAMENT_NOTICE, HISTORY_DETAIL,
})
ACTIVE_STATES = {"printing", "paused"}
IDLE_STATES = {"standby", "complete", "cancelled", "error"}
LABELS = ("o30_BLMLHY6D", "o31_BLMLHY6D", "o32_ICHIAFVC")
THUMBNAIL_PAGES = {FILES, FILE_DETAIL, PRINT, HISTORY_DETAIL}
THUMB_SCAN_BYTES = 256 * 1024
# Page 7 file cards: time (Hh Mm) + filament meters, matching stock layout.
FILE_ROW_DETAILS = (
    ("n0", "t0", "n1", "t1", "t2", "t3"),
    ("n2", "t4", "n3", "t5", "t6", "t7"),
    ("n4", "t8", "n5", "t9", "t10", "t11"),
)
# Orca/Prusa put these near the end of the file; Moonraker 0.7 on SK1 often omits them.
GCODE_TIME_RE = re.compile(
    r";\s*estimated printing time[^=]*=\s*(?:(\d+)\s*d\s*)?(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?(?:(\d+)\s*s\s*)?",
    re.I,
)
GCODE_FILAMENT_RE = re.compile(r";\s*filament used \[mm\]\s*=\s*([0-9.]+)", re.I)
FILENAME_TIME_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)(?:(\d+)s)?\.gcode$", re.I)


def relative_path(value):
    if isinstance(value, str) and not any(c in value for c in "\\\r\n\0"):
        parts = value.split("/")
        if all(part and part not in {".", ".."} for part in parts):
            return PurePosixPath(value)
    return None


def nonnegative_number(value):
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return value
    return None


def parse_gcode_list_stats(text):
    """Extract list-card stats from Orca/Prusa comment blocks."""
    estimated = filament = None
    match = GCODE_TIME_RE.search(text)
    if match:
        days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
        estimated = days * 86400 + hours * 3600 + minutes * 60 + seconds
    match = GCODE_FILAMENT_RE.search(text)
    if match:
        try:
            filament = float(match.group(1))
        except ValueError:
            filament = None
    return estimated, filament


def estimated_time_from_filename(filename):
    match = FILENAME_TIME_RE.search(PurePosixPath(filename).name)
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total or None


def job_duration(value):
    seconds = nonnegative_number(value)
    if seconds is None:
        return ""
    minutes = int(seconds) // 60
    return "{}h{:02}m".format(minutes // 60, minutes % 60)


def job_timestamp(value, date_format):
    timestamp = nonnegative_number(value)
    try:
        return datetime.fromtimestamp(timestamp).strftime(date_format) if timestamp else ""
    except (ValueError, OSError, OverflowError):
        return ""


class PrintEvents:
    _file_directory = ()
    _file_offset = 0
    _file_metadata = None
    _file_meta_cache = None
    _file_thumb_cache = None
    _file_selection = None
    _pending_delete = None
    _print_menu_job = None
    _history_rows = ()
    _history_error = False
    _usb_back = FILES
    _observed_print = None
    _last_print = None
    _thumb_task = None
    _cancel_in_flight = False
    enable_thumbnails = False

    def reset_print_events(self):
        self._file_directory = ()
        self._file_offset = 0
        self._file_metadata = None
        self._file_meta_cache = {}
        self._file_thumb_cache = {}
        self._file_selection = None
        self._pending_delete = None
        self._print_menu_job = None
        self._history_rows = ()
        self._history_error = False
        self._usb_back = FILES
        self._observed_print = None
        self._last_print = None
        self._thumb_commands = deque()
        self._reset_file_previews()

    def _reset_file_previews(self):
        if self._thumb_task is not None:
            self._thumb_task.cancel()
            self._thumb_task = None
        self._thumb_commands.clear()
        self._thumb_rows = None

    def print_page_changed(self, old, new):
        if old in THUMBNAIL_PAGES and old != new:
            self._reset_file_previews()
        if old == PRINT and new != PRINT:
            self._observed_print = None
        if old == HISTORY_DETAIL and new != HISTORY_DETAIL:
            self._last_print = None
        if old == FILE_DELETE and new != FILE_DELETE:
            self._pending_delete = None
        if old in (PAUSE_MENU, RESUME_MENU) and new != old:
            self._print_menu_job = None
        if old in (*FILE_MENUS, FILE_MENU, FILE_DELETE, FILE_DETAIL, PRINT_SETTINGS) and new not in {
            *FILE_MENUS, FILE_MENU, FILE_DELETE, FILE_DETAIL, PRINT_SETTINGS,
        }:
            self._file_selection = None
            self._file_metadata = None

    async def open_local_files(self, reset=True):
        if reset:
            self._file_directory = ()
            self._file_offset = 0
            self._file_meta_cache = {}
            self._file_thumb_cache = {}
            self._reset_file_previews()
        await self.refresh_files()
        while self._file_directory and not self._file_rows():
            self._file_directory = self._file_directory[:-1]
        last = max(0, (len(self._file_rows()) - 1) // 3 * 3)
        self._file_offset = min(self._file_offset, last)
        if self.page == FILES:
            await self.render_print()
        else:
            await self.go(FILES)

    async def _metadata_for_file(self, filename):
        cache = self._file_meta_cache
        if cache is None:
            self._file_meta_cache = {}
            cache = self._file_meta_cache
        if filename in cache:
            return cache[filename]
        meta = {}
        if self.moonraker.ready:
            try:
                result = await self.moonraker.call("server.files.metadata", {"filename": filename})
                if isinstance(result, dict):
                    meta = dict(result)
            except (RpcError, asyncio.TimeoutError, TypeError) as exc:
                LOG.info("Metadados G-code indisponíveis para %s: %s", filename, exc)
        if (nonnegative_number(meta.get("estimated_time")) is None
                or nonnegative_number(meta.get("filament_total")) is None):
            meta.update(await self._enrich_file_stats(filename, meta))
        cache[filename] = meta
        return meta

    async def _enrich_file_stats(self, filename, meta):
        enriched = {}
        if nonnegative_number(meta.get("estimated_time")) is None:
            from_name = estimated_time_from_filename(filename)
            if from_name is not None:
                enriched["estimated_time"] = from_name
        session = getattr(self.moonraker, "session", None)
        base_url = getattr(self.moonraker, "base_url", None)
        if session is None or not base_url or not self.moonraker.ready:
            return enriched
        need_time = nonnegative_number(meta.get("estimated_time")) is None and "estimated_time" not in enriched
        need_filament = nonnegative_number(meta.get("filament_total")) is None
        if not (need_time or need_filament):
            return enriched
        url = "{}/server/files/gcodes/{}".format(base_url, quote(filename, safe="/"))
        try:
            async with session.get(url, headers={"Range": "bytes=-65536"}, timeout=5) as response:
                if response.status not in (200, 206):
                    response.raise_for_status()
                text_tail = await response.text(errors="ignore")
            estimated, filament = parse_gcode_list_stats(text_tail)
            if need_time and estimated is not None:
                enriched["estimated_time"] = estimated
            if need_filament and filament is not None:
                enriched["filament_total"] = filament
        except (ClientError, asyncio.TimeoutError, OSError, UnicodeError) as exc:
            LOG.info("Leitura local de estatísticas G-code falhou para %s: %s", filename, exc)
        return enriched

    async def _colpic_for_file(self, filename, size):
        cache = self._file_thumb_cache
        key = (filename, size)
        if key in cache:
            return cache[key]
        encoded = None
        session = getattr(self.moonraker, "session", None)
        base_url = getattr(self.moonraker, "base_url", None)
        if session is not None and base_url and self.moonraker.ready:
            url = "{}/server/files/gcodes/{}".format(base_url, quote(filename, safe="/"))
            try:
                byte_range = "bytes=0-{}".format(THUMB_SCAN_BYTES - 1)
                async with session.get(url, headers={"Range": byte_range}, timeout=5) as response:
                    response.raise_for_status()
                    try:
                        head = await response.content.readexactly(THUMB_SCAN_BYTES)
                    except asyncio.IncompleteReadError as exc:
                        head = exc.partial
                thumbnail = extract_thumbnail(head)
                if thumbnail is not None:
                    encoded = await asyncio.get_running_loop().run_in_executor(None, image_to_colpic, thumbnail, size)
            except (ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                LOG.info("Preview G-code indisponível para %s: %s", filename, exc)
                return None
        cache[key] = encoded
        return encoded

    def _schedule_file_previews(self, rows, commands, size, slot_count):
        rows = tuple(rows)
        if not self.enable_thumbnails or rows == self._thumb_rows:
            return
        self._reset_file_previews()
        self._thumb_rows = rows
        for index in range(slot_count):
            commands.extend(("cp{}.close()".format(index), "vis cp{},0".format(index)))

        async def worker():
            for index, (folder, item) in enumerate(rows):
                if not folder:
                    encoded = await self._colpic_for_file(item, size)
                    if encoded:
                        LOG.debug("Miniatura ColPic cp%s preparada (%s caracteres)", index, len(encoded))
                        self._thumb_commands.extend(preview_commands(index, encoded))

        self._thumb_task = asyncio.create_task(worker())

    async def finish_thumbnails(self):
        # The UART loop reads and dispatches touches before sending the next command.
        if self.page in THUMBNAIL_PAGES and self._thumb_commands:
            await self.send(self._thumb_commands.popleft())

    def _file_row_detail_commands(self, index, folder, metadata):
        hours_id, hour_label, mins_id, min_label, filament_id, filament_unit = FILE_ROW_DETAILS[index]
        fields = (hours_id, hour_label, mins_id, min_label, filament_id, filament_unit)
        if folder or not metadata:
            return ["vis {},0".format(name) for name in fields]
        duration = nonnegative_number(metadata.get("estimated_time"))
        filament = nonnegative_number(metadata.get("filament_total"))
        commands = []
        if duration is None:
            commands.extend("vis {},0".format(name) for name in (hours_id, hour_label, mins_id, min_label))
        else:
            minutes = int(duration) // 60
            commands.extend((
                "vis {},1".format(hours_id), "vis {},1".format(hour_label),
                "vis {},1".format(mins_id), "vis {},1".format(min_label),
                "{}.val={}".format(hours_id, minutes // 60),
                "{}.val={}".format(mins_id, minutes % 60),
            ))
        if filament is None:
            commands.extend(("vis {},0".format(filament_id), "vis {},0".format(filament_unit)))
        else:
            commands.extend((
                "vis {},1".format(filament_id), "vis {},1".format(filament_unit),
                text(filament_id, "{:.2f}".format(filament / 1000), 10),
            ))
        return commands

    def _file_rows(self):
        folders = set()
        files = []
        depth = len(self._file_directory)
        for filename in self.files:
            path = relative_path(filename)
            if path is None or path.parts[:depth] != self._file_directory:
                continue
            if len(path.parts) > depth + 1:
                folders.add(path.parts[depth])
            elif len(path.parts) == depth + 1:
                files.append((False, filename))
        return [(True, folder) for folder in sorted(folders, key=lambda name: (name.casefold(), name))] + files

    async def _print_nav(self, action):
        if action == 2:
            if self.page not in (FILES, HISTORY, USB_FILES, USB_FILES_ALT):
                await self.open_local_files(reset=False)
        else:
            target = {0: PRINT if self.job in ACTIVE_STATES else HOME, 1: MOVE, 3: SYSTEM, 4: SUPPORT}[action]
            await self.go(target)

    async def _show_history(self):
        self._history_rows = ()
        self._history_error = not self.moonraker.ready
        if self.moonraker.ready:
            try:
                result = await self.moonraker.call("server.history.list", {"limit": 5, "order": "desc"})
                self._history_rows = tuple(row for row in result["jobs"][:5] if isinstance(row, dict))
            except (RpcError, asyncio.TimeoutError, KeyError, TypeError) as exc:
                LOG.info("Histórico de impressão indisponível: %s", exc)
                self._history_error = True
        await self.go(HISTORY)

    async def _show_usb_notice(self, origin):
        self._usb_back = origin
        await self.go(USB_NOTICE)

    async def _file_details(self):
        filename = self._file_selection
        if filename not in self.files:
            return
        self._file_metadata = {}
        if self.moonraker.ready:
            try:
                result = await self.moonraker.call("server.files.metadata", {"filename": filename})
                if isinstance(result, dict):
                    self._file_metadata = dict(result)
            except (RpcError, asyncio.TimeoutError) as exc:
                LOG.info("Metadados G-code indisponíveis: %s", exc)
        if (nonnegative_number(self._file_metadata.get("estimated_time")) is None
                or nonnegative_number(self._file_metadata.get("filament_total")) is None):
            self._file_metadata.update(await self._enrich_file_stats(filename, self._file_metadata))
        await self.go(FILE_DETAIL)

    async def _idle_for_file_operation(self):
        state = await self.fresh_status("print_stats", "virtual_sdcard")
        return (state.get("print_stats", {}).get("state") in IDLE_STATES
                and state.get("virtual_sdcard", {}).get("is_active") is False)

    async def _print_selected_file(self):
        filename = self._file_selection
        if (relative_path(filename) is not None and filename in self.files
                and await self._idle_for_file_operation()
                and await self.action("printer.print.start", {"filename": filename})):
            self._file_selection = None
            await self.go(PRINT)

    async def _delete_selected_file(self, action):
        pending = self._pending_delete
        self._pending_delete = None
        if action == 1 and pending and pending[1] == getattr(self.moonraker, "generation", 0):
            filename, _ = pending
            if (relative_path(filename) is not None and filename in self.files
                    and await self._idle_for_file_operation()
                    and await self.action("server.files.delete_file", {"path": "gcodes/" + filename})):
                self.files = [item for item in self.files if item != filename]
                while self._file_directory and not self._file_rows():
                    self._file_directory = self._file_directory[:-1]
                self._file_offset = min(self._file_offset, max(0, (len(self._file_rows()) - 1) // 3 * 3))
        await self.go(FILES)

    async def _list_button(self, page, action):
        if action in range(5):
            await self._print_nav(action)
        elif action == 5:
            await self.open_local_files(reset=page != FILES)
        elif action == 6:
            await self._show_usb_notice(page)
        elif action == 7:
            await self._show_history()
        elif page != FILES:
            await self._show_usb_notice(FILES)
        elif action in (8, 9, 10):
            index = self._file_offset + action - 8
            rows = self._file_rows()
            if self.moonraker.ready and index < len(rows):
                folder, item = rows[index]
                if folder:
                    self._file_directory += (item,)
                    self._file_offset = 0
                    await self.render_print()
                else:
                    self._file_selection = item
                    self._file_metadata = None
                    await self.go(FILE_MENUS[action - 8])
        elif action in (11, 12):
            last = max(0, (len(self._file_rows()) - 1) // 3 * 3)
            self._file_offset = min(last, max(0, self._file_offset + (3 if action == 12 else -3)))
            await self.render_print()
        elif action == 13:
            self._file_directory = self._file_directory[:-1]
            self._file_offset = 0
            await self.render_print()

    async def _print_menu(self, page, action):
        captured = self._print_menu_job
        self._print_menu_job = None
        if captured and action in (0, 1):
            state = await self.fresh_status("print_stats")
            job = state.get("print_stats", {})
            current = job.get("state")
            expected = "paused" if page == RESUME_MENU else "printing"
            if (current in ACTIVE_STATES and job.get("filename") == captured[0]
                    and getattr(self.moonraker, "generation", 0) == captured[1]):
                if action == 1:
                    self._cancel_in_flight = True
                    try:
                        await self.go(STOPPING)
                        cancelled = await self.action("printer.gcode.script", {"script": "CANCEL_PRINT"})
                    except (RpcError, asyncio.TimeoutError):
                        await self.go(PRINT if self.job in ACTIVE_STATES else HOME)
                        raise
                    finally:
                        self._cancel_in_flight = False
                    if cancelled:
                        await self.render_print()
                        return
                if action == 0 and current == expected:
                    await self.action("printer.print.resume" if page == RESUME_MENU else "printer.print.pause")
        await self.go(PRINT if self.job in ACTIVE_STATES else HOME)

    async def finish_print_page(self, previous):
        stats = self.status.get("print_stats", {})
        filename = stats.get("filename")
        generation = getattr(self.moonraker, "generation", 0)
        if (previous not in ACTIVE_STATES or self.job != "complete"
                or self._observed_print != (filename, generation) or relative_path(filename) is None):
            await self.go(HOME)
            return
        duration = nonnegative_number(stats.get("print_duration"))
        self._last_print = (filename, generation, duration, None)
        try:
            result = await self.moonraker.call("server.history.list", {"limit": 1, "order": "desc"})
            row = next(iter(result["jobs"]), {})
            if (row.get("filename") == filename and row.get("status") == "completed"
                    and duration is not None and row.get("print_duration") == duration):
                self._last_print = (filename, generation, row.get("total_duration"), row.get("start_time"))
        except (RpcError, asyncio.TimeoutError, AttributeError, KeyError, TypeError) as exc:
            LOG.info("Resultado da última impressão sem detalhes de histórico: %s", exc)
        if (self.job != "complete" or self.status.get("print_stats", {}).get("filename") != filename
                or generation != getattr(self.moonraker, "generation", 0)):
            self._last_print = None
            await self.go(PRINT if self.job in ACTIVE_STATES else HOME)
            return
        await self.go(HISTORY_DETAIL)

    async def _reprint_last_file(self):
        completed = self._last_print
        if not completed or not self.enable_controls or not self.moonraker.ready:
            return
        filename, generation, _, _ = completed
        if generation != getattr(self.moonraker, "generation", 0):
            return
        try:
            metadata = await self.moonraker.call("server.files.metadata", {"filename": filename})
        except (RpcError, asyncio.TimeoutError) as exc:
            LOG.info("Arquivo da última impressão indisponível: %s", exc)
            return
        if not isinstance(metadata, dict) or not metadata or metadata.get("filename", filename) != filename:
            return
        status = await self.fresh_status("print_stats", "virtual_sdcard")
        stats = status.get("print_stats", {})
        if (stats.get("state") == "complete" and stats.get("filename") == filename
                and status.get("virtual_sdcard", {}).get("is_active") is False
                and generation == getattr(self.moonraker, "generation", 0)
                and await self.action("printer.print.start", {"filename": filename})):
            await self.go(PRINT)

    async def handle_print(self, event):
        if event.kind == "boot":
            self.reset_print_events()
            return False
        if event.page != self.page:
            return False
        if event.kind == "number":
            return event.value is None and (event.page, event.action) in {(EMERGENCY, 0), (STOPPING, 0)}
        if event.kind != "button":
            return False
        page, action = event.page, event.action
        if (page, action) == (PRINT, 5):
            if self.job in ACTIVE_STATES:
                filename = self.status.get("print_stats", {}).get("filename")
                self._print_menu_job = ((filename, getattr(self.moonraker, "generation", 0))
                                        if filename else None)
                await self.go(RESUME_MENU if self.job == "paused" else PAUSE_MENU)
            return True
        if page in (FILES, USB_FILES, USB_FILES_ALT) and action in range(14):
            if page == USB_FILES_ALT and action == 4:
                return False
            await self._list_button(page, action)
        elif page in (*FILE_MENUS, FILE_MENU) and action in (0, 1, 2, 7, 8, 9, 10, 11):
            if page == FILE_MENU and action >= 7:
                return False
            if action == 0:
                await self._file_details()
            elif action == 1 and self._file_selection in self.files:
                self._pending_delete = (self._file_selection, getattr(self.moonraker, "generation", 0))
                await self.go(FILE_DELETE)
            elif action == 2:
                await self.go(FILES)
            elif action >= 7:
                await self._print_nav(action - 7)
        elif page == FILE_DELETE and action in (0, 1):
            await self._delete_selected_file(action)
        elif page == FILE_DETAIL and action in range(9):
            if action <= 4:
                await self._print_nav(action)
            elif action in (5, 8):
                await self.go(PRINT_SETTINGS)
            elif action == 6:
                await self.go(FILES)
            else:
                await self._print_selected_file()
        elif page == HISTORY and action in range(8):
            if action <= 4:
                await self._print_nav(action)
            elif action == 5:
                await self.open_local_files()
            elif action == 6:
                await self._show_usb_notice(HISTORY)
        elif (page, action) == (USB_NOTICE, 0):
            await self.go(self._usb_back)
        elif page in (PAUSE_MENU, RESUME_MENU) and action in (0, 1, 2):
            await self._print_menu(page, action)
        elif page == EMERGENCY and action in (0, 1):
            LOG.info("Ação do alerta de emergência sem confirmação de estado: %s", action)
        elif page == FILAMENT_NOTICE and action in (0, 1):
            if action == 1:
                await self.go(PRINT if self.job in ACTIVE_STATES else HOME)
            else:
                LOG.info("Alerta de filamento sem origem de sensor confirmada")
        elif page == HISTORY_DETAIL and action in range(7):
            if action == 5:
                await self._reprint_last_file()
            elif action == 6:
                await self.go(PRINT if self.job in ACTIVE_STATES else HOME)
        else:
            return False
        return True

    async def render_print(self):
        page = self.page
        if page == STOPPING and not self._cancel_in_flight and self.job in IDLE_STATES:
            await self.go(HOME)
            return
        commands = []
        if page in (FILES, USB_FILES, USB_FILES_ALT):
            rows = self._file_rows()[self._file_offset:self._file_offset + 3] if page == FILES else []
            if page == FILES:
                self._schedule_file_previews(rows, commands, FILE_PREVIEW_SIZE, len(LABELS))
            for index, label in enumerate(LABELS):
                value = ""
                folder, item, metadata = True, "", None
                if index < len(rows):
                    folder, item = rows[index]
                    value = item + "/" if folder else PurePosixPath(item).name
                    if page == FILES and not folder:
                        metadata = await self._metadata_for_file(item)
                commands.append(text(label, value, 39))
                if page == FILES:
                    commands.extend(self._file_row_detail_commands(index, folder or index >= len(rows), metadata))
        elif page == FILE_DETAIL and self._file_selection:
            self._schedule_file_previews([(False, self._file_selection)], commands, DETAIL_PREVIEW_SIZE, 1)
            metadata = self._file_metadata or {}
            duration = nonnegative_number(metadata.get("estimated_time"))
            filament = nonnegative_number(metadata.get("filament_total"))
            minutes = int(duration or 0) // 60
            commands.extend((
                text("g0", PurePosixPath(self._file_selection).name, 39),
                "n4.val={}".format(minutes // 60), "n5.val={}".format(minutes % 60),
                text("t2", "{:.2f}".format(filament / 1000) if filament is not None else "", 10),
            ))
        elif page in (FILE_DELETE, FILE_MENU) and self._file_selection:
            commands.append(text("t0", PurePosixPath(self._file_selection).name, 48))
        elif page == USB_NOTICE:
            commands.append(text("t0", "USB indisponivel", 48))
        elif page == PRINT:
            filename = self.status.get("print_stats", {}).get("filename") or ""
            rows = [(False, filename)] if filename else []
            self._schedule_file_previews(rows, commands, DETAIL_PREVIEW_SIZE, 1)
            if self.job in ACTIVE_STATES:
                self._observed_print = (filename, getattr(self.moonraker, "generation", 0))
            commands.append(text("g0", PurePosixPath(filename).name, 39))
        elif page == HISTORY_DETAIL:
            filename, _, duration, started = self._last_print or ("", None, None, None)
            rows = [(False, filename)] if filename else []
            self._schedule_file_previews(rows, commands, DETAIL_PREVIEW_SIZE, 1)
            commands.extend((
                text("g0", PurePosixPath(filename).name if filename else "", 39),
                text("t2", job_timestamp(started, "%d/%m %H:%M"), 20),
                text("t4", job_duration(duration), 16),
                text("t5", "Concluido" if filename else "", 16),
                "tsw b5,{}".format(int(bool(filename))),
            ))
        elif page == HISTORY:
            for index in range(5):
                row = self._history_rows[index] if index < len(self._history_rows) else {}
                path = relative_path(row.get("filename"))
                stamp = job_timestamp(row.get("start_time"), "%d/%m/%y %H:%M")
                elapsed = job_duration(row.get("total_duration"))
                if index == 0 and self._history_error:
                    stamp = "Indisponivel"
                commands.extend((
                    text("t{}".format(4 + 3 * index), path.name if path else "", 24),
                    text("t{}".format(5 + 3 * index), stamp, 24),
                    text("t{}".format(6 + 3 * index), elapsed, 16),
                    "q{}.picc={}".format(index, 24 if row.get("status") == "completed" else 74),
                ))
        await self.paint(commands)
