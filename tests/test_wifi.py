import unittest
from contextlib import nullcontext
from unittest.mock import patch

from mksclient_alt.wifi import WifiError, WifiNetwork, _connect, parse_scan_results


class WifiTests(unittest.TestCase):
    def test_association_failure_restores_the_active_profile_without_saving(self):
        commands = []

        def request(_, command):
            commands.append(command)
            return {
                b"LIST_NETWORKS": b"network id / ssid / bssid / flags\n0\tOld\tany\t[CURRENT]\n",
                b"STATUS": b"id=0\nwpa_state=COMPLETED\n",
                b"GET update_config": b"1\n",
                b"GET_NETWORK 0 priority": b"4\n",
                b"ADD_NETWORK": b"1\n",
            }.get(command, b"OK\n")

        target = WifiNetwork('Net "1"', -68, False, "psk")
        with patch("mksclient_alt.wifi._open_control", return_value=nullcontext(object())), \
                patch("mksclient_alt.wifi._request", side_effect=request), \
                patch("mksclient_alt.wifi._wait_for_address", side_effect=WifiError("timeout")):
            with self.assertRaises(WifiError):
                _connect(target, b"local-test-pass", "/test.sock")
        self.assertIn(b"SET_NETWORK 1 ssid 4e657420223122", commands)
        self.assertTrue(all(b"local-test-pass" not in command for command in commands))
        self.assertEqual(commands[-4:], [
            b"SELECT_NETWORK 1", b"REMOVE_NETWORK 1", b"SELECT_NETWORK 0", b"ENABLE_NETWORK 0",
        ])
        self.assertNotIn(b"SAVE_CONFIG", commands)

    def test_scan_deduplicates_ssids_and_keeps_the_connected_bss(self):
        results = (
            b"bssid / frequency / signal level / flags / ssid\n"
            b"aa\t2412\t-70\t[WPA2]\tRede\\x20Caf\\xc3\\xa9\n"
            b"bb\t5180\t-43\t[WPA2]\tRede Caf\xc3\xa9\n"
            b"cc\t2437\t-50\t[ESS]\tGuest\n"
            b"dd\t2437\t-80\t[ESS]\tGuest\n"
            b"ee\t2437\t-40\t[ESS]\t\\x00\n"
            b"bad row\n"
        )
        self.assertEqual(parse_scan_results(results, b"aa"), [
            WifiNetwork("Rede Café", -70, True),
            WifiNetwork("Guest", -50, False),
        ])
