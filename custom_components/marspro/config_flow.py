"""Configuration flow for Mars Pro integration."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN
from .api import MarsProAPI, AuthError

class MarsProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mars Pro."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            try:
                api = MarsProAPI(user_input["email"], user_input["password"])
                await self.hass.async_add_executor_job(api.login)
                devices = await self.hass.async_add_executor_job(api.fetch_devices)
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    return self.async_create_entry(
                        title=f"Mars Pro ({user_input['email']})",
                        data={
                            "email": user_input["email"],
                            "password": user_input["password"],
                            "mqtt_user": api.mqtt_user,
                            "mqtt_pwd": api.mqtt_pwd,
                        },
                    )
            except AuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("email"): str,
                vol.Required("password"): str,
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MarsProOptionsFlow(config_entry)


class MarsProOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Mars Pro."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage options."""
        return self.async_show_form(step_id="init", data_schema=vol.Schema({}))
