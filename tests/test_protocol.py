import unittest

from mksclient_alt.protocol import PanelDecoder, PanelEvent, TERM, encode, text


class ProtocolTests(unittest.TestCase):
    def test_split_and_concatenated_stock_events(self):
        decoder = PanelDecoder()
        self.assertEqual(list(decoder.feed(bytes.fromhex("91 65 00"))), [PanelEvent("hello")])
        self.assertEqual(list(decoder.feed(bytes.fromhex("07 ff"))), [])
        self.assertEqual(list(decoder.feed(bytes.fromhex("ff ff 71 00 07 ff 00 ff ff ff 91"))), [
            PanelEvent("button", 0, 7), PanelEvent("number", 0, 7, 255), PanelEvent("hello"),
        ])

    def test_system_brightness_byte_does_not_swallow_the_next_button(self):
        decoder = PanelDecoder()
        frames = bytes.fromhex("58 65 0b 0a ff ff ff")
        self.assertEqual(list(decoder.feed(frames, system_brightness=True)), [PanelEvent("button", 11, 10)])

    def test_startup_page_response_and_touch_format(self):
        decoder = PanelDecoder()
        frames = bytes.fromhex("00 00 00 ff ff ff 88 ff ff ff 66 26 ff ff ff 65 00 07 00 ff ff ff")
        self.assertEqual(list(decoder.feed(frames)), [PanelEvent("boot"), PanelEvent("page", 38)])

    def test_ignores_password_frames_and_unknown_packets(self):
        decoder = PanelDecoder()
        self.assertEqual(list(decoder.feed(b"\x70\x16\x01private" + TERM)), [])
        self.assertEqual(list(decoder.feed(b"\x71\x1a\x00" + TERM)), [PanelEvent("number", 26, 0)])

    def test_wifi_keyboard_emits_an_unterminated_packet(self):
        decoder = PanelDecoder()
        self.assertEqual(list(decoder.feed(b"\x70\x12\x08test-")), [])
        self.assertEqual(list(decoder.feed(b"password")), [])
        event, = decoder.idle()
        self.assertEqual(event, PanelEvent("wifi_password", 18, 8, secret=b"test-password"))
        self.assertNotIn("test-password", repr(event))
        self.assertEqual(list(decoder.idle()), [])

    def test_string_values_cannot_create_commands(self):
        command = text("t0", 'a"\n\\á', limit=5)
        self.assertEqual(command, 't0.txt="a   "')
        self.assertEqual(encode(command), b't0.txt="a   "' + TERM)
