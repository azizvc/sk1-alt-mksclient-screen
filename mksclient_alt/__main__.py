"""Command line entry point for the stock LKS UART client."""

import argparse
import asyncio
from contextlib import suppress
import json
import logging
import os
from urllib.parse import urlsplit

import aiohttp

from .moonraker import Moonraker, RpcError
from .panel import ACTIVE, HOME, PRINT, SYSTEM, Panel
from .protocol import PanelDecoder, encode
from .wifi import DEFAULT_SOCKET, connect_wifi, scan_wifi

LOG = logging.getLogger(__name__)


async def run_probe(moonraker, base_url):
    async def snapshot():
        while not moonraker.ready:
            moonraker.changed.clear()
            await moonraker.changed.wait()
        server = await moonraker.call("server.info")
        printer = await moonraker.call("printer.info")
        files = await moonraker.call("server.files.list", {"root": "gcodes"})
        return {
            "url": base_url,
            "moonraker": server.get("moonraker_version"),
            "moonraker_api": server.get("api_version_string"),
            "klipper": printer.get("software_version"),
            "state": printer.get("state"),
            "objects": sorted(moonraker.status),
            "gcode_files": len(files),
        }

    print(json.dumps(await asyncio.wait_for(snapshot(), timeout=15), indent=2))


async def run_panel(moonraker, args):
    import serial

    loop = asyncio.get_running_loop()
    decoder = PanelDecoder()
    options = {"exclusive": True} if os.name == "posix" else {}
    with serial.Serial(args.serial, 115200, timeout=0.12, write_timeout=1, **options) as uart:
        async def send(command):
            payload = encode(command)
            written = await loop.run_in_executor(None, uart.write, payload)
            if written != len(payload):
                raise OSError("Escrita UART incompleta")

        async def scan_local_wifi():
            return await scan_wifi(args.wifi_socket)

        async def connect_local_wifi(network, password):
            await connect_wifi(network, password, args.wifi_socket)

        panel = Panel(moonraker, send, enable_controls=args.enable_controls,
                      printer_address=args.display_ip,
                      wifi_scanner=scan_local_wifi if os.name == "posix" else None,
                      wifi_connector=connect_local_wifi if os.name == "posix" else None,
                      enable_thumbnails=not args.no_thumbnails)
        if args.no_thumbnails:
            LOG.info("Envio de miniaturas ao TFT desativado")
        LOG.info("UART %s @ 115200, controles %s", args.serial,
                 "ativos" if args.enable_controls else "desativados")
        await send("sendme")
        generation = 0
        was_ready = False
        while True:
            chunk = await loop.run_in_executor(None, uart.read, 256)
            if generation != moonraker.generation or (was_ready and not moonraker.ready):
                panel.sent_attributes.clear()
                panel.resume_action = None
                panel.reset_print_events()
                generation = moonraker.generation
                if moonraker.ready and panel.page is None:
                    await panel.go(PRINT if panel.job in ACTIVE else HOME)
            was_ready = moonraker.ready
            events = decoder.feed(chunk, system_brightness=panel.page == SYSTEM) if chunk else decoder.idle()
            for event in events:
                await panel.process(event)
            await panel.finish_wifi_scan()
            await panel.finish_wifi_connection()
            await panel.finish_operation()
            await panel.finish_system()
            if moonraker.ready and moonraker.changed.is_set():
                moonraker.changed.clear()
                await panel.update()
            await panel.finish_thumbnails()
            await panel.render_wifi_icon()


async def run(args):
    api_key = os.environ.get("MKSCLIENT_ALT_API_KEY")
    headers = {"X-Api-Key": api_key} if api_key else {}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as session:
        moonraker = Moonraker(session, args.moonraker)
        task = asyncio.create_task(moonraker.run())
        try:
            if args.probe:
                await run_probe(moonraker, args.moonraker)
            else:
                await run_panel(moonraker, args)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", action="store_true", help="Consulta Moonraker sem abrir a UART")
    mode.add_argument("--serial", metavar="PORTA", help="Porta da tela LKS, por exemplo /dev/ttyS1")
    parser.add_argument("--moonraker", default="http://127.0.0.1:7125", metavar="URL")
    parser.add_argument("--display-ip", default="", help="IP exibido na página de rede ao usar Moonraker local")
    parser.add_argument("--wifi-socket", default=DEFAULT_SOCKET, help="Socket local do wpa_supplicant")
    controls = parser.add_mutually_exclusive_group()
    controls.add_argument("--read-only", dest="enable_controls", action="store_false",
                          help="Desativa comandos de controle pela tela")
    controls.add_argument("--enable-controls", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(enable_controls=None)
    parser.add_argument("--no-thumbnails", action="store_true",
                        default=os.environ.get("MKSCLIENT_ALT_DISABLE_THUMBNAILS") == "1",
                        help="Desativa o envio de miniaturas ao TFT na lista de arquivos")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.moonraker)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
        parser.error("--moonraker deve ser uma URL HTTP(S) sem credenciais, consulta ou fragmento")
    if args.probe and args.enable_controls:
        parser.error("--enable-controls requer --serial")
    if args.enable_controls is None:
        args.enable_controls = not args.probe
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except (RpcError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        LOG.error("%s", str(exc) or "Tempo limite aguardando a SK1")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
