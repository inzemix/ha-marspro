"""REST API client for Mars Pro cloud."""
import json
import time
import urllib.request
from .const import REST_API_URL, LOGIN_PATH, DEVICE_LIST_PATH, APP_VERSION, OS_TYPE, OS_VERSION

class MarsProAPI:
    """Handles REST calls to Mars Pro cloud."""

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._token: str | None = None
        self._mqtt_user: str | None = None
        self._mqtt_pwd: str | None = None
        self._devices: list[dict] = []

    @property
    def mqtt_user(self) -> str:
        return self._mqtt_user

    @property
    def mqtt_pwd(self) -> str:
        return self._mqtt_pwd

    @property
    def devices(self) -> list[dict]:
        return self._devices

    def _systemdata(self) -> str:
        """Build the required systemdata JSON header."""
        now = int(time.time() * 1000)
        h = {
            "reqId": now, "appVersion": APP_VERSION, "osType": OS_TYPE,
            "osVersion": OS_VERSION, "deviceType": "sdk", "deviceId": "sdk",
            "netType": "wifi", "wifiName": "x", "timestamp": now,
            "language": "English",
        }
        if self._token:
            h["token"] = self._token
            h["timezone"] = "0"
        return json.dumps(h)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            REST_API_URL + path,
            json.dumps(body).encode(),
            {
                "Content-Type": "application/json",
                "User-Agent": "Dart/3.5 (dart:io)",
                "systemdata": self._systemdata(),
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def login(self) -> None:
        """Authenticate and retrieve token + MQTT credentials."""
        resp = self._post(LOGIN_PATH, {
            "email": self._email,
            "password": self._password,
            "loginMethod": "1",
        })
        if resp.get("code") != "000":
            raise AuthError(resp.get("msg", "Login failed"))
        data = resp["data"]
        self._token = data["token"]
        self._mqtt_user = data["mqttName"]
        self._mqtt_pwd = data["mqttPwd"]

    def fetch_devices(self) -> list[dict]:
        """Retrieve all devices from the account."""
        devices = []
        for g in range(10):
            resp = self._post(DEVICE_LIST_PATH, {
                "currentPage": 1,
                "type": None,
                "deviceProductGroup": g,
            })
            items = (resp.get("data") or {}).get("list") or []
            for d in items:
                serial = d.get("deviceSerialnum")
                ptype = d.get("productType", "")
                model = ptype.replace("MH-", "", 1) if ptype.startswith("MH-") else ptype
                devices.append({
                    "serial": serial,
                    "model": model,
                    "productType": ptype,
                    "name": d.get("deviceName", serial),
                    "id": d.get("id"),
                })
        self._devices = devices
        return devices


class AuthError(Exception):
    """Authentication failed."""
