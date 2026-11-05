<p align="center">
  <img src="assets/banner.jpg" alt="Mars Pro — Smart growing. Fully connected. Home Assistant integration" width="100%">
</p>

# Mars Hydro — Home Assistant Integration

[![Open your Home Assistant instance and open a repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=inzemix&repository=ha-marspro&category=integration)
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

Entities are only created for the two controller types listed above. Any other device type triggers a notification with a ready-to-send diagnostic report — see below.

## Adding support for another device

A controller that is not listed above can only be supported once its protocol has actually been observed — that is how iHub Pro support was added in the first place.

**You have nothing to install and nothing to run.** When the integration finds a device it does not support yet, it probes it once and shows a notification in Home Assistant with a link to the report:

1. It asks the device to describe itself — strictly read-only by default (`getDevSta`, `getSysSta`, `getConfigFile`). **Nothing is ever switched on or off.**
2. The report is written to `marspro_device_report.txt` in your Home Assistant configuration folder and served through a **private link that expires after 7 days**.
3. Send it to us in an issue and support can be added.

The report is built to avoid a long back and forth, so it also contains:

- the **actuators the device reports** (each one becomes a Home Assistant entity) and how they are driven (`setConfigField`);
- the **exact requests** that were sent, and which ones the device did **not** answer;
- a short **checklist of questions** about your setup to fill in.

**Please review the report before sending it**: it contains your device names, serial numbers and their raw state payloads (never your credentials).

You can also run it on demand: **Developer tools → Actions → `marspro.generate_device_report`**. It accepts an optional experimental *Test write commands* switch, which checks whether the device accepts commands by rewriting the values it just reported (nothing changes state) — leave it off unless we ask you to turn it on.

Note: a type already known to produce no entities (for example a grow light that only talks Bluetooth) is **not** probed automatically — opening a second connection to the vendor's broker for a device that already works in the Mars Pro app is not a risk worth taking. Use the action above if you want a report for such a device.

What the report answers is the key question: does the device answer on the cloud broker at all?

- **It answers** (like the iHub Pro): its data blocks become visible, and support can be added.
- **No reply** (like the FC series grow lights): the device is most likely Bluetooth-only. It talks to the Mars Pro app over BLE and exposes nothing through the cloud, so this integration cannot reach it.

`tools/discover_device.py` does the same from any computer (Windows, macOS, Linux) using **only the Python standard library — nothing to install**, handy when your Home Assistant is not reachable. Run it on a normal computer, **not** on your Home Assistant server: Home Assistant OS has no usable Python/pip environment.

```bash
python3 discover_device.py      # macOS / Linux
python discover_device.py       # Windows
```

Paste its output in a new issue.

### Checking a report before acting on it

`tools/audit_units.py` cross-checks the numbers in a report against a second, independent physical
quantity: power factor from V × I vs P, VPD recomputed from temperature and humidity, energy
magnitude, and plausible ranges for CO₂, PPFD and the soil probes. Standard library only, nothing to
install, exit code 1 when something is inconsistent:

```bash
python3 audit_units.py marspro_device_report.txt
```

A mismatch means the mapping the integration uses for that field is probably wrong. This exists
because **a healthy entity is not a true value**: `outlet.aRms` was reported in centiamps and
exposed as amps for weeks — the sensor was available and updating, and displayed 103 A for a 1.03 A
load. Presence, liveness and "0 unavailable" cannot catch that; only arithmetic on the numbers can.

## Installation

### Via the Marketplace (Home Assistant 2026.11 and newer)

Starting with Home Assistant 2026.11, HACS ships built in as the **Marketplace**.

1. Go to **Settings** → **Marketplace**
2. In the upper-right corner, select **Add from link**
3. Enter `https://github.com/inzemix/ha-marspro` (or `inzemix/ha-marspro`)
4. Select **Add**, open the repository, then **Install**
5. Restart Home Assistant

Adding a repository from a link requires a GitHub connection. Once connected, the Marketplace keeps it up to date; without one, a repository added from a link is not updated.

### Via HACS (Home Assistant 2026.10 and older)

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
- ⚠️ Device fault alarms (dehumidifier bucket full, humidifier tank empty) as a `problem` binary sensor
- 🔄 Automatic reconnection with exponential backoff
- 🩺 Automatic read-only diagnostic when an unsupported device is found, with a ready-to-send report
- 📐 **Recipe included:** compute DLI (daily light integral) from the PPFD sensor — see [below](#dli--daily-light-integral-from-ppfd)

## Fault alarms

Devices report faults in two different ways. The integration exposes both, but they are **not**
equivalent — this section says exactly what each one gives you.

### 1. Per-actuator `alarm` field — `<device> Alarm` binary sensor

Some devices put the fault in the actuator block concerned, as an `alarm` code:

| Block | `alarm` | Meaning |
|---|---|---|
| `dehumidifier` | `5` | Water bucket full |
| `humidifier` | `4` | Tank empty / needs refill |

Those are exposed as one `problem` binary sensor per controller, `<device> Alarm`. The field is
carried by every `getDevSta` frame (polled every 60 s) while the fault is active, so the sensor
returns to `off` by itself once the bucket is emptied. Attributes: `alarms` (raw snapshot, a
reported `0` is kept), `active` (decoded code + meaning), `alarm_last`, and
**`reports_alarm_field`** — which says whether this device actually reports the field at all.

⚠️ **A fault is transient.** On an iHub Pro the field appears while the fault is active and is gone
again afterwards: measured on real hardware, it was present for 6 s, 24 s and about three hours
across three bucket-full events, and absent from every steady-state frame. So a device report taken
minutes later shows nothing — that is expected, not a sign that the hub never reports it. The event
below is the reliable trigger; this sensor is the live state, and it can be brief.

### 2. `alarmLast` marker — `marspro_alarm` event

Every device carries a sticky `alarmLast` marker (`{id, epoch, devType}`) pointing at its most
recent alarm. The integration fires a **`marspro_alarm` event** once per new alarm — i.e. when the
marker's `id` changes. The first frame after startup only sets the baseline, so an alarm raised
while Home Assistant was down never triggers a notification storm.

Event data: `serial`, `name`, `alarm_id`, `epoch`, `dev_type`, `device` (a hint of *which* device
alarmed, e.g. `dehumidifier` / `inline fan`), and — when the device answered — `alarm_type` and
`cause` (`5` → *Water bucket full*, `3` → *Device disconnected*).

⚠️ **`alarmLast` itself carries no cause** — `devType 26` identifies the dehumidifier, but the marker
alone cannot tell a full bucket from a defrost. The **cause comes from the device**: on a new alarm
the integration asks the hub for its own record (`getAlarmLog`), which answers
`{"alarmType": 5, "devType": 26, …}`, and adds `alarm_type` and `cause` to the event when it gets
one. So an automation can target the actual reason:

```yaml
    triggers:
      - trigger: event
        event_type: marspro_alarm
        event_data:
          cause: Water bucket full
```

If the device does not answer within a few seconds, the event still fires — without `cause`. The
alarm is never delayed or lost for a missing detail. Verified on an iHub-10 Pro (v1.0.47): the hub
answers `getAlarmLog` from the integration and the event carries `cause: Water bucket full`.

A second, cloud-side route exists — the **`marspro.fetch_alarm_feed`** action, read-only — but on the
iHub account it was tested against it returned `count: 0` even right after a confirmed alarm, so
`count: 0` is a possible answer, not a malfunction. `getAlarmLog` is the route that works.

```yaml
automation:
  - alias: Dehumidifier alarm
    triggers:
      - trigger: event
        event_type: marspro_alarm
        event_data:
          device: dehumidifier
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Mars Pro
          message: "{{ trigger.event.data.name }} — alarm #{{ trigger.event.data.alarm_id }}"
```

## Recipes

Things you can build on top of the entities, without touching any code.

### DLI — daily light integral, from PPFD

DLI (mol/m²/day) is the total light your plants actually receive over the lit period — the integral of PPFD over time. That is what drives growth; the instantaneous PPFD reading is only the ingredient.

> **Requires a PPFD probe.** The `ppfd` entity only carries a value if your controller has the sensor fitted. If it stays `unknown`, this recipe produces nothing meaningful.

Home Assistant computes it with three built-in helpers — no code, no custom component. Add this to `configuration.yaml`:

```yaml
sensor:
  # 1 — accumulate the PPFD samples into a running total
  - platform: integration
    source: sensor.mh_ihub10_xxxxxxxxxxxx_ppfd    # replace with your own PPFD entity
    name: "PPFD integral"
    method: left
    unit_time: h

utility_meter:
  # 2 — slice that running total into days
  dli_daily:
    source: sensor.ppfd_integral
    cycle: daily

template:
  # 3 — scale to physical units
  - sensor:
      - name: "DLI"
        unique_id: mars_pro_dli
        unit_of_measurement: "mol/m²"
        state: "{{ (states('sensor.dli_daily') | float(0) * 0.0036) | round(2) }}"
        availability: "{{ states('sensor.dli_daily') not in ['unknown', 'unavailable'] }}"
```

**About `0.0036`** — it looks like a magic number, so here it is taken apart: it is `3600 × 1e-6`. Because step 1 runs with `unit_time: h`, its total is expressed in µmol × hours; multiplying by 3600 brings it back to seconds, and by 1e-6 it becomes moles instead of µmoles. Together: 0.0036.

**Expected values.** 12 h of light at ~450 µmol/m²/s lands around 19–20 mol/m² for the day — a normal figure for a grow tent under LED.

**Three things worth knowing:**

- **It survives a Home Assistant restart.** The `integration` helper is a `RestoreEntity` — it restores its accumulated total, and the meter recomputes the day's delta from that monotonic source. No custom persistence is involved.
- **It resets at local midnight** (`cycle: daily`). That is the correct boundary whenever the lit period does not cross midnight — a 08:00–20:00 schedule, for example. If your photoperiod *does* cross midnight, use the controller's own day/night window instead (`target.dayTime`, visible in the diagnostic report) so that a lit period is never split across two DLI days. Whichever you pick, document it.
- **The result depends on how often PPFD is sampled.** `getDevSta` only carries the instantaneous PPFD — the controller keeps no daily accumulation of its own, so there is nothing better to read. A steady polling interval gives an accurate figure; a long integration outage will slightly under-count.

> **Credit:** this recipe was contributed and verified on real hardware (iHub Pro + CB43) by [stefanr09](https://github.com/stefanr09) — see [issue #4](https://github.com/inzemix/ha-marspro/issues/4).

## Security

- Credentials stored encrypted by Home Assistant (config entry)
- MQTT connection uses TLS encryption
- Per-account ACLs enforced by Mars Hydro's broker
- **No personal data, keys, or tokens in this repository**

## FAQ

**Q: Does this work without the Mars Hydro cloud?**
A: No. Devices communicate through Mars Hydro's MQTT broker (`mars-pro.mqtt.lgledsolutions.com`). The firmware project [ihub-pro-open](https://github.com/thorstendjthb-glitch/ihub-pro-open) enables fully local control for iHub Pro.

**Q: My device doesn't appear in Home Assistant.**
A: Home Assistant shows a notification titled **"Mars Pro — unsupported device"** containing a link to a diagnostic report you can send us. The log also contains a line like:

```
Mars Pro: UNSUPPORTED device '<name>' (productType=<type>, serial=<serial>) — probing it now (read-only) so support can be added.
```

It tells you the exact device type found on your account. Entities are only created for `MH-IHUB10` and `MH-CB43`; other controllers (for example iConnect / iControl, which replaced the older Controller 43) were never reverse-engineered and therefore cannot be supported — see the credit note above for why. Also make sure the device is online and visible in the Mars Pro app first.

**Q: Can I control intensity/brightness?**
A: Yes, for lights connected via the RJ12 dimmer port (iHub Pro) or the CB43 light channel. Standard 230V outlets are on/off only.

## License

MIT © 2026 Xavier Clement
