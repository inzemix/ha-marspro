"""Binary sensor platform for Mars Pro integration."""
import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from .alarms import active_alarms, alarm_text
from .const import DOMAIN, DEVICE_IHUB10, DEVICE_CB43

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensors."""
    state = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for dev in state["devices"]:
        serial = dev["serial"]
        ptype = dev["productType"]
        if ptype not in (DEVICE_IHUB10, DEVICE_CB43):
            continue
        entities.extend([
            MarsProBinarySensor(state, serial, dev, "isDaySensor", "Day Sensor", BinarySensorDeviceClass.LIGHT),
            MarsProBinarySensor(state, serial, dev, "connectivity", "Connected", BinarySensorDeviceClass.CONNECTIVITY),
            MarsProAlarmBinarySensor(state, serial, dev),
        ])

    async_add_entities(entities)


class MarsProBinarySensor(BinarySensorEntity):
    """Binary sensor (day/night, connectivity)."""

    def __init__(self, state: dict, serial: str, device_info: dict, field: str, label: str, dev_class):
        self._state = state
        self._serial = serial
        self._field = field
        self._attr_unique_id = f"marspro_{serial}_{field}"
        self._attr_name = f"{device_info['name']} {label}"
        self._attr_device_class = dev_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device_info["name"],
            model=device_info["productType"],
            manufacturer="Mars Hydro",
        )

    @property
    def is_on(self):
        if self._field == "isDaySensor":
            data = self._state["live_data"].get(self._serial, {})
            devsta = data.get("getDevSta", {}).get("data", {})
            sensor = devsta.get("sensor", {})
            return bool(sensor.get("isDaySensor", 0))
        if self._field == "connectivity":
            return self._serial in self._state["live_data"]
        return False

    @property
    def available(self):
        return True


class MarsProAlarmBinarySensor(BinarySensorEntity):
    """Fault raised by the device itself, in an actuator's `alarm` field.

    Documented for the CB43 by iClint/MarsHydroAPIDocs §3: the dehumidifier
    reports `alarm: 5` when its water bucket is full, the humidifier `alarm: 4`
    when its tank is empty. The field is read from every actuator block rather
    than a hardcoded list, so a fault raised by a block we have never seen
    still shows up.

    Semantics, stated honestly: `off` means "no fault field is currently
    raised". The attribute `alarms` carries the raw snapshot (a zero is kept if
    the device sends one) and `alarm_last` the frame's `alarmLast` marker, so
    "no fault" can never be confused with "this device does not report faults".
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, state: dict, serial: str, device_info: dict):
        self._state = state
        self._serial = serial
        self._attr_unique_id = f"marspro_{serial}_alarm"
        self._attr_name = f"{device_info['name']} Alarm"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device_info["name"],
            model=device_info["productType"],
            manufacturer="Mars Hydro",
        )

    @property
    def _devsta(self) -> dict:
        data = self._state["live_data"].get(self._serial, {})
        return data.get("getDevSta", {}).get("data", {}) or {}

    @property
    def _snapshot(self) -> dict:
        return self._state.get("alarms", {}).get(self._serial, {})

    @property
    def is_on(self) -> bool:
        return bool(active_alarms(self._snapshot))

    @property
    def extra_state_attributes(self) -> dict:
        snapshot = self._snapshot
        active = active_alarms(snapshot)
        attributes = {
            "alarms": snapshot,
            "active": {
                block: {"code": code, "meaning": alarm_text(block, code)}
                for block, code in active.items()
            },
            # Some devices report the field only while the fault is active (an
            # iHub Pro: measured 6 s to ~3 h), so this flag says whether it has
            # ever been seen — an "off" before that is genuinely unknown, not
            # an all-clear.
            "reports_alarm_field": bool(
                self._state.get("alarm_field_seen", {}).get(self._serial)
            ),
        }
        alarm_last = self._devsta.get("alarmLast")
        if isinstance(alarm_last, dict):
            attributes["alarm_last"] = alarm_last
        return attributes

    @property
    def available(self) -> bool:
        return self._serial in self._state["live_data"]

