"""Constants for Mars Pro integration."""

DOMAIN = "marspro"

# Mars Pro cloud infrastructure (public — documented by iClint/MarsHydroAPIDocs)
REST_API_HOST = "mars-pro.api.lgledsolutions.com"
REST_API_URL = f"https://{REST_API_HOST}"
MQTT_HOST = "mars-pro.mqtt.lgledsolutions.com"
MQTT_PORT = 8883

# REST endpoints
LOGIN_PATH = "/api/android/ulogin/mailLogin/v1"
DEVICE_LIST_PATH = "/api/android/udm/getDeviceList/v1"

# MQTT topics pattern: MHPRO/{model}/API/{UP|DOWN}/{serial}
MQTT_TOPIC_UP = "MHPRO/{model}/API/UP/{serial}"
MQTT_TOPIC_DOWN = "MHPRO/{model}/API/DOWN/{serial}"

# App info for systemdata header (matches Mars Pro v2.1.0)
APP_VERSION = "2.1.0"
OS_TYPE = "android"
OS_VERSION = "14"

# Device classes (productType)
DEVICE_IHUB10 = "MH-IHUB10"
DEVICE_CB43 = "MH-CB43"

# The global master of an iHub Pro is outlet 0. It is switched with `setOutlet`
# (num = outlet index, on = 0/1). `outlet.masterOn` is READ-ONLY on the device:
# the vendor app's deserialiser stores it and nothing writes it, so a
# `setConfigField` on ["outlet"] is acknowledged (`code: 200`) then ignored.
# Verified on real hardware, 2026-10-26.
MASTER_OUTLET = 0

# Device types that are known to expose NO entities through this integration:
# they never answer an MQTT request, so there is nothing to read and nothing to
# write. These are reported discreetly at setup — they are expected, not errors.
#
# NOTE: MZU001 is shared by more than one product (grow-light fixtures, but also
# dimmer boxes belonging to other users), so this must describe the *observed
# behaviour* rather than a product family — the earlier wording ("BLE-only
# fixture") asserted more than was ever verified.
KNOWN_NO_ENTITY_TYPES = {
    "MZU001": "no reply to MQTT requests (no cloud actuators via this integration)",
}

# Device support report (service marspro.generate_device_report).
# The report is written inside the Home Assistant config directory so the user
# can open it with the File Editor and paste it into a GitHub issue.
SERVICE_GENERATE_REPORT = "generate_device_report"
SERVICE_FETCH_ALARM_FEED = "fetch_alarm_feed"
REPORT_FILENAME = "marspro_device_report.txt"
SUPPORTED_TYPES = (DEVICE_IHUB10, DEVICE_CB43)

# Home Assistant event fired once per new device alarm (issue #5). An iHub Pro
# reports no alarm state inside its actuator blocks, only the sticky `alarmLast`
# marker, so an event — not a binary sensor — is the honest primitive there.
EVENT_ALARM = f"{DOMAIN}_alarm"

# Server-side notification / alarm log (read-only). The device frame carries no
# alarm cause on an iHub, so this feed is the only documented place where one
# could be found (`alarmType`). Endpoint named by iClint/MarsHydroAPIDocs; the
# path follows the same `/api/android/{section}/{action}/v1` shape as LOGIN_PATH
# and DEVICE_LIST_PATH.
ALARM_FEED_PATH = "/api/android/notice_msg/queryMessageList/v1"

# `outlet.aRms` is reported in centiamps (0.01 A), while `vRms` is in volts and
# `wattP` in watts. No document states it; the factor is derived from the
# physics and holds on two installations:
#   105 raw / 236 V / 159 W  -> 1.05 A gives PF 0.64 (plausible); raw would be 24 kW
#   525 raw / 235 V / 1198 W -> 5.25 A gives PF 0.97 (plausible); raw would be 123 kW
#   3   raw / 232 V / 3 W    -> 0.03 A gives PF 0.43 (plausible); raw would be PF 0.004
# A garbage frame must not enter long-term statistics, so anything above this
# bound (100 A after scaling) is reported as unknown instead of as a value.
# It is a plausibility guard, NOT a specification: no controller socket of this
# class carries 100 A.
MAX_PLAUSIBLE_A_RAW = 10000

# Known actuator mapping per device class
ACTUATORS_IHUB10 = {
    "light":        ("light", "Light 1"),
    "light2":       ("light", "Light 2"),
    "blower":       ("fan", "Blower (Inline Fan)"),
    "fan":          ("fan", "Oscillating Fan"),
    "heater":       ("switch", "Heater"),
    "humidifier":   ("switch", "Humidifier"),
    "dehumidifier": ("switch", "Dehumidifier"),
    "watering":     ("switch", "Watering"),
    "device1":      ("switch", "Device 1"),
    "device2":      ("switch", "Device 2"),
}

ACTUATORS_CB43 = {
    "light":        ("light", "Light"),
    "light2":       ("light", "Light 2"),
    "fan":          ("fan", "Oscillating Fan"),
    "blower":       ("fan", "Blower (Inline Fan)"),
    "humidifier":   ("switch", "Humidifier"),
    "dehumidifier": ("switch", "Dehumidifier"),
}

# The four switched outlets some CB43 units carry (`device.ps4`). They are not
# siblings of the other actuators: each channel is written on its own keyPath
# (["device", "ps4", "O1"]), because writing the parent key replaces the whole
# block and would wipe the neighbouring channels' settings.
PS4_OUTPUTS = ("O1", "O2", "O3", "O4")

SENSOR_NAMES = {
    "temp": "Temperature",
    "humi": "Humidity",
    "vpd": "VPD",
    "vRms": "Voltage",
    "aRms": "Current",
    "wattP": "Power",
    "energy": "Energy",
    "ppfd": "PPFD",
    "tempSoil": "Soil Temperature",
    "humiSoil": "Soil Humidity",
    "ECSoil": "Soil EC",
    "co2": "CO2",
}

# Sensors common to all devices
SENSOR_FIELDS = {
    "sensor": {
        "temp": ("temperature", "°C", "temperature"),
        "humi": ("humidity", "%", "humidity"),
        "vpd":  (None, "kPa", None),
        "co2":  ("carbon_dioxide", "ppm", "carbon_dioxide"),
    },
    "outlet": {
        "vRms":   ("voltage", "V", "voltage"),
        "aRms":   ("current", "A", "current"),
        "wattP":  ("power", "W", "power"),
        "energy": ("energy", "kWh", "energy"),
    },
}

# CB43-specific extra sensors
SENSOR_CB43_EXTRA = {
    "ppfd": (None, "µmol/m²/s", "illuminance"),
    "tempSoil": ("temperature", "°C", "temperature"),
    "humiSoil": ("humidity", "%", "humidity"),
    "ECSoil": (None, "mS/cm", None),
}

# Reconnection backoff (seconds)
RECONNECT_BASE = 30
RECONNECT_MAX = 600
