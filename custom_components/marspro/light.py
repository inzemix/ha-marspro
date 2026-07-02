"""Light platform for Mars Pro integration."""
import logging
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, ACTUATORS_IHUB10, ACTUATORS_CB43, DEVICE_IHUB10, DEVICE_CB43

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Mars Pro lights."""
    state = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for dev in state["devices"]:
        ptype = dev["productType"]
        if ptype not in (DEVICE_IHUB10, DEVICE_CB43):
            continue
        actuators = ACTUATORS_IHUB10 if ptype == DEVICE_IHUB10 else ACTUATORS_CB43
        for act_name, (domain, label) in actuators.items():
            if domain == "light":
                entities.append(MarsProLight(state, dev, act_name, label))

    async_add_entities(entities)


class MarsProLight(LightEntity):
    """Dimmable light (RJ12 / 0-10V)."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, state: dict, device_info: dict, actuator: str, label: str):
        self._state = state
        self._serial = device_info["serial"]
        self._model = device_info["model"]
        self._actuator = actuator
        self._attr_unique_id = f"marspro_{self._serial}_{actuator}_light"
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
    def brightness(self):
        data = self._state["live_data"].get(self._serial, {})
        devsta = data.get("getDevSta", {}).get("data", {})
        act = devsta.get(self._actuator, {})
        level = act.get("level", 0)
        return int(level * 255 / 100)  # 0-100 → 0-255

    @property
    def available(self):
        return self._serial in self._state["live_data"]

    async def async_turn_on(self, **kwargs):
        brightness = kwargs.get("brightness", 255)
        level = int(brightness * 100 / 255)  # 0-255 → 0-100
        # Round to nearest 5% for reliability
        level = max(5, min(100, round(level / 5) * 5))

        # Cancel any pending dim command (debounce — only send final value)
        if hasattr(self, "_debounce_timer") and self._debounce_timer:
            self._debounce_timer.cancel()

        async def _do_dim():
            mqtt = self._state.get("mqtt")
            if not mqtt:
                return
            await self.hass.async_add_executor_job(
                mqtt.publish, self._serial, self._model, "setConfigField",
                {"pid": self._serial, "keyPath": ["device", self._actuator],
                 self._actuator: {"mOnOff": 1, "mLevel": level}}
            )
            # Verify after 3 seconds
            async def _verify():
                mqtt = self._state.get("mqtt")
                if mqtt:
                    mqtt.publish(self._serial, self._model, "getDevSta",
                                 {"pid": self._serial})
            self.hass.loop.call_later(3, lambda: self.hass.async_create_task(_verify()))

        self._debounce_timer = self.hass.loop.call_later(1,
            lambda: self.hass.async_create_task(_do_dim()))

    async def async_turn_off(self, **kwargs):
        mqtt = self._state.get("mqtt")
        if mqtt:
            await self.hass.async_add_executor_job(
                mqtt.publish, self._serial, self._model, "setConfigField",
                {"pid": self._serial, "keyPath": ["device", self._actuator],
                 self._actuator: {"mLevel": 0}}
            )
