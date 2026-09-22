"""Mars Pro integration for Home Assistant."""
import logging
import os
from datetime import datetime, timedelta, timezone

import voluptuous as vol
from aiohttp import web

from homeassistant.components import persistent_notification
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.const import Platform, __version__ as HA_VERSION
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.loader import async_get_integration

from .const import (
    DEVICE_IHUB10,
    DOMAIN,
    KNOWN_NO_ENTITY_TYPES,
    MQTT_HOST,
    REPORT_FILENAME,
    SERVICE_GENERATE_REPORT,
    SUPPORTED_TYPES,
)
from .api import AuthError, MarsProAPI
from .mqtt_client import MarsProMQTT
from .probe import build_report, probe_device

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.LIGHT, Platform.FAN, Platform.BINARY_SENSOR]

ISSUES_URL = "https://github.com/inzemix/ha-marspro/issues/new"

UNSUPPORTED_NOTIFICATION_ID = f"{DOMAIN}_unsupported_devices"

# How long the notification's report link stays valid.
REPORT_LINK_TTL = timedelta(days=7)

# The automatic scan waits this long for each device to answer.
AUTO_SCAN_WAIT_SECONDS = 12

SERVICE_GENERATE_REPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("include_supported", default=False): cv.boolean,
        vol.Optional("test_writes", default=False): cv.boolean,
        vol.Optional("wait_seconds", default=12): vol.All(
            vol.Coerce(int), vol.Range(min=3, max=60)
        ),
    }
)

# Short, actionable notification texts. The report itself lives behind the link.
MESSAGES = {
    "en": {
        "title": "Mars Pro — unsupported device",
        "ready": (
            "**{names}** — found on your Mars Pro account, but not supported by this "
            "integration yet, so it does not appear in Home Assistant.\n\n"
            "A read-only diagnostic report was generated automatically:\n"
            "[📄 Open the report]({link})\n\n"
            "Send it to us so support can be added:\n"
            "[Open an issue on GitHub]({issues})\n\n"
            "_No command was sent to the device: the report only asks it to "
            "describe itself._"
        ),
        "failed": (
            "**{names}** — found on your Mars Pro account, but not supported by this "
            "integration yet.\n\n"
            "The automatic diagnostic could not be completed ({error}). You can retry "
            "it from **Developer tools → Actions** with `{service}`.\n\n"
            "_No command was sent to the device._"
        ),
        "nothing_to_report": (
            "Every device on this Mars Pro account is already supported — there is "
            "nothing to report."
        ),
        "manual_ready": (
            "Report generated for **{names}**:\n"
            "[📄 Open the report]({link})\n\n"
            "Send it to us so support can be added:\n"
            "[Open an issue on GitHub]({issues})"
        ),
    },
    "fr": {
        "title": "Mars Pro — appareil non pris en charge",
        "ready": (
            "**{names}** — trouvé sur votre compte Mars Pro, mais pas encore pris en "
            "charge par cette intégration : il n'apparaît donc pas dans Home "
            "Assistant.\n\n"
            "Un rapport de diagnostic (lecture seule) a été généré automatiquement :\n"
            "[📄 Ouvrir le rapport]({link})\n\n"
            "Envoyez-le nous pour que le support puisse être ajouté :\n"
            "[Ouvrir une issue GitHub]({issues})\n\n"
            "_Aucun ordre n'a été envoyé à l'appareil : le rapport se contente de lui "
            "demander de se décrire._"
        ),
        "failed": (
            "**{names}** — trouvé sur votre compte Mars Pro, mais pas encore pris en "
            "charge par cette intégration.\n\n"
            "Le diagnostic automatique n'a pas pu aboutir ({error}). Vous pouvez le "
            "relancer depuis **Outils de développement → Actions** avec "
            "`{service}`.\n\n"
            "_Aucun ordre n'a été envoyé à l'appareil._"
        ),
        "nothing_to_report": (
            "Tous les appareils de ce compte Mars Pro sont déjà pris en charge — il n'y "
            "a rien à signaler."
        ),
        "manual_ready": (
            "Rapport généré pour **{names}** :\n"
            "[📄 Ouvrir le rapport]({link})\n\n"
            "Envoyez-le nous pour que le support puisse être ajouté :\n"
            "[Ouvrir une issue GitHub]({issues})"
        ),
    },
}


