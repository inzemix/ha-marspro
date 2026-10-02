"""Switch platform for Mars Pro integration."""
import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN, ACTUATORS_IHUB10, ACTUATORS_CB43, DEVICE_IHUB10, DEVICE_CB43
from .actuators import async_set_config_field

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
        # Switch général (outlet.masterOn) — « réveille » l'iHub après coupure
        entities.append(MarsProMasterSwitch(state, dev))

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
        await async_set_config_field(
            self.hass, self._state, self._serial, self._model,
            ["device", self._actuator],
            {"mOnOff": 1},
        )

    async def async_turn_off(self, **kwargs):
        # "Off" means the mOnOff key is *absent* on this platform: writing a 0
        # is not the same thing, so the field is dropped instead.
        await async_set_config_field(
            self.hass, self._state, self._serial, self._model,
            ["device", self._actuator],
            {"mLevel": 0},
            drop=("mOnOff",),
        )


class MarsProMasterSwitch(SwitchEntity):
    """Switch général de l'iHub (outlet.masterOn).

    Après une coupure de courant, l'iHub redémarre avec masterOn=0 et ignore
    toutes les commandes d'actuateurs tant qu'on ne l'a pas « réveillé ».
    Ce switch expose le masterOn : turn_on le réveille, turn_off coupe tout.
    """

    _attr_icon = "mdi:power"

    def __init__(self, state: dict, device_info: dict):
        self._state = state
        self._serial = device_info["serial"]
        self._model = device_info["model"]
        self._attr_unique_id = f"marspro_{self._serial}_master_switch"
        self._attr_name = f"{device_info['name']} Master"
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
        outlet = devsta.get("outlet", {})
        return bool(outlet.get("masterOn", 0))

    @property
    def available(self):
        return self._serial in self._state["live_data"]

    async def async_turn_on(self, **kwargs):
        """Réveille l'iHub (masterOn=1)."""
        await async_set_config_field(
            self.hass, self._state, self._serial, self._model,
            ["outlet"],
            {"masterOn": 1},
        )

    async def async_turn_off(self, **kwargs):
        """Coupe l'iHub globalement (masterOn=0)."""
        await async_set_config_field(
            self.hass, self._state, self._serial, self._model,
            ["outlet"],
            {"masterOn": 0},
        )
