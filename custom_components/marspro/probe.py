"""Read-only device probing for the Mars Pro integration.

When Home Assistant reports a device that this integration does not support yet,
there is nothing to display and nothing to control — and no way for a user to
tell us what the device actually exposes. This module fills that gap: it asks
the device for its state and packs everything needed to add support into a
report the user can paste into a GitHub issue.

**Strictly read-only.** Only `getDevSta`, `getSysSta` and `getConfigFile` are
sent. No `setConfigField`, ever: this runs against hardware we do not
understand yet, so it must never command anything.

The MQTT layer is implemented on the Python standard library (socket + ssl).
That is deliberate: adding a dependency for a diagnostic feature would be a
poor trade, and Home Assistant already ships everything needed.
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import time
from typing import Any

MQTT_PORT = 8883
TOPIC_UP = "MHPRO/{model}/API/UP/{serial}"
TOPIC_DOWN = "MHPRO/{model}/API/DOWN/{serial}"

# Read-only requests only. Keep this list short and explicitly harmless.
READONLY_METHODS = ("getDevSta", "getSysSta", "getConfigFile")


class MiniMQTT:
    """Minimal MQTT 3.1.1 client: connect, subscribe, publish QoS1, collect."""

    CONNECT, CONNACK, PUBLISH, SUBSCRIBE, SUBACK = 0x10, 0x20, 0x30, 0x80, 0x90
    PINGREQ, DISCONNECT = 0xC0, 0xE0

    def __init__(self, host: str, port: int, client_id: str, username: str,
                 password: str, keepalive: int = 60) -> None:
        self.host, self.port = host, port
        self.client_id, self.username, self.password = client_id, username, password
        self.keepalive = keepalive
        self.sock = None
        self._buf = b""
        self._pid = 0

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
        self.sock.sendall(bytes([packet_type | flags]) + self._encode_length(len(payload)) + payload)

    def _recv_packet(self, timeout: float) -> tuple[int, bytes]:
        """Read one MQTT packet as (first_byte, payload).

        The whole first byte is returned so callers get the packet type (upper
        nibble) and the flags (lower nibble, which carry a PUBLISH's QoS).
        """
        self.sock.settimeout(timeout)
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
        variable = (self._encode_string("MQTT") + bytes([0x04, flags])
                    + struct.pack("!H", self.keepalive))
        self._send(self.CONNECT, 0, variable + payload)

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                packet = self._recv_packet(max(1.0, deadline - time.time()))
            except (socket.timeout, TimeoutError):
                continue
            if packet and (packet[0] & 0xF0) == self.CONNACK:
                return packet[1][1]
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

    def collect(self, seconds: float) -> list[str]:
        """Listen for PUBLISH payloads for `seconds`, return them as strings."""
        out: list[str] = []
        end = time.time() + seconds
        last_ping = time.time()
        while time.time() < end:
            try:
                first_byte, payload = self._recv_packet(min(1.0, max(0.1, end - time.time())))
            except (socket.timeout, TimeoutError):
                first_byte, payload = 0, b""
            if (first_byte & 0xF0) == self.PUBLISH:
                qos = (first_byte & 0x06) >> 1
                topic_len = struct.unpack("!H", payload[:2])[0]
                offset = 2 + topic_len + (2 if qos else 0)
                out.append(payload[offset:].decode(errors="replace"))
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


def probe_device(host: str, user: str, password: str, device: dict[str, Any],
                 wait: int = 12) -> dict[str, Any]:
    """Ask a single device for its state. Never sends a command."""
    model = device.get("model") or device.get("productType", "")
    serial = device.get("serial", "")
    result: dict[str, Any] = {
        # only the fields a maintainer needs: no account-level ids in the report
        "device": {key: device.get(key) for key in
                   ("name", "serial", "productType", "model", "firmware", "connected")},
        "topic_up": TOPIC_UP.format(model=model, serial=serial),
        "topic_down": TOPIC_DOWN.format(model=model, serial=serial),
        "connected": False,
        "replies": {},
        "error": None,
    }

    client = MiniMQTT(host, MQTT_PORT, f"hapro-report-{int(time.time())}", user, password)
    try:
        code = client.connect()
    except Exception as err:  # noqa: BLE001 - reported to the user
        result["error"] = f"connect failed: {type(err).__name__}: {err}"
        return result

    result["connected"] = True
    if code != 0:
        result["error"] = f"broker refused the connection (CONNACK code {code})"
        return result

    if client.subscribe(result["topic_up"], qos=0) is None:
        result["error"] = "no SUBACK from the broker"
        return result

    for method in READONLY_METHODS:
        client.publish(result["topic_down"],
                       json.dumps({"method": method, "params": {"pid": serial}}), qos=1)
        time.sleep(0.3)

    try:
        for raw in client.collect(max(wait, 1)):
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            method = parsed.get("method", "")
            if method in READONLY_METHODS and method not in result["replies"]:
                result["replies"][method] = parsed
    except Exception as err:  # noqa: BLE001
        result["error"] = f"collect failed: {type(err).__name__}: {err}"
    finally:
        client.close()

    return result


def summarise_block(method: str, payload: dict[str, Any]) -> list[str]:
    """Human-readable one-liner(s) describing what a device replied."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return ["      (no data block)"]
    lines = []
    for block, content in data.items():
        if isinstance(content, dict):
            keys = ", ".join(sorted(content))
            lines.append(f"      {block}: {keys}")
        else:
            lines.append(f"      {block}: {content!r}")
    return lines or ["      (empty data block)"]


def build_report(devices: list[dict[str, Any]], probes: dict[str, dict[str, Any]],
                 supported_types: tuple[str, ...], integration_version: str,
                 host: str, ha_version: str = "unknown", generated_at: str = "") -> str:
    """Assemble the text report a user pastes into a GitHub issue."""
    lines: list[str] = []
    add = lines.append

    add("Mars Pro — device support report")
    add("=" * 72)
    add(f"Generated        : {generated_at}")
    add(f"Integration      : marspro {integration_version}")
    add(f"Home Assistant   : {ha_version}")
    add(f"MQTT broker      : {host}:{MQTT_PORT}")
    add(f"Supported types  : {', '.join(supported_types)}")
    add("")
    add("This report is read-only: only getDevSta / getSysSta / getConfigFile were")
    add("sent. No command was issued to any device. Credentials are NOT included.")
    add("")
    add("Paste this whole report into the GitHub issue:")
    add("https://github.com/inzemix/ha-marspro/issues")
    add("")

    for device in devices:
        serial = device.get("serial", "")
        probe = probes.get(serial, {})
        add("-" * 72)
        add(f"DEVICE          : {device.get('name')}")
        add(f"productType     : {device.get('productType')}   <-- the key value")
        add(f"MQTT model      : {device.get('model')}  (topic MHPRO/{device.get('model')}/API/...)")
        add(f"serial          : {serial}")

        # the device list rarely carries the firmware; getSysSta does
        sys_reply = probe.get("replies", {}).get("getSysSta", {})
        sys_block = sys_reply.get("data", {}).get("sys", {}) if isinstance(
            sys_reply.get("data"), dict
        ) else {}
        firmware = device.get("firmware") or sys_block.get("ver") or "not reported"
        hardware = sys_block.get("hwver")
        add(f"firmware        : {firmware}"
            + (f"   (hardware revision: {hardware})" if hardware else ""))
        add(f"supported       : {'yes' if device.get('productType') in supported_types else 'NO'}")

        if probe.get("error"):
            add(f"MQTT probe      : FAILED — {probe['error']}")
        elif not probe.get("replies"):
            add("MQTT probe      : connected and subscribed, but the device never")
            add("                  answered any request. It most likely communicates")
            add("                  over Bluetooth only and exposes nothing on the")
            add("                  cloud broker — such a device cannot be supported")
            add("                  through this integration.")
        else:
            add(f"MQTT probe      : OK — replied to {', '.join(sorted(probe['replies']))}")
            for method, payload in probe["replies"].items():
                add(f"    {method}:")
                lines.extend(summarise_block(method, payload))
        add("")

    add("-" * 72)
    add("RAW PAYLOADS (full JSON, for the maintainer)")
    add(json.dumps({d.get("serial"): probes.get(d.get("serial", ""), {})
                    for d in devices}, indent=2, sort_keys=True))
    return "\n".join(lines)
