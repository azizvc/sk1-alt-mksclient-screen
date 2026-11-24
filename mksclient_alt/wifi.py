"""Wi-Fi discovery and confirmed connection changes through wpa_supplicant."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
from ipaddress import AddressValueError, IPv4Address
import logging
from pathlib import Path
import re
import socket
from tempfile import TemporaryDirectory
from time import monotonic, sleep

DEFAULT_SOCKET = "/run/wpa_supplicant/wlan0"
ESCAPE = re.compile(rb'\\(?:x([0-9a-fA-F]{2})|([\\"enrt]))')
ESCAPED = {b"\\": b"\\", b'"': b'"', b"e": b"\x1b", b"n": b"\n", b"r": b"\r", b"t": b"\t"}
LOG = logging.getLogger(__name__)


class WifiError(RuntimeError):
    pass


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int
    connected: bool
    security: str = field(default="open", compare=False)
    raw_ssid: bytes = field(default=b"", compare=False)


def parse_scan_results(results, connected_bssid):
    networks = {}
    for line in results.split(b"\n")[1:]:
        fields = line.split(b"\t", 4)
        if len(fields) != 5:
            continue
        bssid, _, signal, flags, ssid = fields
        try:
            signal = int(signal)
        except ValueError:
            continue
        raw_ssid = ESCAPE.sub(
            lambda match: bytes([int(match[1], 16)]) if match[1] else ESCAPED[match[2]], ssid
        )
        decoded = raw_ssid.decode("utf-8", errors="replace")
        if not any(char.isprintable() and not char.isspace() for char in decoded):
            continue
        protected = any(mark in flags for mark in (b"WPA", b"RSN", b"WEP", b"SAE", b"OWE", b"PRIVACY"))
        security = "psk" if b"-PSK" in flags else ("unsupported" if protected else "open")
        candidate = WifiNetwork(decoded, signal, bssid.lower() == connected_bssid.lower(), security, raw_ssid)
        known = networks.get(decoded)
        if known is None or (candidate.connected, candidate.signal) > (known.connected, known.signal):
            networks[decoded] = candidate
    return sorted(networks.values(), key=lambda row: (not row.connected, -row.signal, row.ssid.casefold()))


def _request(channel, command):
    channel.send(command)
    return channel.recv(65536)


@contextmanager
def _open_control(socket_path):
    with TemporaryDirectory(prefix="mks-wifi-") as directory, \
            socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.bind(str(Path(directory) / "ctrl"))
        channel.connect(socket_path)
        channel.settimeout(2)
        yield channel


def _status(control):
    return dict(line.split(b"=", 1) for line in _request(control, b"STATUS").splitlines() if b"=" in line)


def _wait_for_scan(channel, seconds=6):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        channel.settimeout(max(0.001, deadline - monotonic()))
        try:
            event = channel.recv(4096)
        except socket.timeout:
            return False
        if b"CTRL-EVENT-SCAN-RESULTS" in event:
            return True
        if b"CTRL-EVENT-SCAN-FAILED" in event:
            return False
    return False


def _scan(socket_path):
    with _open_control(socket_path) as control, _open_control(socket_path) as monitor:
        if _request(monitor, b"ATTACH").strip() != b"OK":
            raise WifiError("wpa_supplicant recusou a assinatura de eventos")
        try:
            response = _request(control, b"SCAN").strip()
            if response not in (b"OK", b"FAIL-BUSY"):
                raise WifiError("wpa_supplicant recusou a varredura")
            completed = _wait_for_scan(monitor)
            status = _status(control)
            connected = status.get(b"bssid", b"") if status.get(b"wpa_state") == b"COMPLETED" else b""
            result = _request(control, b"SCAN_RESULTS")
            if not result.startswith(b"bssid"):
                raise WifiError("wpa_supplicant nao retornou a lista de redes")
            networks = parse_scan_results(result, connected)
            if not completed and not networks:
                raise WifiError("varredura Wi-Fi sem resposta")
            return networks
        finally:
            # The monitor socket is about to close. Send DETACH without waiting on an event.
            try:
                monitor.send(b"DETACH")
            except OSError:
                pass


async def scan_wifi(socket_path=DEFAULT_SOCKET):
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _scan, socket_path)
    except OSError as exc:
        raise WifiError(str(exc)) from exc


def _credentials(network, password):
    ssid = network.raw_ssid or network.ssid.encode("utf-8")
    if not 1 <= len(ssid) <= 32:
        raise WifiError("SSID invalido")
    if network.security == "open" and not password:
        return {b"ssid": ssid.hex().encode("ascii"), b"key_mgmt": b"NONE"}
    if network.security != "psk":
        raise WifiError("seguranca Wi-Fi nao suportada")
    if re.fullmatch(rb"[0-9a-fA-F]{64}", password):
        psk = password
    elif 8 <= len(password) <= 63 and all(32 <= byte <= 126 for byte in password):
        psk = hashlib.pbkdf2_hmac("sha1", password, ssid, 4096, 32).hex().encode("ascii")
    else:
        raise WifiError("senha WPA invalida")
    return {b"ssid": ssid.hex().encode("ascii"), b"key_mgmt": b"WPA-PSK", b"psk": psk}


def _checked(control, command):
    if _request(control, command).strip() != b"OK":
        # Never include command bodies. SET_NETWORK psk contains a credential.
        raise WifiError("wpa_supplicant recusou " + command.split(b" ", 1)[0].decode("ascii"))


def _enabled_profiles(control):
    rows = _request(control, b"LIST_NETWORKS").splitlines()
    if not rows or not rows[0].startswith(b"network id"):
        raise WifiError("lista de perfis Wi-Fi indisponivel")
    return [fields[0] for row in rows[1:] for fields in [row.split(b"\t", 3)]
            if len(fields) == 4 and fields[0].isdigit() and b"[DISABLED]" not in fields[3]
            and b"[P2P-PERSISTENT]" not in fields[3]]


def _wait_for_address(control, network_id):
    deadline = monotonic() + 35
    while monotonic() < deadline:
        status = _status(control)
        if status.get(b"id") == network_id and status.get(b"wpa_state") == b"COMPLETED":
            try:
                address = IPv4Address(status.get(b"ip_address", b"").decode("ascii"))
                if not (address.is_link_local or address.is_loopback or address.is_unspecified):
                    return
            except (AddressValueError, UnicodeDecodeError):
                pass
        sleep(0.5)
    raise WifiError("a rede nao obteve um endereco IPv4")


def _connect(network, password, socket_path):
    settings = _credentials(network, password)
    with _open_control(socket_path) as control:
        enabled = _enabled_profiles(control)
        previous = _status(control).get(b"id")
        if _request(control, b"GET update_config").strip() != b"1":
            raise WifiError("wpa_supplicant nao permite salvar configuracao")
        priorities = [_request(control, b"GET_NETWORK " + old + b" priority").strip() for old in enabled]
        priorities = [int(value) for value in priorities if re.fullmatch(rb"-?\d+", value)]
        priority = min(max([0] + priorities) + 1, 2147483647)
        network_id = _request(control, b"ADD_NETWORK").strip()
        if not network_id.isdigit():
            raise WifiError("wpa_supplicant nao criou o perfil")
        selection_attempted = False
        try:
            settings[b"priority"] = str(priority).encode("ascii")
            for key, value in settings.items():
                _checked(control, b"SET_NETWORK " + network_id + b" " + key + b" " + value)
            selection_attempted = True
            _checked(control, b"SELECT_NETWORK " + network_id)
            _wait_for_address(control, network_id)
            for old in enabled:
                _checked(control, b"ENABLE_NETWORK " + old)
            _checked(control, b"SAVE_CONFIG")
        except (WifiError, OSError):
            restore = [b"REMOVE_NETWORK " + network_id]
            if selection_attempted:
                if previous in enabled:
                    restore.append(b"SELECT_NETWORK " + previous)
                restore.extend(b"ENABLE_NETWORK " + old for old in enabled)
            for command in restore:
                try:
                    _checked(control, command)
                except (WifiError, OSError):
                    LOG.warning("Falha ao restaurar operacao Wi-Fi: %s", command.split(b" ", 1)[0].decode("ascii"))
            raise


async def connect_wifi(network, password, socket_path=DEFAULT_SOCKET):
    try:
        await asyncio.get_running_loop().run_in_executor(None, _connect, network, password, socket_path)
    except OSError as exc:
        raise WifiError("comunicacao com wpa_supplicant falhou") from exc
