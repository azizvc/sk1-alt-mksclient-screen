"""G-code thumbnail extraction and TJC ColPic encoding for the LKS file cards."""

import base64
from collections import Counter
from itertools import groupby
import io
import re
import struct

from PIL import Image, ImageOps

THUMB_BEGIN_RE = re.compile(br"^;[ \t]*thumbnail(?:_jpg)? begin \d+x\d+ (\d+)[ \t]*\r?$", re.I | re.M)
THUMB_END_RE = re.compile(br"^;[ \t]*thumbnail(?:_jpg)? end[ \t]*\r?$", re.I | re.M)
FILE_PREVIEW_SIZE = (98, 98)
DETAIL_PREVIEW_SIZE = (155, 155)
PREVIEW_CHUNK_SIZE = 1024
COLPIC_MAGIC = 98419516
COLPIC_ALPHABET = bytes.maketrans(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
    bytes(range(48, 112)).replace(b"\\", b"~"),
)


def extract_thumbnail(data):
    """Return the first complete embedded PNG or JPEG."""
    for marker in THUMB_BEGIN_RE.finditer(data):
        end = THUMB_END_RE.search(data, marker.end())
        if end is None:
            break
        lines = data[marker.end():end.start()].splitlines()
        encoded = b"".join(line.strip()[1:].strip() for line in lines if line.strip().startswith(b";"))
        if len(encoded) != int(marker[1]):
            continue
        try:
            image = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        if image.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
            return image
    return None


def _rgb565_pixels(image):
    return [((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)
            for red, green, blue in image.getdata()]


def image_to_colpic(image_bytes, size=FILE_PREVIEW_SIZE):
    """Fit the image into a file-card canvas and encode it for cpN.write()."""
    resampling = getattr(Image, "Resampling", Image)
    with Image.open(io.BytesIO(image_bytes)) as source:
        fitted = ImageOps.contain(source.convert("RGBA"), size, resampling.LANCZOS)
    image = Image.new("RGB", size)
    image.paste(fitted, ((image.width - fitted.width) // 2, (image.height - fitted.height) // 2),
                fitted.getchannel("A"))
    pixels = _rgb565_pixels(image)
    if len(set(pixels)) > 256:
        pixels = _rgb565_pixels(image.quantize(colors=256).convert("RGB"))
    return _encode_colpic(pixels, *image.size)


def _encode_colpic(pixels, width, height):
    palette = [color for color, _ in Counter(pixels).most_common()]
    indices = {color: index for index, color in enumerate(palette)}
    runs = bytearray()
    bank = 0
    for color, group in groupby(pixels):
        count = sum(1 for _ in group)
        next_bank, entry = divmod(indices[color], 32)
        if bank != next_bank:
            runs.append(0xe0 | next_bank)
            bank = next_bank
        while count > 255:
            runs.extend((entry, 255))
            count -= 255
        if count > 6:
            runs.extend((entry, count))
        else:
            runs.append((count << 5) | entry)

    header = struct.pack("<8I", 3, width, height, COLPIC_MAGIC, len(palette) * 2, len(runs), 0, 0)
    raw = header + struct.pack("<{}H".format(len(palette)), *palette) + runs
    # The vendor encoder terminates the image with zero padding to the next group.
    raw += bytes(3 - len(raw) % 3)
    return base64.b64encode(raw).translate(COLPIC_ALPHABET).decode("ascii")


def preview_commands(index, encoded):
    component = "cp{}".format(index)
    yield "vis {},1".format(component)
    for offset in range(0, len(encoded), PREVIEW_CHUNK_SIZE):
        chunk = encoded[offset:offset + PREVIEW_CHUNK_SIZE]
        yield '{}.write("{}")'.format(component, chunk)
