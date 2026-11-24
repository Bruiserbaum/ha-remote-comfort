"""Constants for Remote Comfort."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.const import UnitOfTemperature

DOMAIN = "remote_comfort"
LOGGER = logging.getLogger(__package__)

UPDATE_INTERVAL = timedelta(minutes=2)
FORECAST_INTERVAL = timedelta(minutes=30)
FORECAST_HORIZON = timedelta(hours=18)

SUBENTRY_ZONE = "zone"
SUBENTRY_ROOM = "room"

# Main entry options
CONF_WEATHER = "weather_entity"
CONF_PRESENCE = "presence_entities"
CONF_RESPECT_MANUAL = "respect_manual_changes"

# Zone subentry
CONF_CLIMATE = "climate_entity"
CONF_UNIT = "unit"
CONF_FLOOR = "floor"
CONF_COMPENSATE = "compensate"

# Room subentry
CONF_ZONE = "zone"
CONF_SENSORS = "sensors"
CONF_ROLE = "role"

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ROLES = ["bedroom", "office", "living", "other"]
SELECTABLE_MODES = ["auto", "home", "away", "sleep", "vacation"]
DAY_OVERRIDES = ["auto", "remote", "office", "off"]

# Setpoints exposed as number entities (adjustable from dashboards).
LIVE_SETPOINTS = [
    "comfort_cool",
    "comfort_heat",
    "sleep_cool",
    "sleep_heat",
    "away_cool",
    "away_heat",
]

SCHEDULE_DEFAULTS: dict[str, object] = {
    "remote_days": ["mon", "fri"],
    "office_days": ["tue", "wed", "thu"],
    "wake_time": "06:30:00",
    "off_day_wake_time": "08:00:00",
    "sleep_time": "22:30:00",
    "work_start": "08:00:00",
    "work_end": "17:00:00",
    "leave_time": "07:30:00",
    "return_time": "17:30:00",
}

# Anything that is not a temperature.
_COMMON_DEFAULTS: dict[str, object] = {
    CONF_RESPECT_MANUAL: True,
    "presence_sets_away": False,
    "wake_lead": 30,
    "sleep_lead": 30,
    "return_lead": 45,
    "max_lead": 180,
    "precool_until": "14:00:00",
    "balance_enabled": True,
    "suppress_method": "setback",
    "balance_min_minutes": 10,
    "balance_max_minutes": 45,
    "balance_cooldown_minutes": 15,
}

_F_DEFAULTS: dict[str, float] = {
    "comfort_cool": 74,
    "comfort_heat": 69,
    "sleep_cool": 70,
    "sleep_heat": 66,
    "away_cool": 80,
    "away_heat": 62,
    "vacation_offset": 4,
    "min_gap": 3,
    "lead_per_degree": 6,
    "hot_threshold": 90,
    "cold_threshold": 25,
    "precool_offset": 2,
    "hot_away_cap": 3,
    "cold_away_cap": 4,
    "idle_zone_offset": 2,
    "compensation_limit": 3,
    "balance_trigger": 1,
    "balance_spread": 3,
    "balance_safety": 3,
    "balance_setback": 5,
}

_C_DEFAULTS: dict[str, float] = {
    "comfort_cool": 23.5,
    "comfort_heat": 20.5,
    "sleep_cool": 21,
    "sleep_heat": 19,
    "away_cool": 27,
    "away_heat": 16.5,
    "vacation_offset": 2,
    "min_gap": 1.5,
    "lead_per_degree": 10,
    "hot_threshold": 32,
    "cold_threshold": -4,
    "precool_offset": 1,
    "hot_away_cap": 1.5,
    "cold_away_cap": 2,
    "idle_zone_offset": 1,
    "compensation_limit": 1.5,
    "balance_trigger": 0.5,
    "balance_spread": 1.5,
    "balance_safety": 1.5,
    "balance_setback": 3,
}


def default_options(unit: str) -> dict[str, object]:
    """All option defaults for the house's temperature unit."""
    temps = _C_DEFAULTS if unit == UnitOfTemperature.CELSIUS else _F_DEFAULTS
    return {**SCHEDULE_DEFAULTS, **_COMMON_DEFAULTS, **temps}
