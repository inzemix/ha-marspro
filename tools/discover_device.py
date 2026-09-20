#!/usr/bin/env python3
"""
Mars Pro / Mars Hydro — device discovery tool (zero dependency)
==============================================================

Read-only probe for Mars Hydro devices. It logs into the Mars Hydro cloud API,
lists every device on the account, then connects to the MQTT broker and asks
each device for its state.

**It never commands or changes anything.** Only read-only requests are sent
(`getDevSta`, `getSysSta`, `getConfigFile`) — no `setConfigField`. Safe to run
against a live grow setup.

**Nothing to install.** This script uses only the Python standard library, so
it runs anywhere Python 3.8+ is available — no `pip install`, no virtualenv.
(That matters: Home Assistant OS, Docker containers and many systems either
have no pip or refuse to install into the system Python.)

Why: this integration only creates entities for device types that have been
reverse-engineered. If your device does not appear in Home Assistant, run this
tool and share its output — that is how support for a new device gets added.

Usage
-----
    python3 discover_device.py

    # Windows (if python3 is not found):
    python discover_device.py

    # non-interactively:
    MARSPRO_EMAIL=you@example.com MARSPRO_PASSWORD=secret python3 discover_device.py

Do NOT run it on a Home Assistant server/appliance: run it on a normal computer
(Windows, macOS, Linux) that has Python. The script only needs internet access,
not the HA machine itself.

Options
-------
    --wait SECONDS   how long to listen for MQTT replies per device (default 12)
    --out FILE       also dump the raw payloads to a JSON file

Notes
-----
* The Mars Pro broker **rejects wildcard subscriptions** (`MHPRO/#` returns
  "Unspecified error"), so this tool subscribes to each device's exact topic.
* Credentials are only used in memory and are never printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request

REST_HOST = "mars-pro.api.lgledsolutions.com"
MQTT_HOST = "mars-pro.mqtt.lgledsolutions.com"
MQTT_PORT = 8883
LOGIN_PATH = "/api/android/ulogin/mailLogin/v1"
DEVICE_LIST_PATH = "/api/android/udm/getDeviceList/v1"
TOPIC_UP = "MHPRO/{model}/API/UP/{serial}"
TOPIC_DOWN = "MHPRO/{model}/API/DOWN/{serial}"
READONLY_METHODS = ("getDevSta", "getSysSta", "getConfigFile")


# --------------------------------------------------------------------------
# Minimal MQTT 3.1.1 client (standard library only)
# --------------------------------------------------------------------------
class MiniMQTT:
    """Just enough MQTT 3.1.1 to subscribe to a topic and publish a request."""

    CONNECT, CONNACK, PUBLISH, PUBACK = 0x10, 0x20, 0x30, 0x40
    SUBSCRIBE, SUBACK, PINGREQ, DISCONNECT = 0x80, 0x90, 0xC0, 0xE0

    def __init__(self, host: str, port: int, client_id: str,
                 username: str, password: str, keepalive: int = 60):
        self.host, self.port = host, port
        self.client_id, self.username, self.password = client_id, username, password
        self.keepalive = keepalive
        self.sock: ssl.SSLSocket | None = None
        self._buf = b""
        self._pid = 0

    # -- low level ---------------------------------------------------------
    @staticmethod
    def _encode_length(length: int) -> bytes:
        out = b""
        while True:
            byte = length % 128
            length //= 128
            if length:
                byte |= 0x80
            out += bytes([byte])
            if not length:
                return out

    @staticmethod
    def _encode_string(text: str) -> bytes:
        raw = text.encode()
        return struct.pack("!H", len(raw)) + raw

    def _send(self, packet_type: int, flags: int, payload: bytes) -> None:
        header = bytes([packet_type | flags]) + self._encode_length(len(payload))
        self.sock.sendall(header + payload)

    def _recv_packet(self, timeout: float) -> tuple[int, bytes] | None:
        """Read one MQTT packet. Returns (first_byte, payload).

        The full first byte is returned so callers can read the packet type
        (upper nibble) AND the flags (lower nibble, which carry the QoS level
        of a PUBLISH)."""
        self.sock.settimeout(timeout)
        while True:
            while len(self._buf) < 2:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("connection closed by broker")
                self._buf += chunk
            multiplier, length, index = 1, 0, 1
            while True:
                if index >= len(self._buf):
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("connection closed by broker")
                    self._buf += chunk
                byte = self._buf[index]
                length += (byte & 127) * multiplier
                multiplier *= 128
                index += 1
                if not byte & 0x80:
                    break
            total = index + length
            while len(self._buf) < total:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("connection closed by broker")
                self._buf += chunk
            first_byte = self._buf[0]
            payload = self._buf[index:total]
            self._buf = self._buf[total:]
            return first_byte, payload

    # -- public API --------------------------------------------------------
    def connect(self) -> int:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.host, self.port), timeout=30)
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        flags = 0x02  # clean session
        payload = self._encode_string(self.client_id)
        if self.username:
            flags |= 0x80
            payload += self._encode_string(self.username)
        if self.password:
            flags |= 0x40
            payload += self._encode_string(self.password)
        variable = self._encode_string("MQTT") + bytes([0x04, flags]) + struct.pack("!H", self.keepalive)
        self._send(self.CONNECT, 0, variable + payload)

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                packet = self._recv_packet(max(1.0, deadline - time.time()))
            except (socket.timeout, TimeoutError):
                continue
            if packet and (packet[0] & 0xF0) == self.CONNACK:
                return packet[1][1]  # return code
        raise TimeoutError("no CONNACK received")

    def subscribe(self, topic: str, qos: int = 0) -> bytes | None:
        self._pid += 1
        payload = struct.pack("!H", self._pid) + self._encode_string(topic) + bytes([qos])
        self._send(self.SUBSCRIBE, 0x02, payload)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                packet = self._recv_packet(max(1.0, deadline - time.time()))
            except (socket.timeout, TimeoutError):
                continue
            if packet and (packet[0] & 0xF0) == self.SUBACK:
                return packet[1]
        return None

    def publish(self, topic: str, message: str, qos: int = 1) -> None:
        body = self._encode_string(topic)
        if qos:
            self._pid += 1
            body += struct.pack("!H", self._pid)
        body += message.encode()
        self._send(self.PUBLISH, qos << 1, body)

    def ping(self) -> None:
        self._send(self.PINGREQ, 0, b"")

    def collect(self, seconds: float) -> list[tuple[str, str]]:
        """Listen for PUBLISH packets for `seconds`; return (topic, payload) pairs."""
        out: list[tuple[str, str]] = []
        end = time.time() + seconds
        last_ping = time.time()
        while time.time() < end:
            try:
                packet = self._recv_packet(min(1.0, max(0.1, end - time.time())))
            except (socket.timeout, TimeoutError):
                packet = None
            if packet:
                first_byte, payload = packet
                if (first_byte & 0xF0) == self.PUBLISH:
                    qos = (first_byte & 0x06) >> 1
                    topic_len = struct.unpack("!H", payload[:2])[0]
                    topic = payload[2:2 + topic_len].decode(errors="replace")
                    # a QoS>0 PUBLISH carries a 2-byte packet id after the topic
                    offset = 2 + topic_len + (2 if qos else 0)
                    out.append((topic, payload[offset:].decode(errors="replace")))
            if time.time() - last_ping > max(20, self.keepalive - 10):
                self.ping()
                last_ping = time.time()
        return out

    def close(self) -> None:
        try:
            self._send(self.DISCONNECT, 0, b"")
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Mars Hydro cloud API (standard library only)
# --------------------------------------------------------------------------
def systemdata(token: str | None = None) -> str:
    now = int(time.time() * 1000)
    header = {
        "reqId": now, "appVersion": "2.1.0", "osType": "android", "osVersion": "14",
        "deviceType": "sdk", "deviceId": "sdk", "netType": "wifi", "wifiName": "x",
        "timestamp": now, "language": "English",
    }
    if token:
        header["token"] = token
        header["timezone"] = "0"
    return json.dumps(header)


def post(path: str, body: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        f"https://{REST_HOST}{path}",
        json.dumps(body).encode(),
        {
            "Content-Type": "application/json",
            "User-Agent": "Dart/3.5 (dart:io)",
            "systemdata": systemdata(token),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def login(email: str, password: str) -> dict:
    resp = post(LOGIN_PATH, {"email": email, "password": password, "loginMethod": "1"})
    if resp.get("code") != "000":
        print(f"  ! Login rejected by Mars Hydro: {resp.get('msg')} (code {resp.get('code')})")
        sys.exit(2)
    return resp["data"]


def fetch_devices(token: str) -> list[dict]:
    devices, seen = [], set()
    for group in range(10):
        try:
            resp = post(DEVICE_LIST_PATH, {"currentPage": 1, "type": None,
                                           "deviceProductGroup": group}, token)
        except urllib.error.HTTPError as err:
            print(f"  ! group {group}: HTTP {err.code}")
            continue
        for d in (resp.get("data") or {}).get("list") or []:
            serial = d.get("deviceSerialnum")
            if not serial or serial in seen:
                continue
            seen.add(serial)
            ptype = d.get("productType", "") or ""
            devices.append({
                "name": d.get("deviceName", serial),
                "serial": serial,
                "productType": ptype,
                "model": ptype[3:] if ptype.startswith("MH-") else ptype,
                "firmware": d.get("deviceVersion"),
                "connected": d.get("connectStatus"),
                "group": group,
            })
    return devices


def probe_device(dev: dict, user: str, pwd: str, wait: int) -> dict:
    topic_up = TOPIC_UP.format(model=dev["model"], serial=dev["serial"])
    topic_down = TOPIC_DOWN.format(model=dev["model"], serial=dev["serial"])
    info = {"topic_up": topic_up, "topic_down": topic_down, "suback": None,
            "connected": False, "reply_code": None, "methods": [], "replies": {}}

    client = MiniMQTT(MQTT_HOST, MQTT_PORT, f"marspro-discover-{int(time.time())}", user, pwd)
    try:
        code = client.connect()
    except Exception as err:
        info["error"] = f"{type(err).__name__}: {err}"
        return info

    info["connected"] = True
    info["reply_code"] = code
    if code != 0:
        info["error"] = f"broker refused the connection (CONNACK code {code})"
        return info

    suback = client.subscribe(topic_up, qos=0)
    info["suback"] = suback.hex() if suback else None

    for method in READONLY_METHODS:
        client.publish(topic_down, json.dumps({"method": method,
                                               "params": {"pid": dev["serial"]}}), qos=1)
        time.sleep(0.3)

    for _, payload in client.collect(max(wait, 1)):
        try:
            parsed = json.loads(payload)
        except Exception:
            continue
        method = parsed.get("method", "unknown")
        if method in READONLY_METHODS and method not in info["replies"]:
            info["replies"][method] = parsed

    client.close()
    info["methods"] = sorted(info["replies"])
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="Mars Hydro device discovery (read-only, zero dependency)")
    ap.add_argument("--wait", type=int, default=12,
                    help="seconds to listen for MQTT replies per device (default 12)")
    ap.add_argument("--out", help="write raw payloads to this JSON file")
    args = ap.parse_args()

    email = os.environ.get("MARSPRO_EMAIL") or input("Mars Pro email: ").strip()
    password = os.environ.get("MARSPRO_PASSWORD") or getpass.getpass("Mars Pro password: ")

    print("\n[1/3] Logging in to the Mars Hydro cloud...")
    data = login(email, password)
    print("      OK (token + MQTT credentials received)")

    print("[2/3] Listing devices on the account...")
    devices = fetch_devices(data["token"])
    if not devices:
        print("      No device found. Is your device visible in the Mars Pro app?")
        return 1

    print(f"      {len(devices)} device(s) found:\n")
    for d in devices:
        print(f"        - {d['name']}")
        print(f"            productType : {d['productType']}   <-- the value that matters")
        print(f"            serial      : {d['serial']}")
        print(f"            model used  : {d['model']}  (MQTT topic MHPRO/{d['model']}/API/...)")
        print(f"            firmware    : {d['firmware']}  connected: {d['connected']}")

    print(f"\n[3/3] Probing MQTT ({args.wait}s per device, read-only)...")
    dump = {"devices": devices, "probes": {}}
    for d in devices:
        print(f"\n  === {d['name']}  ({d['productType']}) ===")
        info = probe_device(d, data["mqttName"], data["mqttPwd"], args.wait)
        dump["probes"][d["serial"]] = info

        if info.get("error"):
            print(f"    MQTT: FAILED — {info['error']}")
            continue
        print(f"    subscribed to {info['topic_up']}")
        if not info["methods"]:
            print("    No MQTT reply received.")
            print("    => this device does not answer on the cloud broker.")
            print("       It may be Bluetooth-only, or simply offline.")
            continue
        for method, payload in info["replies"].items():
            block = payload.get("data", {})
            blocks = ", ".join(sorted(block)) if isinstance(block, dict) else "?"
            print(f"    + {method}: replied, data blocks = [{blocks}]  "
                  f"({len(json.dumps(payload))} bytes)")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(dump, fh, indent=2)
        print(f"\nRaw payloads written to {args.out}")

    print("\n" + "=" * 72)
    print("Done. To help add support for your device, share in the GitHub issue:")
    print("  * the device list above (name + productType)")
    print("  * for each device: did it reply on MQTT? which data blocks?")
    print("  * if you used --out, the JSON file (review it: it contains your readings)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
