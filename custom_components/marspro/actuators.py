"""Read-modify-write helpers for actuator commands.

The Mars Pro broker **replaces** the object at ``keyPath`` instead of merging it
into the stored configuration: any field absent from the command is removed from
the device. Measured on an iHub Pro on 2026-10-02 — turning a light off from
Home Assistant (which sent only ``{"mLevel": 0}``) deleted ``mOnOff`` from
``config.device.light``.

Consequence: a command must carry the **whole** actuator object. We read the
device configuration, apply only the intended change, and write the complete
object back. Fields the user never touched — ``modeType``, ``timePeriod``, speed
limits — then survive instead of being silently dropped.

When the device does not answer the configuration read in time we fall back to
the previous partial write rather than dropping the command: same observable
behaviour as before, minus the protection.
"""
import logging

_LOGGER = logging.getLogger(__name__)

# How long to wait for the device's getConfigFile reply before writing anyway.
CONFIG_READ_TIMEOUT = 3.0

# The hub acknowledges a `setOutlet` in ~5 s (measured), and answers a
# `getDevSta` just as fast. Both are slower than a config read.
OUTLET_ACK_TIMEOUT = 10.0
STATE_READ_TIMEOUT = 6.0

# How long the relay takes to follow the command (327 W -> 0 W took ~10 s).
RELAY_SETTLE_DELAY = 10.0


def _config_root(reply: dict | None) -> dict:
    """Return the ``configFile`` mapping carried by a getConfigFile reply."""
    if not isinstance(reply, dict):
        return {}
    data = reply.get("data")
    if not isinstance(data, dict):
        return {}
    root = data.get("configFile", data)
    return root if isinstance(root, dict) else {}


def _object_at(reply: dict | None, key_path: list) -> dict | None:
    """Follow ``key_path`` into a configFile reply and copy the object found."""
    node = _config_root(reply)
    for key in key_path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return dict(node) if isinstance(node, dict) else None


def _cached_reply(state: dict, serial: str) -> dict | None:
    """Last getConfigFile message kept in memory for this device, if any."""
    live = state.get("live_data", {}).get(serial, {})
    reply = live.get("getConfigFile")
    return reply if isinstance(reply, dict) else None


async def async_set_config_field(hass, state: dict, serial: str, model: str,
                                 key_path: list, changes: dict | None = None,
                                 drop: tuple = (), refresh: bool = True) -> bool:
    """Write ``changes`` at ``keyPath`` while preserving every other field.

    ``drop`` names fields to remove, which is how "off" is expressed on this
    platform (a switched-off actuator has no ``mOnOff`` key at all).

    Returns True when the command was published, False when there is no
    connection to publish on.
    """
    mqtt = state.get("mqtt")
    if mqtt is None:
        return False

    # 1. Read the live object. The device is the only source of truth: a cached
    #    copy can be stale if the value was changed from the vendor app.
    current = None
    if refresh:
        reply = await mqtt.request(serial, model, "getConfigFile", {"pid": serial},
                                   timeout=CONFIG_READ_TIMEOUT)
        current = _object_at(reply, key_path)

    # 2. Fall back to the copy kept in memory.
    if current is None:
        current = _object_at(_cached_reply(state, serial), key_path)

    # 3. Last resort: behave exactly as before (partial write) rather than
    #    refuse the command. The user asked for something; do it.
    if current is None:
        _LOGGER.warning(
            "Mars Pro: no configuration read from %s (keyPath=%s) — sending a "
            "partial command, which may drop fields held by the device.",
            serial, "/".join(str(k) for k in key_path),
        )
        current = {}

    payload_object = dict(current)
    payload_object.update(changes or {})
    for field in drop:
        payload_object.pop(field, None)

    command = {
        "pid": serial,
        "keyPath": list(key_path),
        key_path[-1]: payload_object,
    }
    await hass.async_add_executor_job(
        mqtt.publish, serial, model, "setConfigField", command
    )
    return True


async def async_set_outlet(hass, state: dict, serial: str, model: str,
                           on: bool, num: int = 0) -> bool:
    """Switch one of the hub's outlets — index 0 being the global master.

    The global master is **not** a configuration field. `outlet.masterOn` is
    read-only in the vendor app: its deserialiser stores the value and nothing
    ever writes it back, so a `setConfigField` on `["outlet"]` is accepted by
    the broker and silently ignored by the device. That is why the master
    switch never worked.

    The verified command is `setOutlet`, carrying an outlet index and the
    desired state. Measured on an iHub Pro (2026-10-26): the hub answers
    `code: 200` within ~5 s, `masterOn` flips in the same reply, and the relay
    itself follows ~5-10 s later (327 W → 0 W).

    Returns True when the command was published, False when there is no
    connection to publish on.
    """
    mqtt = state.get("mqtt")
    if mqtt is None:
        return False

    desired = 1 if on else 0
    reply = await mqtt.request(
        serial, model, "setOutlet",
        {"pid": serial, "num": num, "on": desired},
        timeout=OUTLET_ACK_TIMEOUT,
    )
    if reply is None:
        _LOGGER.warning(
            "Mars Pro: no setOutlet acknowledgement from %s within %.0fs "
            "(outlet %s -> %s) — the command may still have been applied.",
            serial, OUTLET_ACK_TIMEOUT, num, desired,
        )
        return True

    code = reply.get("code")
    if code not in (200, "200"):
        _LOGGER.warning("Mars Pro: %s refused setOutlet (outlet %s -> %s): code=%s msg=%s",
                        serial, num, desired, code, reply.get("msg"))
        return True

    # The acknowledgement already carries the new `masterOn`, but the entity
    # reads its state from the last getDevSta. Read it back so the switch does
    # not stay stuck on its old position until the next poll.
    await mqtt.request(serial, model, "getDevSta", {"pid": serial},
                       timeout=STATE_READ_TIMEOUT)

    # The relay itself closes a few seconds later, and the power sensor is only
    # refreshed by the 60 s poll: read the state once more after the settle
    # delay, so the consumption does not sit on its old value for a minute.
    if hass is not None:
        def _late_refresh() -> None:
            hass.async_create_task(
                mqtt.request(serial, model, "getDevSta", {"pid": serial},
                             timeout=STATE_READ_TIMEOUT)
            )

        hass.loop.call_later(RELAY_SETTLE_DELAY, _late_refresh)
    return True
