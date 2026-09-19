#!/usr/bin/env python3
"""
Mars Pro / Mars Hydro — device discovery tool
============================================

Read-only probe for Mars Hydro devices. It logs into the Mars Hydro cloud
API, lists every device on the account, then connects to the MQTT broker and
asks each device for its state.

**It never commands or changes anything.** Only read-only requests are sent
(`getDevSta`, `getSysSta`, `getConfigFile`) — no `setConfigField`. It is safe
to run against a live grow setup.

Why: this integration only creates entities for device types that have been
reverse-engineered. If your device does not appear in Home Assistant, run
this tool and share its output — that is how support for a new device gets
added.

Usage
-----
    pip install paho-mqtt
    python3 discover_device.py

    # or non-interactively:
    MARSPRO_EMAIL=you@example.com MARSPRO_PASSWORD=secret python3 discover_device.py

Options
-------
    --wait SECONDS   how long to listen for MQTT replies per device (default 12)
    --out FILE       also dump the raw payloads to a JSON file

Notes
-----
* The Mars Pro broker **rejects wildcard subscriptions** (`MHPRO/#` returns
  "Unspecified error"), so this tool subscribes to each device's exact topic.
* Credentials are only ever used in memory and are never printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
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
    """Devices are bucketed by deviceProductGroup; iterate groups 0..9."""
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
    """Subscribe to the device's exact topic and collect read-only replies."""
    import paho.mqtt.client as mqtt  # imported here for a cleaner error message

    topic_up = TOPIC_UP.format(model=dev["model"], serial=dev["serial"])
    topic_down = TOPIC_DOWN.format(model=dev["model"], serial=dev["serial"])
    replies: dict[str, dict] = {}
    info = {"topic_up": topic_up, "topic_down": topic_down, "suback": None,
            "connected": False, "methods": [], "replies": {}}

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            info["error"] = f"connect refused: {reason_code}"
            return
        info["connected"] = True
        client.subscribe(topic_up, qos=0)

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        info["suback"] = str(reason_codes[0]) if reason_codes else None

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except Exception:
            return
        method = payload.get("method", "unknown")
        if method in READONLY_METHODS:
            replies[method] = payload

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"marspro-discover-{int(time.time())}",
                         protocol=mqtt.MQTTv311)
    client.username_pw_set(user, pwd)
    client.tls_set_context(ctx)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, 30)
    except Exception as err:
        info["error"] = f"{type(err).__name__}: {err}"
        return info

    client.loop_start()
    time.sleep(2)
    for method in READONLY_METHODS:
        client.publish(topic_down, json.dumps({"method": method,
                                               "params": {"pid": dev["serial"]}}), qos=1)
        time.sleep(0.3)
    time.sleep(max(wait, 1))
    client.loop_stop()
    try:
        client.disconnect()
    except Exception:
        pass

    info["methods"] = sorted(replies)
    info["replies"] = replies
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="Mars Hydro device discovery (read-only)")
    ap.add_argument("--wait", type=int, default=12,
                    help="seconds to listen for MQTT replies per device (default 12)")
    ap.add_argument("--out", help="write raw payloads to this JSON file")
    args = ap.parse_args()

    email = os.environ.get("MARSPRO_EMAIL") or input("Mars Pro email: ").strip()
    password = os.environ.get("MARSPRO_PASSWORD") or getpass.getpass("Mars Pro password: ")

    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        print("Missing dependency. Install it with:  pip install paho-mqtt")
        return 1

    print("\n[1/3] Logging in to the Mars Hydro cloud...")
    data = login(email, password)
    token = data["token"]
    print("      OK (token + MQTT credentials received)")

    print("[2/3] Listing devices on the account...")
    devices = fetch_devices(token)
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
        print(f"    subscribed to {info['topic_up']}  -> SUBACK {info['suback']}")
        if not info["methods"]:
            print("    No MQTT reply received.")
            print("    => this device does not answer on the cloud broker.")
            print("       It may be Bluetooth-only, or simply offline.")
            continue
        for method, payload in info["replies"].items():
            data_block = payload.get("data", {})
            blocks = ", ".join(sorted(data_block)) if isinstance(data_block, dict) else "?"
            print(f"    + {method}: replied, data blocks = [{blocks}]  "
                  f"({len(json.dumps(payload))} bytes)")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(dump, fh, indent=2)
        print(f"\nRaw payloads written to {args.out}")

    print("\n" + "=" * 72)
    print("Done. To help add support for your device, share:")
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
