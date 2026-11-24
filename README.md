# Remote Comfort

A Home Assistant integration that runs your HVAC around how you actually live: remote days, office days, bedtime, the weather forecast, and which rooms matter right now.

Part of [BaumHome](https://github.com/Bruiserbaum/BaumHome), a collection of my Home Assistant designs.

## What it does

- **Knows your week.** You choose remote days and office days. Every other day is a day off. You set wake-up, bedtime, work hours, and when you leave and get home on office days.
- **Recovers before you're back.** On office days the house sits at away targets and starts recovering before you get home. In extreme weather that lead time grows automatically, a few minutes per degree past your hot or cold threshold.
- **Reads the forecast.** On hot days it pre-cools in the morning while you're home, so the house coasts through the afternoon peak. When you're away on a hot day it limits how far the house drifts, so it doesn't heat-soak. Cold days get the same treatment for heating.
- **Sleep your way.** Separate sleep targets, plus a bedtime lead time to cool (or warm) the bedrooms before you go to bed.
- **Rooms, floors and units.** Each room has temperature sensors, the zone that serves it, a floor, and a role (bedroom, office, living, other). The rooms in use decide comfort:

  | Period | Rooms that count |
  |---|---|
  | Sleep | Bedrooms |
  | Working (remote day, work hours) | Office, living |
  | Home | Living, office, other |
  | Away / vacation | All rooms (for reporting only) |

  A zone with no rooms in use is relaxed. For example, downstairs overnight.
- **Room compensation.** If a zone's thermostat hangs in a hallway that runs cooler than the office, its setpoint is biased so the **office** reaches the target. The bias is limited.
- **Zone balancing on a shared unit.** When zones share one HVAC unit (zoned dampers) and one zone is far worse than another, the comfortable zone is closed for a while. All the air then goes where it's needed. The classic case is a hot summer afternoon with upstairs at 79 and downstairs at 73: downstairs closes and upstairs gets the full cooling capacity. Minimum and maximum closed times, a safety limit, and a cooldown prevent short-cycling.
- **Respects people.** Presence entities keep comfort targets if anyone is home during a scheduled away period. A thermostat changed by hand is left alone until the next period. A master switch turns off all control.

## Install

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/Bruiserbaum/ha-remote-comfort`, category **Integration**.
2. Install **Remote Comfort**, then restart Home Assistant.

### Manual

Copy `custom_components/remote_comfort` into your HA `config/custom_components/` folder and restart.

Requires Home Assistant **2025.7** or newer.

## Set up

1. **Settings → Devices & services → Add integration → Remote Comfort.**
   - Choose a weather entity (optional, but needed for the weather features) and presence entities (optional).
   - Enter your week: remote days, office days, and times.
   - Set comfort targets: home, sleep, and away, each for cooling and heating.
2. On the Remote Comfort integration page:
   - **Add HVAC zone** once per thermostat (or per zone thermostat on a zoning panel). Zones served by the same furnace or air handler need the **same HVAC unit name**. That shared name is what enables balancing.
   - **Add room** once per room. Pick its temperature sensors, the zone that serves it, its role, and its floor.
3. Fine-tune anything later under **Configure**: general, schedule, comfort, weather, and balancing.

> **Turn off your thermostats' own schedules.** Remote Comfort sends setpoints every few minutes. A thermostat schedule (Ecobee comfort settings, Nest auto-schedule, etc.) would fight it, and would also look like a manual change.

## Entities

| Entity | What it is |
|---|---|
| `sensor.remote_comfort_<name>_status` | Current period: home, working, away, returning, sleep, vacation or paused. Attributes explain why (`reasons`), show the forecast, and list held zones. |
| `sensor.…_cool_target` / `…_heat_target` | House-level targets right now. |
| `number.…_home_cool_target` etc. | Six everyday setpoints, adjustable from a dashboard. |
| `select.…_mode` | Auto, Home, Away, Sleep, Vacation. |
| `select.…_today_is` | Per schedule, Remote day, Office day, Day off. Resets at midnight. |
| `switch.…_thermostat_control` | Master switch. When off, nothing is sent to thermostats. |
| `sensor.<zone>_zone_comfort_temperature` | The temperature of the rooms that matter in that zone. Attributes: demand, setpoint, active rooms, reason. |
| `binary_sensor.<zone>_zone_balancing` | On while the zone is closed to push air elsewhere. |
| `sensor.<room>_temperature` | Average of the room's sensors. Attributes include floor, role, zone, and HVAC unit. |

See [`examples/dashboard.yaml`](examples/dashboard.yaml) for a dashboard that plots rooms by floor.

## How decisions are made

Every 2 minutes:

1. **Period.** The schedule, adjusted by lead times, sets the period. Presence and the mode select can override it.
2. **House targets.** These come from the period, then weather adjustments: pre-cooling, and limits on away drift.
3. **Zone targets.** The rooms in use set each zone's comfort temperature. Zones with no rooms in use are relaxed, and room compensation biases the setpoint.
4. **Balancing.** Within each HVAC unit, while occupied and with every zone in the same cool or heat mode:
   - **Close** a zone when the worst zone is at least `trigger` over target, at least `spread` worse than the zone being closed, and that zone is at target (occupied) or within the safety margin (idle).
   - **Re-open** it when it drifts past `safety`, after `max` minutes, or once the other zones catch up (after at least `min` minutes). Then wait `cooldown` minutes before closing it again.
5. **Apply.** Setpoints are sent only when they differ by more than 0.6°. Thermostats in heat/cool mode get both setpoints, kept at least `min_gap` apart.

"Close a zone" means raising its cooling setpoint (or lowering its heating setpoint) by the setback amount. You can choose to turn the zone **off** instead, and it is turned back on afterwards. Setback is gentler on zoning panels.

The decision logic lives in [`engine.py`](custom_components/remote_comfort/engine.py), which has no Home Assistant imports, so every rule is unit tested.

## Development

```bash
uv venv && uv pip install pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```

## License

MIT
