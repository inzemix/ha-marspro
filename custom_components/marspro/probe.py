"""Device probing for the Mars Pro integration.

When Home Assistant reports a device that this integration does not support yet,
there is nothing to display and nothing to control — and no way for a user to
tell us what the device actually exposes. This module fills that gap: it asks
the device for its state and packs everything needed to add support into a
report the user can paste into a GitHub issue.

**Read-only by default.** Only `getDevSta`, `getSysSta` and `getConfigFile` are
sent. An optional write probe (`test_writes=True`) rewrites the *current* values
of the actuators the device reports about itself, so nothing changes state — it
exists so a maintainer can see whether the device accepts commands, without
asking the volunteer for a second round trip.

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

# Read-only requests, sent first and always. Keep this list short and harmless.
READONLY_METHODS = ("getDevSta", "getSysSta", "getConfigFile")

# Actuator blocks known to exist on this ecosystem. A device reports the subset
# it has; each one becomes one Home Assistant entity.
ACTUATOR_HINTS = {
    "light": "light entity (on/off + brightness 0-100 via mLevel)",
    "light2": "light entity (second dimmer channel)",
    "blower": "fan entity (inline blower)",
    "fan": "fan entity (oscillating fan)",
    "heater": "switch entity",
    "humidifier": "switch entity",
    "dehumidifier": "switch entity",
    "watering": "switch entity",
    "device1": "switch entity (generic outlet 1)",
    "device2": "switch entity (generic outlet 2)",
    "outlet": "power data + the global master switch (outlet.masterOn)",
}

# Example of a command, kept here so the report is self-contained: this is
# exactly what the integration publishes for a supported device.
WRITE_EXAMPLE = (
    '{"method": "setConfigField", "params": {"pid": "<serial>",\n'
    '     "keyPath": ["device", "light"], "light": {"mOnOff": 1}}}\n'
    '     (brightness: {"mLevel": 0-100}, master: keyPath ["outlet"],\n'
    '      {"outlet": {"masterOn": 1}})'
)


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


def _echo_payloads(serial: str, config_reply: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Build harmless `setConfigField` payloads that rewrite the current values.

    Only the two writable fields of this ecosystem are echoed (`mOnOff`,
    `mLevel`), and only for blocks the device itself reports — so a device that
    accepts the command will end up in exactly the state it was already in.

    The global master (`outlet.masterOn`) is deliberately NOT touched: it is a
    recovery mechanism, not an actuator to poke at.
    """
    data = config_reply.get("data")
    config_file = data.get("configFile") if isinstance(data, dict) else None
    device_blocks = config_file.get("device") if isinstance(config_file, dict) else None
    if not isinstance(device_blocks, dict):
        return []

    out: list[tuple[str, str, dict[str, Any]]] = []
    for block, fields in sorted(device_blocks.items()):
        if not isinstance(fields, dict):
            continue
        echo = {k: fields[k] for k in ("mOnOff", "mLevel") if k in fields}
        if not echo:
            continue
        payload = {"method": "setConfigField",
                   "params": {"pid": serial, "keyPath": ["device", block], block: echo}}
        out.append((block, json.dumps(payload), payload))
    return out