class MarsProReportView(HomeAssistantView):
    """Serve the generated device support report.

    Authentication is required, but the integration hands the user a *signed*
    URL (`async_sign_path`), so the link in the notification works without an
    Authorization header while remaining private and expiring after a week.
    """

    url = "/api/marspro/device_report"
    name = "api:marspro:device_report"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the report as plain text."""
        hass: HomeAssistant = request.app["hass"]
        path = hass.config.path(REPORT_FILENAME)
        if not await hass.async_add_executor_job(os.path.exists, path):
            return self.json_message(
                "No device report has been generated yet.", status_code=404
            )
        content = await hass.async_add_executor_job(_read_report, path)
        return web.Response(text=content, content_type="text/plain", charset="utf-8")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mars Pro from a config entry."""
    email = entry.data["email"]
    password = entry.data["password"]

    api = MarsProAPI(email, password)
    # The Mars Hydro cloud occasionally answers 502/503 or times out. Without
    # ConfigEntryNotReady such a hiccup would leave the integration permanently
    # failed (entities unavailable) until a manual reload/restart. Only a
    # genuine credential problem triggers a reauth flow.
    try:
        await hass.async_add_executor_job(api.login)
    except AuthError as err:
        raise ConfigEntryAuthFailed(f"Mars Pro rejected the credentials: {err}") from err
    except Exception as err:
        raise ConfigEntryNotReady(f"Cannot reach the Mars Pro cloud API: {err}") from err

    try:
        devices = await hass.async_add_executor_job(api.fetch_devices)
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Cannot fetch devices from the Mars Pro cloud: {err}"
        ) from err

    if not devices:
        _LOGGER.error("No devices found for account %s", email)
        return False

    # Entities are only created for the controller types below (see the platform
    # modules). A type we do not know at all is probed automatically, read-only,
    # so support can be added — that is the whole point of this diagnostic.
    #
    # Types listed in KNOWN_NO_ENTITY_TYPES are deliberately NOT probed on
    # startup any more. Probing opens a second connection to the vendor's MQTT
    # broker for a device that already works in the vendor app, and an
    # unsupported device does not justify risking a working setup: one user
    # reported losing dimming control in the Mars Hydro app right after a
    # release that started probing his device. The report can still be produced
    # on demand with the marspro.generate_device_report action.
    unsupported: list[dict] = []
    for d in devices:
        ptype = d["productType"]
        if ptype in SUPPORTED_TYPES:
            _LOGGER.info(
                "Mars Pro: discovered device '%s' serial=%s productType=%s model=%s",
                d["name"], d["serial"], ptype, d["model"],
            )
        elif ptype in KNOWN_NO_ENTITY_TYPES:
            _LOGGER.info(
                "Mars Pro: device '%s' (productType=%s, serial=%s) is a %s — no "
                "entities by design, and not probed automatically. Run the %s "
                "action if you want a diagnostic report.",
                d["name"], ptype, d["serial"], KNOWN_NO_ENTITY_TYPES[ptype],
                f"{DOMAIN}.{SERVICE_GENERATE_REPORT}",
            )
        else:
            unsupported.append(d)
            _LOGGER.warning(
                "Mars Pro: UNSUPPORTED device '%s' (productType=%s, serial=%s) — "
                "probing it now (read-only) so support can be added.",
                d["name"], ptype, d["serial"],
            )

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

    def on_mqtt_reconnect():
        """Called after successful MQTT reconnection — restore masterOn + poll."""
        for dev in devices:
            # Only the iHub Pro has the masterOn outlet. Never write to a device
            # we do not understand: a dimmer box answers getDevSta too, and
            # there is no reason to push it a command whose effect we cannot
            # predict. Reading stays unconditional.
            if dev["productType"] == DEVICE_IHUB10:
                _LOGGER.info(
                    "Mars Pro MQTT reconnected — restoring masterOn on %s (the only "
                    "device that has that outlet)", dev["serial"]
                )
                mqtt.publish(dev["serial"], dev["model"], "setConfigField",
                             {"pid": dev["serial"], "keyPath": ["outlet"],
                              "outlet": {"masterOn": 1}})
            mqtt.publish(dev["serial"], dev["model"], "getDevSta",
                         {"pid": dev["serial"]})
        # Log per device, never a blanket message: an unconditional
        # "restoring masterOn" line once made a user believe we were writing to
        # a dimmer box we only ever poll.
        _LOGGER.debug("Mars Pro MQTT reconnected — %d device(s) polled (read-only)",
                      len(devices))

    mqtt = MarsProMQTT(
        hass,
        user=entry.data["mqtt_user"],
        password=entry.data["mqtt_pwd"],
        devices=devices,
        message_callback=on_mqtt_message,
        on_reconnect=on_mqtt_reconnect,
    )
    try:
        await mqtt.connect()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to the Mars Pro MQTT broker: {err}"
        ) from err
    state["mqtt"] = mqtt

    # Poll after a short delay to let MQTT subscriptions settle
    def poll_devices():
        for dev in devices:
            mqtt.publish(dev["serial"], dev["model"], "getDevSta",
                         {"pid": dev["serial"]})
    hass.loop.call_later(3, poll_devices)

    # Periodic health polling (every 60s)
    async def periodic_poll(now=None):
        for dev in devices:
            mqtt.publish(dev["serial"], dev["model"], "getDevSta",
                         {"pid": dev["serial"]})
        state["_poll_timer"] = hass.loop.call_later(60,
            lambda: hass.async_create_task(periodic_poll()))

    state["_poll_timer"] = hass.loop.call_later(65,
        lambda: hass.async_create_task(periodic_poll()))

    async def async_generate_device_report(call: ServiceCall) -> None:
        """Probe devices and write a report the user can send us (manual trigger).

        Read-only: only getDevSta / getSysSta / getConfigFile are sent.
        """
        include_supported = call.data["include_supported"]
        test_writes = call.data["test_writes"]
        wait_seconds = call.data["wait_seconds"]

        targets = [
            d for d in devices
            if include_supported or d["productType"] not in SUPPORTED_TYPES
        ]
        if not targets:
            persistent_notification.async_create(
                hass,
                _text(hass, "nothing_to_report"),
                title="Mars Pro",
                notification_id=f"{DOMAIN}_report_result",
            )
            return

        _LOGGER.info(
            "Mars Pro: probing %d device(s) for the support report (read-only)",
            len(targets),
        )
        try:
            await _async_probe_and_write_report(hass, entry, targets, wait_seconds,
                                                test_writes)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Mars Pro: device report failed")
            await _async_notify(
                hass, "failed",
                names=_device_names(targets),
                error=f"{type(err).__name__}: {err}",
                service=f"{DOMAIN}.{SERVICE_GENERATE_REPORT}",
            )
            return

        await _async_notify(hass, "manual_ready", names=_device_names(targets),
                            link=_report_link(hass), issues=ISSUES_URL)

    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_REPORT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_REPORT,
            async_generate_device_report,
            schema=SERVICE_GENERATE_REPORT_SCHEMA,
        )

    if not hass.data.get(f"{DOMAIN}_report_view_registered"):
        hass.http.register_view(MarsProReportView)
        hass.data[f"{DOMAIN}_report_view_registered"] = True

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = state
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Devices we cannot support yet: probe them on our own so the user has
    # nothing to install or run, and tell them where to send the result. This
    # runs as a background task: the probe waits ~12 s per device and must never
    # delay the integration setup.
    if unsupported:
        entry.async_create_background_task(
            hass,
            _async_auto_scan(hass, entry, unsupported),
            name=f"{DOMAIN}_auto_scan",
        )
    else:
        await _async_dismiss_unsupported_notification(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    state = hass.data[DOMAIN].pop(entry.entry_id, {})
    if timer := state.get("_poll_timer"):
        timer.cancel()
    if mqtt := state.get("mqtt"):
        mqtt.disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_auto_scan(hass: HomeAssistant, entry: ConfigEntry,
                           targets: list[dict]) -> None:
    """Probe the unsupported devices automatically, then point the user to the report.

    Only genuinely unknown types reach this function: types listed in
    KNOWN_NO_ENTITY_TYPES are no longer probed on startup (see
    `async_setup_entry`), because opening a second broker connection for a
    device that already works in the vendor app is not a risk worth taking.
    """
    names = _device_names(targets)
    _LOGGER.info(
        "Mars Pro: automatically probing %d unsupported device(s) (read-only)",
        len(targets),
    )
    try:
        await _async_probe_and_write_report(
            hass, entry, targets, AUTO_SCAN_WAIT_SECONDS
        )
        link = _report_link(hass)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Mars Pro: automatic device report failed")
        await _async_notify(
            hass, "failed", names=names, error=f"{type(err).__name__}: {err}",
            service=f"{DOMAIN}.{SERVICE_GENERATE_REPORT}",
        )
        return

    await _async_notify(hass, "ready", names=names, link=link, issues=ISSUES_URL)


async def _async_probe_and_write_report(hass: HomeAssistant, entry: ConfigEntry,
                                        targets: list[dict], wait_seconds: int,
                                        test_writes: bool = False) -> str:
    """Probe the given devices and write the report. Returns its path.

    Read-only unless `test_writes` is set: the write probe only rewrites the
    values the device itself just reported, so nothing changes state.
    """
    # Refresh the account's device list so the cloud metadata in the report
    # (connectStatus, deviceInfo, …) describes *now*, not the moment Home
    # Assistant started. A report generated days after a restart would
    # otherwise carry stale values — and connectStatus is exactly what tells us
    # whether a device is reachable at all.
    try:
        api = MarsProAPI(entry.data["email"], entry.data["password"])
        await hass.async_add_executor_job(api.login)
        fresh = {d["serial"]: d for d in
                 await hass.async_add_executor_job(api.fetch_devices)}
        targets = [{**d, **fresh.get(d["serial"], {})} for d in targets]
    except Exception as err:  # noqa: BLE001 - the report must still be written
        _LOGGER.warning(
            "Mars Pro: could not refresh the device list (%s: %s) — the report "
            "will use the values read at startup", type(err).__name__, err
        )

    probes: dict[str, dict] = {}
    for device in targets:
        probes[device["serial"]] = await hass.async_add_executor_job(
            probe_device,
            MQTT_HOST,
            entry.data["mqtt_user"],
            entry.data["mqtt_pwd"],
            device,
            wait_seconds,
            test_writes,
        )

    integration = await async_get_integration(hass, DOMAIN)
    report = build_report(
        targets,
        probes,
        SUPPORTED_TYPES,
        integration.version or "unknown",
        MQTT_HOST,
        ha_version=HA_VERSION,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    path = hass.config.path(REPORT_FILENAME)
    await hass.async_add_executor_job(_write_report, path, report)
    _LOGGER.warning("Mars Pro: device support report written to %s", path)
    return path


def _report_link(hass: HomeAssistant) -> str:
    """Return a signed, expiring **absolute** URL serving the report.

    The URL must be absolute: the phone apps hand a bare relative path over to
    an external browser, where it cannot be resolved and the link looks dead.
    """
    path = async_sign_path(hass, MarsProReportView.url, REPORT_LINK_TTL)
    try:
        base = get_url(
            hass, allow_internal=True, allow_external=True, allow_cloud=True
        )
    except NoURLAvailableError:
        # last resort: relative link, still fine when opened from the web UI
        return path
    return f"{base}{path}"


def _device_names(devices: list[dict]) -> str:
    return ", ".join(f"{d['name']} ({d['productType']})" for d in devices)


def _messages(hass: HomeAssistant) -> dict:
    """Notification texts for the user's language."""
    language = str(getattr(hass.config, "language", "en") or "en")[:2]
    return MESSAGES.get(language, MESSAGES["en"])


def _text(hass: HomeAssistant, key: str, **kwargs) -> str:
    """Translated notification body, formatted with the given values."""
    texts = _messages(hass)
    template = texts.get(key) or MESSAGES["en"].get(key, "")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):  # never fail on a text formatting issue
        return template


async def _async_notify(hass: HomeAssistant, key: str, **kwargs) -> None:
    """Create (or replace) the notification, in the user's language."""
    persistent_notification.async_create(
        hass,
        _text(hass, key, **kwargs),
        title=_messages(hass).get("title", "Mars Pro"),
        notification_id=UNSUPPORTED_NOTIFICATION_ID,
    )


async def _async_dismiss_unsupported_notification(hass: HomeAssistant) -> None:
    """Remove the notification once every device is supported."""
    try:
        persistent_notification.async_dismiss(hass, UNSUPPORTED_NOTIFICATION_ID)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def _read_report(path: str) -> str:
    """Read the report from disk (runs in an executor)."""
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _write_report(path: str, report: str) -> None:
    """Write the report to disk (runs in an executor)."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report)
