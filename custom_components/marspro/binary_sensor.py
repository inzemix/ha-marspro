"""Binary sensor platform for Mars Pro integration."""
import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
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
