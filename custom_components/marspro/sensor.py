"""Sensor platform for Mars Pro integration."""
import logging
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, SENSOR_FIELDS, SENSOR_CB43_EXTRA, SENSOR_NAMES, DEVICE_IHUB10, DEVICE_CB43

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Mars Pro sensors."""
    state = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for dev in state["devices"]:
        serial = dev["serial"]
        ptype = dev["productType"]
        # Only controllers (iHub, CB43) have sensors. Lights have none.
        if ptype not in (DEVICE_IHUB10, DEVICE_CB43):
            continue
        is_cb43 = ptype == DEVICE_CB43

        # Common sensors (sensor block)
        for field, (dev_class, unit, _) in SENSOR_FIELDS.get("sensor", {}).items():
            entities.append(MarsProSensor(state, serial, dev, "sensor", field, dev_class, unit))

        # Outlet sensors (power)
        for field, (dev_class, unit, _) in SENSOR_FIELDS.get("outlet", {}).items():
            entities.append(MarsProSensor(state, serial, dev, "outlet", field, dev_class, unit))

        # CB43 extra sensors (soil, ppfd)
        if is_cb43:
            for field, (dev_class, unit, _) in SENSOR_CB43_EXTRA.items():
                entities.append(MarsProSensor(state, serial, dev, "sensor", field, dev_class, unit))

    async_add_entities(entities)


class MarsProSensor(SensorEntity):
    """Sensor for Mars Pro device data."""

    def __init__(self, state: dict, serial: str, device_info: dict, block: str, field: str, dev_class, unit):
        self._state = state
        self._serial = serial
        self._block = block
        self._field = field
        self._attr_unique_id = f"marspro_{serial}_{SENSOR_NAMES.get(field, field).lower().replace(' ', '_')}"
        self._attr_name = f"{device_info['name']} {SENSOR_NAMES.get(field, field)}"
        self._attr_native_unit_of_measurement = unit
        if dev_class:
            self._attr_device_class = dev_class
        if field == "energy":
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        else:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            name=device_info["name"],
            model=device_info["productType"],
            manufacturer="Mars Hydro",
        )

    @property
    def native_value(self):
        data = self._state["live_data"].get(self._serial, {})
        devsta = data.get("getDevSta", {}).get("data", {})
        block_data = devsta.get(self._block, {})
        val = block_data.get(self._field)
        if val is not None and self._field == "energy":
            return round(val / 1000.0, 3)  # Wh → kWh
        return val

    @property
    def available(self):
        return self._serial in self._state["live_data"]
