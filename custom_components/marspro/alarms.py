"""Alarm semantics for Mars Pro devices.

Nothing here touches Home Assistant, the network or the filesystem: it decides
what a device frame *means*, so the logic can be exercised by doubles.

Two mechanisms exist in this ecosystem, and they are not equivalent:

* Some devices report a fault inside the actuator block concerned, as an
  `alarm` code (`alarm: 5` on a dehumidifier = water bucket full). That is a
  state: `alarm_snapshot()` reads it, and the binary sensor exposes it.
* An iHub Pro reports it too, but **only while the fault is active and not for
  long**: measured on real hardware (issue #5), the field was present for 6 s,
  24 s and about three hours across three faults, and absent from every
  steady-state frame. That is why a report taken minutes later shows nothing,
  and why polling alone is a race.
* Every device also carries a sticky `alarmLast` marker
  (`{id, epoch, devType}`) that changes when a new alarm is raised, without
  saying why. `AlarmTracker` turns a change of its id into exactly one event —
  that is the reliable trigger, independent of the transient field.
* The **cause** comes from a device-local call: `getAlarmLog` answers with
  `{"data": {"count": n, "list": [{"alarmType": 5, "devType": 26, ...}]}}`.
  `resolve_alarm_cause()` asks for it and enriches the event, so "water bucket
  full" can be stated without guessing.
"""

# Fault codes the device raises in an actuator's own `alarm` field. Documented
# by iClint/MarsHydroAPIDocs §3 (CB43, confirmed against physical state); the
# iHub Pro is expected to use the same codes if it ever reports them.
ALARM_MEANINGS = {
    "dehumidifier": {5: "Water bucket full"},
    "humidifier": {4: "Tank empty / needs refill"},
}

# Codes seen on other actuator types, kept separate so a code is never given a
# meaning it was not observed with.
GENERIC_MEANINGS = {3: "Device disconnected"}

# `alarmType` codes, as answered by the device itself in `getAlarmLog`. `5` was
# observed on three real bucket-full alarms (issue #5, iHub-10 Pro); `3` comes
# from the protocol notes (inline fan disconnected). Anything else stays
# unnamed rather than guessed.
ALARM_TYPE_MEANINGS = {
    3: "Device disconnected",
    5: "Water bucket full",
}

# Device types seen in an alarm record. `26` is the dehumidifier: observed on an
# iHub-10 Pro at the exact minute the vendor app pushed "tank full" (issue #5,
# 2026-11-03 16:54:57Z and 2026-11-04 13:38:56Z) and seen with earlier alarms of
# the same device; the protocol notes also recorded `devType 26` next to
# `alarmType 5` (water bucket full) on a CB43. `19` = inline fan, per the same
# notes.
#
# ⚠️ This says WHICH device, never WHY: an iHub's `alarmLast` carries no
# `alarmType` at all, so a full bucket and a defrost cycle are indistinguishable
# there. Never map this to "water full" without a cause field.
DEVICE_TYPE_HINTS: dict[int, str] = {19: "inline fan", 26: "dehumidifier"}


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


def alarm_text(block: str, code) -> str:
    """Human meaning of one fault code, or an honest fallback."""
    known = ALARM_MEANINGS.get(block, {})
    if code in known:
        return known[code]
    if code in GENERIC_MEANINGS:
        return GENERIC_MEANINGS[code]
    return f"Unknown fault code {code}"


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


def parse_alarm_log(reply, alarm_id=None) -> dict | None:
    """Extract the alarm record from a `getAlarmLog` answer.

    Observed shape (issue #5, iHub-10 Pro):
        {"code": 200, "data": {"count": 1, "list": [
            {"alarmType": 5, "devType": 26, "epoch": 1793724889, "id": 48}]}}

    The record matching `alarm_id` is preferred; otherwise the first one is
    used. Returns None when the device did not answer usefully — the caller
    then keeps the alarm without a cause rather than inventing one.
    """
    if not isinstance(reply, dict):
        return None
    if str(reply.get("code")) != "200":
        return None
    data = reply.get("data")
    records = data.get("list") if isinstance(data, dict) else None
    if not isinstance(records, list) or not records:
        return None
    record = None
    for candidate in records:
        if isinstance(candidate, dict) and candidate.get("id") == alarm_id:
            record = candidate
            break
    if record is None:
        first = records[0]
        record = first if isinstance(first, dict) else None
    if record is None:
        return None
    alarm_type = record.get("alarmType")
    return {
        "alarm_id": record.get("id"),
        "alarm_type": alarm_type,
        "dev_type": record.get("devType"),
        "epoch": record.get("epoch"),
        "cause": ALARM_TYPE_MEANINGS.get(alarm_type)
        if isinstance(alarm_type, int) else None,
    }


async def resolve_alarm_cause(mqtt, serial: str, model: str, payload: dict,
                              timeout: float = 3.0) -> dict:
    """Ask the hub for its own alarm record and enrich the event payload.

    `getAlarmLog` is a read-only request, answered by the device (no cloud feed
    involved — that one is empty on iHub accounts). It must never make the
    alarm event wait forever or fail: on any problem the original payload is
    returned unchanged, so the event still fires, just without a cause.
    """
    try:
        reply = await mqtt.request(serial, model, "getAlarmLog",
                                   {"pid": serial}, timeout=timeout)
    except Exception:  # noqa: BLE001 — an alarm must not be lost to a probe
        return payload
    detail = parse_alarm_log(reply, payload.get("alarm_id"))
    if not detail:
        return payload
    enriched = dict(payload)
    if detail.get("alarm_type") is not None:
        enriched["alarm_type"] = detail["alarm_type"]
    if detail.get("cause"):
        enriched["cause"] = detail["cause"]
    return enriched

