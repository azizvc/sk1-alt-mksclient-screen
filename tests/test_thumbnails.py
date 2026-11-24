import base64
import unittest

from mksclient_alt.thumbnails import (
    PREVIEW_CHUNK_SIZE, _encode_colpic, extract_thumbnail, preview_commands,
)


class ThumbnailTests(unittest.TestCase):
    def test_extract_png_from_orca_comments(self):
        png = b"\x89PNG\r\n\x1a\n" + bytes(16)
        body = base64.b64encode(png)
        data = b"; thumbnail begin 2x2 %d\n; %s\n; thumbnail end\n" % (len(body), body)
        self.assertEqual(extract_thumbnail(data), png)
        self.assertIsNone(extract_thumbnail(data.split(b"; thumbnail end")[0]))

    def test_colpic_matches_stock_sk1_encoder_for_a_solid_tile(self):
        # Output captured from the SK1's libColPic.so for an 8x8 RGB565 red tile.
        expected = "0`0000P000080000?<?M1@8000020000000000000000n0100000"
        self.assertEqual(_encode_colpic([0xf800] * 64, 8, 8), expected)

    def test_preview_writes_bounded_strings_to_the_active_page_component(self):
        full_chunk = "0" * PREVIEW_CHUNK_SIZE
        self.assertEqual(list(preview_commands(2, full_chunk + "abcd")), [
            "vis cp2,1",
            'cp2.write("{}")'.format(full_chunk),
            'cp2.write("abcd")',
        ])
