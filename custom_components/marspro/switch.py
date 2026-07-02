"""Switch platform for Mars Pro integration."""
import json
import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, ACTUATORS_IHUB10, ACTUATORS_CB43, DEVICE_IHUB10, DEVICE_CB43

_LOGGER = logging.getLogger(__name__)

ACTUATOR_SWITCHES = {**ACTUATORS_IHUB10, **ACTUATORS_CB43}

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Mars Pro switches."""
    state = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for dev in state["devices"]:
        ptype = dev["productType"]
        if ptype not in (DEVICE_IHUB10, DEVICE_CB43):
            continue  # skip non-controller devices (lights, etc.)
        actuators = ACTUATORS_IHUB10 if ptype == DEVICE_IHUB10 else ACTUATORS_CB43
        for act_name, (domain, label) in actuators.items():
            if domain == "switch":
                entities.append(MarsProSwitch(state, dev, act_name, label))

    async_add_entities(entities)


class MarsProSwitch(SwitchEntity):
    """Switch for Mars Pro outlet."""

    def __init__(self, state: dict, device_info: dict, actuator: str, label: str):
        self._state = state
        self._serial = device_info["serial"]
        self._model = device_info["model"]
        self._actuator = actuator
        self._attr_unique_id = f"marspro_{self._serial}_{actuator}_switch"
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
    def available(self):
        return self._serial in self._state["live_data"]

    async def async_turn_on(self, **kwargs):
        mqtt = self._state.get("mqtt")
        if mqtt:
            await self.hass.async_add_executor_job(
                mqtt.publish, self._serial, self._model, "setConfigField",
                {"pid": self._serial, "keyPath": ["device", self._actuator],
                 self._actuator: {"mOnOff": 1}}
            )

    async def async_turn_off(self, **kwargs):
        mqtt = self._state.get("mqtt")
        if mqtt:
            await self.hass.async_add_executor_job(
                mqtt.publish, self._serial, self._model, "setConfigField",
                {"pid": self._serial, "keyPath": ["device", self._actuator],
                 self._actuator: {"mLevel": 0}}
            )
