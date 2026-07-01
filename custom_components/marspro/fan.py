"""Fan platform for Mars Pro integration."""
import json
import logging
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, ACTUATORS_IHUB10, ACTUATORS_CB43, DEVICE_IHUB10, DEVICE_CB43

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Mars Pro fans."""
    state = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for dev in state["devices"]:
        ptype = dev["productType"]
        actuators = ACTUATORS_IHUB10 if ptype == DEVICE_IHUB10 else ACTUATORS_CB43
        for act_name, (domain, label) in actuators.items():
            if domain == "fan":
                entities.append(MarsProFan(state, dev, act_name, label))

    async_add_entities(entities)


class MarsProFan(FanEntity):
    """Fan (inline blower, oscillating)."""

    _attr_supported_features = FanEntityFeature.SET_SPEED

    def __init__(self, state: dict, device_info: dict, actuator: str, label: str):
        self._state = state
        self._serial = device_info["serial"]
        self._model = device_info["model"]
        self._actuator = actuator
        self._attr_unique_id = f"marspro_{self._serial}_{actuator}_fan"
        self._attr_name = f"{device_info['name']} {label}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device_info["name"],
            model=device_info["productType"],
            manufacturer="Mars Hydro",
        )

    @property
    def is_on(self):
        data = self._state["live_data"].get(self._serial, {})
        devsta = data.get("getDevSta", {}).get("data", {})
        act = devsta.get(self._actuator, {})
        return bool(act.get("on", 0))

    @property
    def percentage(self):
        data = self._state["live_data"].get(self._serial, {})
        devsta = data.get("getDevSta", {}).get("data", {})
        act = devsta.get(self._actuator, {})
        return int(act.get("level", 0))

    @property
    def speed_count(self):
        return 10 if "fan" in self._actuator else 100

    @property
    def available(self):
        return self._serial in self._state["live_data"]

    async def async_turn_on(self, percentage=None, **kwargs):
        level = percentage if percentage is not None else 50
        mqtt = self._state.get("mqtt")
        if mqtt:
            await self._hass.async_add_executor_job(
                mqtt.publish, self._serial, self._model, "setConfigField",
                {"pid": self._serial, "keyPath": ["device", self._actuator],
                 self._actuator: {"mOnOff": 1, "mLevel": int(level)}}
            )

    async def async_turn_off(self, **kwargs):
        mqtt = self._state.get("mqtt")
        if mqtt:
            await self._hass.async_add_executor_job(
                mqtt.publish, self._serial, self._model, "setConfigField",
                {"pid": self._serial, "keyPath": ["device", self._actuator],
                 self._actuator: {"mLevel": 0}}
            )

    async def async_set_percentage(self, percentage: int):
        await self.async_turn_on(percentage=percentage)
