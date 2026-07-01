"""Mars Pro integration for Home Assistant."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from .const import DOMAIN
from .api import MarsProAPI
from .mqtt_client import MarsProMQTT

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.LIGHT, Platform.FAN, Platform.BINARY_SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mars Pro from a config entry."""
    email = entry.data["email"]
    password = entry.data["password"]

    api = MarsProAPI(email, password)
    await hass.async_add_executor_job(api.login)
    devices = await hass.async_add_executor_job(api.fetch_devices)

    if not devices:
        _LOGGER.error("No devices found for account %s", email)
        return False

    # Shared state between platforms
    state = {"devices": devices, "live_data": {}, "mqtt": None}

    def on_mqtt_message(topic: str, data: dict):
        """Handle incoming MQTT messages."""
        method = data.get("method", "")
        if method in ("getDevSta", "getConfigFile", "getSysSta"):
            # Extract serial from topic: MHPRO/{model}/API/UP/{serial}
            parts = topic.split("/")
            if len(parts) >= 5:
                serial = parts[4]
                dev_state = state["live_data"].get(serial, {})
                dev_state[method] = data
                state["live_data"][serial] = dev_state

    mqtt = MarsProMQTT(
        hass,
        user=entry.data["mqtt_user"],
        password=entry.data["mqtt_pwd"],
        devices=devices,
        message_callback=on_mqtt_message,
    )
    await mqtt.connect()
    state["mqtt"] = mqtt

    # Poll each device immediately
    for dev in devices:
        mqtt.publish(dev["serial"], dev["model"], "getDevSta",
                     {"pid": dev["serial"]})

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = state
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    state = hass.data[DOMAIN].pop(entry.entry_id, {})
    if mqtt := state.get("mqtt"):
        mqtt.disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
