<p align="center">
  <img src="assets/banner.jpg" alt="Mars Pro — Smart growing. Fully connected. Home Assistant integration" width="100%">
</p>

# Mars Hydro — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/inzemix/ha-marspro)](https://github.com/inzemix/ha-marspro/releases)
[![License](https://img.shields.io/github/license/inzemix/ha-marspro)](LICENSE)

Connect your Mars Hydro / Mars Pro devices (iHub Pro, iController, lamps, fans) to Home Assistant — sensors, switches, lights, and fans all discovered automatically.

> **Credit:** The Mars Pro cloud API foundation (REST login, MQTT topic scheme and broker authentication) was reverse-engineered by [iClint/MarsHydroAPIDocs](https://github.com/iClint/MarsHydroAPIDocs), verified there against a **Controller 43 (`MH-CB43`)**. This integration is built on top of that work. The **iHub Pro (`MH-IHUB10`)** protocol is *not* covered by that documentation — it was reverse-engineered separately for this project by observing the device's live MQTT traffic.

## Supported Devices

| Device | Product Type | Entities |
|---|---|---|
| **iHub Pro** | `MH-IHUB10` | T°, RH, VPD, power/energy, 10 outlets, 2 dimmers, fans |
| **iController Pro** | `MH-CB43` | T°, RH, VPD, PPFD, soil sensors, light, fan, blower, sockets |
| Grow lights (FC / TS series) | `MZU001` | ⚠️ **None of their own** — BLE-only devices that expose nothing through the cloud API. Their brightness is controlled through the iHub/iController dimmer port they are plugged into. |

Entities are only created for the two controller types listed above. Any other device type is reported in the Home Assistant log — see the FAQ below.

## Adding support for another device

A controller that is not listed above can only be supported once its protocol has actually been observed — that is how iHub Pro support was added in the first place.

`tools/discover_device.py` is a **read-only** probe: it lists every device on your account, then connects to the MQTT broker and asks each device for its state. It never sends a command and never changes anything on your setup.

```bash
pip install paho-mqtt
python3 tools/discover_device.py
```

Paste its output in a new issue. It shows the exact `productType` of each device and, crucially, **whether the device answers on the cloud broker at all**:

- **It answers** (like the iHub Pro): its data blocks become visible, and support can be added.
- **No reply** (like the FC series grow lights): the device is most likely Bluetooth-only. It talks to the Mars Pro app over BLE and exposes nothing through the cloud, so this integration cannot reach it.

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. URL: `https://github.com/inzemix/ha-marspro`
3. Category: **Integration**
4. Install → Restart Home Assistant

### Manual

Copy `custom_components/marspro/` to `<config>/custom_components/marspro/`

## Setup

1. **Settings** → **Devices & Services** → **Add Integration** → **Mars Pro**
2. Enter your Mars Pro account **email** and **password**
3. The integration automatically discovers all your devices

## Features

- 🌡️ Live sensor data (temperature, humidity, VPD, power, energy)
- 💡 Light control with brightness (dimming over RJ12 / 0-10V)
- 🔌 Individual outlet switching (replaces Zigbee plugs!)
- 🌀 Fan speed control (inline blower, oscillating)
- 📊 Per-device configuration via `setConfigField`
- ⚠️ Low water / fault alarms
- 🔄 Automatic reconnection with exponential backoff

## Security

- Credentials stored encrypted by Home Assistant (config entry)
- MQTT connection uses TLS encryption
- Per-account ACLs enforced by Mars Hydro's broker
- **No personal data, keys, or tokens in this repository**

## FAQ

**Q: Does this work without the Mars Hydro cloud?**
A: No. Devices communicate through Mars Hydro's MQTT broker (`mars-pro.mqtt.lgledsolutions.com`). The firmware project [ihub-pro-open](https://github.com/thorstendjthb-glitch/ihub-pro-open) enables fully local control for iHub Pro.

**Q: My device doesn't appear in Home Assistant.**
A: Check your Home Assistant log for a line like:

```
Mars Pro: UNSUPPORTED device '<name>' (productType=<type>, serial=<serial>) — no entities will be created for this device
```

It tells you the exact device type found on your account. Entities are only created for `MH-IHUB10` and `MH-CB43`; other controllers (for example iConnect / iControl, which replaced the older Controller 43) were never reverse-engineered and therefore cannot be supported — see the credit note above for why. Also make sure the device is online and visible in the Mars Pro app first.

**Q: Can I control intensity/brightness?**
A: Yes, for lights connected via the RJ12 dimmer port (iHub Pro) or the CB43 light channel. Standard 230V outlets are on/off only.

## License

MIT © 2026 Xavier Clement
