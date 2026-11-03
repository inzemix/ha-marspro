"""Diagnostic capture for MQTT messages and device faults.

Shipped in the 1.0.46 beta for issue #5 (dehumidifier tank-full alarm): the
cloud protocol documentation at iClint/MarsHydroAPIDocs states that the
dehumidifier reports `alarm: 5` (water bucket full) inside its own actuator
block of `getDevSta`, and that the broker **denies wildcard subscriptions** —
so an alarm is not a separate push message we could discover by listening
wider, it is a field of the frame we already poll every minute.

Two questions stay open on hardware we do not own, and this module records
what is needed to close them:

1. Does the iHub Pro report the same `alarm` field as the CB43? The snapshot
   is logged on every change, including the very first `{}` — which proves by
   itself that the field was absent at that moment.
2. Is any unrecognised `method` ever delivered on the UP topic? Only the first
   occurrence of each one is logged, with its raw payload.

No `homeassistant` import here on purpose: the decision logic is exercised by
doubles outside the container (see the release test for this version).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# Methods the integration already stores and understands.
HANDLED_METHODS = ("getDevSta", "getConfigFile", "getSysSta")

# Fault codes the device raises in an actuator's own `alarm` field. Documented
# by iClint/MarsHydroAPIDocs §3 (CB43, confirmed against physical state); the
# iHub Pro is expected to use the same codes, which is what we are verifying.
ALARM_MEANINGS = {
    "dehumidifier": {5: "Water bucket full"},
    "humidifier": {4: "Tank empty / needs refill"},
}

# Codes seen on other actuator types, kept separate so a code is never given a
# meaning it was not observed with.
GENERIC_MEANINGS = {3: "Device disconnected"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def serial_from_topic(topic: str) -> str | None:
    """Extract the serial from `MHPRO/{model}/API/UP/{serial}`."""
    parts = (topic or "").split("/")
    return parts[4] if len(parts) >= 5 else None


def alarm_snapshot(devsta_data) -> dict:
    """Return `{actuator_block: alarm_code}` for every block carrying `alarm`.

    Blocks without the key are omitted, and that absence is meaningful: the
    documented frames only carry `alarm` while there is a fault. Zero is kept
    when the device sends it, so "reported 0" and "never reported" stay
    distinguishable in the snapshot.
    """
    if not isinstance(devsta_data, dict):
        return {}
    snapshot = {}
    for block, value in devsta_data.items():
        if isinstance(value, dict) and "alarm" in value:
            code = value.get("alarm")
            if isinstance(code, bool) or isinstance(code, int):
                snapshot[block] = code
    return snapshot


def active_alarms(snapshot: dict) -> dict:
    """Codes that are actually raised (non-zero) in a snapshot."""
    return {block: code for block, code in snapshot.items() if code}


# Device types seen in an alarm record. `26` is the dehumidifier: observed on an
# iHub-10 Pro at the exact minute the vendor app pushed "tank full" (issue #5,
# 2026-11-03 16:54:57Z) and seen again with two earlier alarms of the same
# device; the protocol notes also recorded `devType 26` next to `alarmType 5`
# (water bucket full) on a CB43. `19` = inline fan, per the same notes.
#
# ⚠️ This says WHICH device, never WHY: an iHub's `alarmLast` carries no
# `alarmType` at all, so a full bucket and a defrost cycle are indistinguishable
# there. Never map this to "water full" without a cause field.
DEVICE_TYPE_HINTS: dict[int, str] = {19: "inline fan", 26: "dehumidifier"}


class AlarmTracker:
    """Turn the sticky `alarmLast` marker into exactly one event per new alarm.

    `alarmLast` persists in every frame and is only replaced by a newer alarm,
    so its mere presence means nothing. Only a change of its `id` is news.
    The first frame of a device sets the baseline: an alarm raised while Home
    Assistant was down (or weeks ago) must not fire a notification at startup —
    it stays visible in the binary sensor's `alarm_last` attribute instead.
    """

    def __init__(self):
        self._last: dict[str, object] = {}

    def observe(self, serial: str, devsta_data) -> dict | None:
        """Return an event payload when `alarmLast.id` changes, else None."""
        if not isinstance(devsta_data, dict):
            return None
        last = devsta_data.get("alarmLast")
        if not isinstance(last, dict):
            return None
        ident = last.get("id")
        if ident is None:
            return None
        if self._last.get(serial) == ident:
            return None
        baseline = serial not in self._last
        self._last[serial] = ident
        if baseline:
            return None
        dev_type = last.get("devType")
        hint = DEVICE_TYPE_HINTS.get(dev_type) if isinstance(dev_type, int) else None
        return {
            "serial": serial,
            "alarm_id": ident,
            "epoch": last.get("epoch"),
            "dev_type": dev_type,
            "alarm_type": last.get("alarmType"),
            "device": hint or "unknown",
        }


def alarm_text(block: str, code) -> str:
    """Human meaning of one fault code, or an honest fallback."""
    known = ALARM_MEANINGS.get(block, {})
    if code in known:
        return known[code]
    if code in GENERIC_MEANINGS:
        return GENERIC_MEANINGS[code]
    return f"Unknown fault code {code}"


class MessageCapture:
    """Bounded, de-duplicated record of what a device tells us.

    Every method returns a formatted line to append to the capture file, or
    None when there is nothing worth recording. The line is produced *after*
    the decision, so the file never claims something that did not happen.
    """

    def __init__(self, max_entries: int = 200, max_methods: int = 25):
        self.max_entries = max_entries
        self.max_methods = max_methods
        self._methods: dict[tuple, int] = {}
        self._last_alarms: dict[str, dict] = {}
        self._written = 0
        self.closed = False

    @property
    def written(self) -> int:
        return self._written

    def _line(self, text: str) -> str | None:
        if self.closed:
            return None
        self._written += 1
        if self._written > self.max_entries:
            self.closed = True
            return (
                f"{_now()} capture limit reached ({self.max_entries} entries) — "
                "send the file and stop here"
            )
        return f"{_now()} {text}"

    def note_message(self, topic: str, data) -> str | None:
        """Record a message whose method we do not handle (once per method)."""
        if self.closed or not isinstance(data, dict):
            return None
        method = data.get("method") or "<no method>"
        if method in HANDLED_METHODS:
            return None
        serial = data.get("pid") or serial_from_topic(topic)
        key = (serial, method)
        if key not in self._methods and len(self._methods) >= self.max_methods:
            return None
        count = self._methods.get(key, 0) + 1
        self._methods[key] = count
        if count == 1:
            return self._line(
                f"UNHANDLED method={method!r} serial={serial} topic={topic} "
                f"payload={_json(data)}"
            )
        if count % 10 == 0:
            return self._line(
                f"UNHANDLED method={method!r} serial={serial} seen {count}x"
            )
        return None

    def note_alarms(self, serial: str, snapshot: dict) -> str | None:
        """Record a device's fault snapshot whenever it changes."""
        if self.closed:
            return None
        previous = self._last_alarms.get(serial)
        self._last_alarms[serial] = snapshot
        if snapshot == previous:
            return None
        return self._line(f"ALARMS serial={serial} snapshot={_json(snapshot)}")


