# Garden Irrigation

A Home Assistant custom integration for science-based drip irrigation scheduling. Calculates daily water needs using the FAO-56 Hargreaves-Samani evapotranspiration model, tracks soil moisture over time, and tells your automation exactly how long to run the valve.

## How it works

Each evening (default 23:00), the integration:

1. **Fetches weather** from [Open-Meteo](https://open-meteo.com) — free, no API key, uses the ECMWF model
2. **Calculates ET₀** (reference evapotranspiration) from yesterday's min/max temperature and your latitude
3. **Adjusts for crop type and growth stage** using FAO-56 crop coefficients (Kc), interpolated across the plant's lifecycle
4. **Updates the soil moisture bucket** — subtracts crop consumption, adds yesterday's measured rain
5. **Computes watering need** — if the bucket drops below your threshold, calculates how many minutes to run the valve to refill it to field capacity, accounting for forecast rain
6. **Notifies you** (optional) with a summary
7. **Exposes the result as sensors** — your automation reads `sensor.zone_watering_duration` in the morning and opens the valve accordingly

The integration never controls the valve directly. That responsibility belongs to your automation, giving you full flexibility to add conditions (wind, presence, local rain sensors, etc.).

## Features

- **Evapotranspiration model** — Hargreaves-Samani ET₀ from temperature alone; no humidity or wind sensor required
- **Growth stage tracking** — Kc interpolated across Initial → Development → Mid-season → Late season, configured with a visual stage selector instead of a planting date
- **Water balance bucket** — tracks soil moisture day-to-day, preventing over- or under-watering after rain
- **Forecast rain awareness** — skips or reduces watering if significant rain is expected in the next 36 hours
- **Multiple zones** — one config entry per zone, each with independent crops, bucket, and valve
- **Custom crops** — add your own crops or override FAO-56 defaults via a JSON file in your HA config directory
- **Automation blueprint** — ready-to-use blueprint for the watering cycle
- **Push notifications** — optional daily summary to one or more mobile app targets

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/matteorosani/ha-garden-irrigation` as an **Integration**
3. Install **Garden Irrigation** from HACS
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/garden_irrigation/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

Go to **Settings → Integrations → Add Integration** and search for **Garden Irrigation**.

The setup wizard has two steps:

### Step 1 — Zone identity

| Field | Description |
|---|---|
| Zone name | Friendly name, e.g. "Vegetable Field" |
| Daily calculation time | When to run ET₀ calculation. **23:00 recommended** — temperatures are settled, rain data is complete |
| Send notification | Toggle to enable daily push summaries |
| Notify services | Select one or more notify services (mobile app, groups, etc.) |

### Step 2 — Crops & system

| Field | Description |
|---|---|
| Crops | Multi-select from the crop library. Kc is averaged across all selected crops |
| Current growth stage | Visual stage selector (Initial / Development / Mid-season / Late) |
| Progress through stage | Slider 0–100% — how far through the current stage your plants are |
| Zone area (m²) | Total footprint of the planted area (length × width) |
| Flow rate (L/min) | Run your valve into a bucket for 1 minute — that volume is your flow rate |
| Max soil capacity (mm) | Sandy: 15–25 mm · Loam: 40–55 mm · Clay: 55–70 mm |
| Watering trigger (mm) | Open valve when moisture drops below this. Typical: 50% of max |

## Entities

For a zone named **Vegetable Field**, the following entities are created:

| Entity | Description |
|---|---|
| `sensor.vegetable_field_bucket_level` | Current soil moisture [mm] |
| `sensor.vegetable_field_bucket_percentage` | Soil moisture as % of max capacity |
| `sensor.vegetable_field_et_today` | Reference ET₀ for today [mm/day] |
| `sensor.vegetable_field_crop_coefficient_kc` | Effective Kc for today's growth stage |
| `sensor.vegetable_field_watering_duration` | Recommended valve open time [min]. **0 = skip** |
| `sensor.vegetable_field_rain_yesterday` | Measured precipitation yesterday [mm] |
| `sensor.vegetable_field_status` | Human-readable daily decision |
| `button.vegetable_field_record_irrigation` | Press after valve closes to update the bucket |
| `button.vegetable_field_reset_bucket` | Set bucket to full (after manual watering or heavy rain) |

### Key attributes

`sensor.vegetable_field_watering_duration` attributes:
- `water_mm` — water depth to deliver
- `volume_liters` — total volume to deliver
- `should_water` — boolean
- `skip_reason` — `"Skipped: soil ok"` or `"Skipped: rain forecast"` when duration is 0

## Automation blueprint

Import the watering cycle blueprint from your repository URL:

**Settings → Automations → Blueprints → Import Blueprint**

Paste the raw URL of `blueprints/automation/garden_irrigation/watering_cycle.yaml`.

Then create one automation per zone. The blueprint:
- Triggers at your chosen watering time (e.g. 06:00)
- Checks whether watering is needed
- Opens the valve for the calculated duration
- Closes the valve (guaranteed — even if something goes wrong)
- Presses the **Record irrigation** button to update the soil moisture model
- Optionally notifies you when watering is skipped

## Adding custom crops

Create `garden_irrigation_crops.json` in your HA config directory (the same folder as `configuration.yaml`):

```json
{
  "crops": [
    {
      "id":     "basil",
      "name":   "Basil",
      "kc_ini": 0.60,
      "kc_mid": 1.10,
      "kc_end": 0.90,
      "l_ini":  10,
      "l_dev":  20,
      "l_mid":  25,
      "l_late": 10
    }
  ]
}
```

Restart HA and the new crop appears in the zone configuration. This file survives integration updates.

To **override** a bundled crop, use the same `id` — your values win.

FAO-56 crop coefficient values: [FAO Irrigation Paper 56, Table 11](https://www.fao.org/3/x0490e/x0490e0b.htm)

## Dashboard

A sample Lovelace view is provided in `blueprints/dashboard/garden_irrigation/dashboard.yaml`. To use it:

1. Open your dashboard → Edit → ⋮ → **Edit in YAML**
2. Paste the contents of the `views:` section
3. Replace all occurrences of `{ZONE}` with your zone's slug

Your zone slug is derived from the zone name: spaces → underscores, all lowercase. Confirm exact entity IDs in **Developer Tools → States**.

## Physical setup tips

**Zone area:** measure the crop footprint (length × width). For open-field planting, use only the planted area, not the full field.

**Flow rate:** open the valve, hold a known-volume container under the drip output for exactly 1 minute, measure the collected water. That volume in litres is your L/min.

**Soil capacity:** do the ribbon test — wet a handful of soil and squeeze it between your fingers. Falls apart = sandy (15–25 mm). Short ribbon = loam (40–55 mm). Long smooth ribbon = clay (55–70 mm). If you've worked in compost, add 5–10 mm.

## Running tests

```bash
pip install -r requirements_test.txt
pytest
```

## Contributing

Pull requests welcome. Please run `ruff check` and `ruff format` before submitting.

Crop coefficient data: Allen, R.G. et al. (1998). *FAO Irrigation and Drainage Paper 56*. FAO, Rome.