"""Stock LKS wire frames. Button numbers are the literals in HMI events."""

from dataclasses import dataclass, field
from typing import Optional

TERM = b"\xff\xff\xff"


@dataclass(frozen=True)
class PanelEvent:
    kind: str
    page: Optional[int] = None
    action: Optional[int] = None
    value: Optional[int] = None
    secret: Optional[bytes] = field(default=None, repr=False)


class PanelDecoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk, *, system_brightness=False):
        self.buffer.extend(chunk)
        while self.buffer:
            opcode = self.buffer[0]
            if opcode == 0x91:  # page 38 timer, no terminator
                del self.buffer[0]
                yield PanelEvent("hello")
                continue
            if system_brightness and 28 <= opcode <= 100 and self.buffer[1:4] != TERM:
                # The System brightness buttons already apply and save dim in
                # the TFT. Their standalone byte must not absorb the next frame.
                del self.buffer[0]
                continue

            end = self.buffer.find(TERM)
            if end < 0:
                if len(self.buffer) > 1024:
                    del self.buffer[:-2]
                return
            frame = bytes(self.buffer[:end])
            del self.buffer[:end + len(TERM)]
            if opcode == 0x65 and len(frame) == 3:
                yield PanelEvent("button", frame[1], frame[2])
            elif opcode == 0x71 and len(frame) in (3, 5):
                value = int.from_bytes(frame[3:5], "little") if len(frame) == 5 else None
                yield PanelEvent("number", frame[1], frame[2], value)
            elif opcode == 0x66 and len(frame) == 2:
                yield PanelEvent("page", frame[1])
            elif opcode == 0x70 and frame.endswith(b"\x65\x16\x00"):
                yield PanelEvent("button", 22, 0)
            elif frame == b"\x88":
                yield PanelEvent("boot")

    def idle(self):
        # The LKS Wi-Fi keyboard emits a raw string without a terminator.
        # The host reader calls this only after a UART receive timeout.
        if not self.buffer or self.buffer[0] != 0x70:
            return
        frame = bytes(self.buffer)
        self.buffer.clear()
        if len(frame) >= 3 and frame[1] == 18 and frame[2] in range(7, 11):
            password = frame[3:]
            if len(password) <= 64 and all(32 <= byte <= 126 for byte in password):
                yield PanelEvent("wifi_password", frame[1], frame[2], secret=password)


def encode(command):
    if any(char in command for char in ("\r", "\n", "\0")):
        raise ValueError("Invalid TJC command")
    return command.encode("utf-8") + TERM


def text(component, value, limit=48):
    # Text arriving from Moonraker must stay inside a single TJC string.
    clean = "".join(char if char.isprintable() and char not in '"\\' else " " for char in str(value))
    bounded = clean.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
    return '{}.txt="{}"'.format(component, bounded)