def append_line(path: str, line: str | None, max_bytes: int) -> bool:
    """Append one line to the capture file, bounded by `max_bytes`.

    Returns False when nothing was written. Never raises: a diagnostic aid must
    never take the integration down with it.
    """
    if not line:
        return False
    try:
        import os

        data = line.encode("utf-8") + b"\n"
        if len(data) > max_bytes:
            return False
        if os.path.exists(path) and os.path.getsize(path) + len(data) > max_bytes:
            return False
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return True
    except OSError as err:  # pragma: no cover - depends on the filesystem
        _LOGGER.debug("Mars Pro: could not write the capture file: %s", err)
        return False


async def _append_in_executor(hass, path: str, line: str, max_bytes: int) -> None:
    await hass.async_add_executor_job(append_line, path, line, max_bytes)


def schedule_append(hass, path: str, line: str | None, max_bytes: int) -> bool:
    """Schedule one capture line from a synchronous callback.

    `hass.async_create_task()` only accepts a **coroutine**
    (`target: Coroutine[Any, Any, _R]` in `homeassistant/core.py`), while
    `hass.async_add_executor_job()` returns a **Future**. Handing the Future to
    async_create_task raises `TypeError: a coroutine was expected, got <Future>`
    *inside the MQTT callback* — which in 1.0.46b1 broke `on_mqtt_message` for
    every incoming message and left all entities unavailable. The executor job
    is therefore awaited inside a coroutine, and that coroutine is scheduled.

    Returns False when there is nothing to write.
    """
    if not line:
        return False
    hass.async_create_task(_append_in_executor(hass, path, line, max_bytes))
    return True