def probe_device(host: str, user: str, password: str, device: dict[str, Any],
                 wait: int = 12, test_writes: bool = False) -> dict[str, Any]:
    """Ask a single device for its state.

    Read-only unless `test_writes` is set, in which case the current values of
    the reported actuators are written back unchanged (see `_echo_payloads`).
    """
    model = device.get("model") or device.get("productType", "")
    serial = device.get("serial", "")
    result: dict[str, Any] = {
        # only the fields a maintainer needs: no account-level ids in the report
        "device": {key: device.get(key) for key in
                   ("name", "serial", "productType", "model", "firmware", "connected")},
        "topic_up": TOPIC_UP.format(model=model, serial=serial),
        "topic_down": TOPIC_DOWN.format(model=model, serial=serial),
        "connected": False,
        "requests": {},
        "replies": {},
        "not_answered": [],
        "write_tests": [],
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
        request = {"method": method, "params": {"pid": serial}}
        result["requests"][method] = json.dumps(request)
        client.publish(result["topic_down"], json.dumps(request), qos=1)
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

    result["not_answered"] = [m for m in READONLY_METHODS if m not in result["replies"]]

    # Optional: prove whether the device accepts commands, without changing
    # anything — every value sent is the value the device just reported.
    if test_writes and result["replies"].get("getConfigFile"):
        try:
            for block, sent, _ in _echo_payloads(serial, result["replies"]["getConfigFile"]):
                client.publish(result["topic_down"], sent, qos=1)
                result["write_tests"].append({"block": block, "sent": sent, "reply": None})
                time.sleep(0.3)
            for raw in client.collect(max(wait, 1)):
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    continue
                if parsed.get("method") == "setConfigField":
                    for test in result["write_tests"]:
                        if test["reply"] is None:
                            test["reply"] = parsed
                            break
        except Exception as err:  # noqa: BLE001
            result["error"] = (result["error"] or "") + f" write probe failed: {err}"

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


def detected_actuators(probe: dict[str, Any]) -> list[tuple[str, str]]:
    """Actuator blocks this device reports, with what each one would become.

    This is the "how do I turn this into entities" answer: every block listed
    here is one Home Assistant entity, driven through `setConfigField`.
    """
    blocks: set[str] = set()
    dev_reply = probe.get("replies", {}).get("getDevSta", {})
    if isinstance(dev_reply.get("data"), dict):
        blocks.update(dev_reply["data"].keys())
    config_reply = probe.get("replies", {}).get("getConfigFile", {})
    config_data = config_reply.get("data")
    if isinstance(config_data, dict):
        config_file = config_data.get("configFile")
        if isinstance(config_file, dict) and isinstance(config_file.get("device"), dict):
            blocks.update(config_file["device"].keys())

    out = []
    for block in sorted(blocks):
        if block in ACTUATOR_HINTS:
            out.append((block, ACTUATOR_HINTS[block]))
    return out


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
    add("Only getDevSta / getSysSta / getConfigFile are sent by default: no")
    add("command is issued and no configuration is written. If the write probe")
    add("was enabled, it only rewrites the values the device just reported, so")
    add("nothing changes state. Credentials are NOT included in this report.")
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
            if probe.get("not_answered"):
                add(f"                  no reply from: {', '.join(probe['not_answered'])}")
            for method, payload in probe["replies"].items():
                add(f"    {method}:")
                lines.extend(summarise_block(method, payload))

            actuators = detected_actuators(probe)
            if actuators:
                add("    ACTUATORS DETECTED (each one becomes a Home Assistant entity):")
                for block, what in actuators:
                    add(f"      {block:<14} -> {what}")
                add("      Commands use setConfigField, same MQTT topics:")
                for line in WRITE_EXAMPLE.splitlines():
                    add(f"      {line}")

            if probe.get("write_tests"):
                add("    WRITE PROBE (values rewritten unchanged, nothing switched):")
                for test in probe["write_tests"]:
                    reply = test.get("reply")
                    verdict = "no reply to the command"
                    if isinstance(reply, dict):
                        verdict = f"code={reply.get('code')} msg={reply.get('msg')!r}"
                    add(f"      {test['block']:<14} -> {verdict}")

        if probe.get("requests"):
            add("    REQUESTS SENT (exact payloads, topic MHPRO/.../API/DOWN/<serial>):")
            for method in sorted(probe["requests"]):
                add(f"      {probe['requests'][method]}")
        add("")

    add("-" * 72)
    add("WHAT WE STILL NEED FROM YOU (please answer in the issue)")
    add("  1. What is plugged into each outlet of this controller (light, fan,")
    add("     humidifier, pump…), and what did the Mars Pro app let you control?")
    add("  2. Does the app offer a brightness/dimming slider or a 0-10V / RJ12")
    add("     option for this device?")
    add("  3. Anything the app can do that this report does not show (schedules,")
    add("     alarms, calibration…)?")
    add("")
    add("-" * 72)
    add("RAW PAYLOADS (full JSON, for the maintainer)")
    add(json.dumps({d.get("serial"): probes.get(d.get("serial", ""), {})
                    for d in devices}, indent=2, sort_keys=True))
    return "\n".join(lines)
