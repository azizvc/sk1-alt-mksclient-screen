"""Export the editable LKS event inventory without copying firmware scripts."""

import argparse
from collections import Counter
import csv
from hashlib import sha256
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO / "docs/LKS-EVENTS.csv"

# These are ownership assignments for review, not assumptions about event behavior.
FILES_PAGES = {0, 1, 2, 7, 8, 9, 10, 18, 22, 23, 24, 25, 26, 27, 28, 30, 31, 38, 43, 44,
               54, 59, 60, 61, 62, 73, 74, 76, 77}
OPERATION_PAGES = {3, 4, 5, 6, 33, 34, 35, 36, 37, 42, 45, 46, 47, 48, 49, 50, 51, 55, 56, 57, 58}
SYSTEM_PAGES = set(range(11, 18)) | {19, 20, 21, 29, 32, 39, 40, 41, 52, 53, 75, 78, 79} | set(range(63, 73))
PRINTS = re.compile(r"prints\s+([^,]+),\s*(\d+)")
HOST_OUTPUT = re.compile(r"(?:printh|prints|print)\b")
LOCAL_PAGE = re.compile(r"page\s+(\S+)")
LOCAL_CLICK = re.compile(r"click\s+([^,]+),")
HEADER = ["page_id", "page", "component_id", "component", "type_id", "event",
          "lines", "execution", "uart", "local_pages", "local_clicks", "area"]


def owner(page_id):
    if page_id in FILES_PAGES:
        return "printing"
    if page_id in OPERATION_PAGES:
        return "operation"
    if page_id in SYSTEM_PAGES:
        return "system"
    raise ValueError("Page has no assigned functional area: {}".format(page_id))


def unsigned_decimal(value):
    try:
        return int(value, 10)
    except ValueError:
        return None


def uart_signature(run, page_id):
    fields = [PRINTS.fullmatch(line) for line in run]
    if not all(fields):
        # Opcode only. Avoid reproducing arbitrary command text from the HMI.
        return "+".join(line.split()[0] for line in run)
    values = [field[1] for field in fields]
    widths = [int(field[2]) for field in fields]
    opcode = unsigned_decimal(values[0])
    if len(values) >= 3 and opcode in {0x65, 0x70, 0x71}:
        page = page_id if values[1] == "dp" else values[1]
        action = unsigned_decimal(values[2])
        action = values[2] if action is None else action
        suffix = values[3:]
        if [unsigned_decimal(value) for value in suffix[-3:]] == [255, 255, 255]:
            payload = suffix[:-3]
            extra = ",u16" if len(payload) == 1 and widths[3] == 2 else ""
            if not payload or extra:
                return "0x{:02X}({},{}){}+FFx3".format(opcode, page, action, extra)
        if opcode == 0x70 and widths[3:] == [0]:
            return "0x70({},{})+unterminated-text".format(page, action)
    if len(fields) == 1:
        if opcode is not None:
            return "0x{:02X}/{}B".format(opcode, widths[0])
        return "variable({})/{}B".format(values[0], widths[0])
    return "prints/{} fields".format(len(fields))


def event_fields(lines, page_id):
    serial, run, pages, clicks = [], [], [], []
    for source in [*lines, b""]:
        line = source.decode("utf-8", errors="replace").strip()
        if HOST_OUTPUT.match(line):
            run.append(line)
            continue
        if run:
            serial.append(uart_signature(run, page_id))
            run.clear()
        page = LOCAL_PAGE.fullmatch(line)
        if page:
            pages.append(page[1])
        click = LOCAL_CLICK.match(line)
        if click:
            clicks.append(click[1])
    mode = "UART" if serial else ("local" if lines else "empty")
    return [len(lines), mode, " | ".join(serial), " | ".join(pages), " | ".join(clicks)]


def export(source, output, decoder_path):
    sys.path.insert(0, str(decoder_path))
    from nxwright.project.container import HmiContainer
    from nxwright.project.page_block import Event, parse_page

    content = source.read_bytes()
    container = HmiContainer(content)
    blocks = container.blocks
    if not container.directory_checksum_is_valid():
        raise ValueError("Invalid HMI directory checksum")
    page_ids = sorted(int(match[1]) for name in blocks if (match := re.fullmatch(r"(\d+)\.pa", name)))
    if page_ids != list(range(80)):
        raise ValueError("The HMI must contain pages 0–79")

    output.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER)
        for page_id in page_ids:
            page = parse_page(blocks["{}.pa".format(page_id)])
            for object_id, obj in enumerate(page.objects):
                name = (obj.attribute("objname") or b"").decode("ascii", errors="replace")
                kind = int.from_bytes(obj.attribute("type") or b"", "little")
                for item in obj.items:
                    if isinstance(item, Event):
                        details = event_fields(item.lines, page_id)
                        writer.writerow([page_id, page.name, object_id, name, kind, item.name, *details, owner(page_id)])
                        counts[details[1]] += 1
    print("HMI SHA-256:", sha256(content).hexdigest())
    print("{} pages / {} declared slots: {}".format(len(page_ids), sum(counts.values()), dict(counts)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--decoder", type=Path, required=True)
    args = parser.parse_args()
    export(args.source, args.output, args.decoder)


if __name__ == "__main__":
    main()
