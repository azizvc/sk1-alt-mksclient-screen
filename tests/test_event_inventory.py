import asyncio
from collections import Counter
import csv
from pathlib import Path
import re
import unittest

from mksclient_alt.panel import Panel
from mksclient_alt.protocol import PanelDecoder, PanelEvent, TERM

CATALOG = Path(__file__).resolve().parents[1] / "docs/LKS-EVENTS.csv"
FIXED_FRAME = re.compile(r"0x(65|71)\((\d+),(\d+)\)(,u16)?\+FFx3")


class OfflineMoonraker:
    ready = False
    generation = 0
    status = {}

    async def network_addresses(self):
        return []


class EventInventoryTests(unittest.TestCase):
    def test_every_numbered_uart_event_can_be_replayed_offline(self):
        with CATALOG.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({int(row["page_id"]) for row in rows}, set(range(80)))
        self.assertEqual(Counter(row["execution"] for row in rows),
                         {"empty": 2064, "local": 427, "UART": 497})
        self.assertEqual({row["area"] for row in rows}, {"printing", "operation", "system"})
        cases = []
        for row in rows:
            for signature in row["uart"].split(" | "):
                match = FIXED_FRAME.fullmatch(signature)
                if match:
                    opcode, page, action = int(match[1], 16), int(match[2]), int(match[3])
                    data = b"\x32\x00" if match[4] else b""
                    event = PanelEvent("button" if opcode == 0x65 else "number",
                                       page, action, 50 if data else None)
                    cases.append((bytes([opcode, page, action]) + data + TERM, event))
        self.assertEqual(len(cases), 492)

        async def replay():
            async def send(command):
                pass

            for frame, expected in cases:
                with self.subTest(event=expected):
                    decoder = PanelDecoder()
                    self.assertEqual(list(decoder.feed(frame[:2])), [])
                    events = list(decoder.feed(frame[2:]))
                    self.assertEqual(events, [expected])
                    panel = Panel(OfflineMoonraker(), send)
                    panel.sync_page(expected.page)
                    await panel.handle(events[0])

        asyncio.run(replay())
