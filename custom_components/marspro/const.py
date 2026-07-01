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

# Sensors common to all devices
SENSOR_FIELDS = {
    "sensor": {
        "temp": ("temperature", "°C", "temperature"),
        "humi": ("humidity", "%", "humidity"),
        "vpd":  (None, "kPa", None),
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
