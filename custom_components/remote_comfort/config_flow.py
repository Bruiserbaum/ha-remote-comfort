"""Config, options and subentry (zone / room) flows for Remote Comfort."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_CLIMATE,
    CONF_COMPENSATE,
    CONF_FLOOR,
    CONF_PRESENCE,
    CONF_RESPECT_MANUAL,
    CONF_ROLE,
    CONF_SENSORS,
    CONF_UNIT,
    CONF_WEATHER,
    CONF_ZONE,
    DOMAIN,
    LIVE_SETPOINTS,
    ROLES,
    SUBENTRY_ROOM,
    SUBENTRY_ZONE,
    WEEKDAYS,
    default_options,
)

# ----------------------------------------------------------------- selectors


def _temp(unit: str, lo: float = -40, hi: float = 120, step: float | None = None) -> sel.NumberSelector:
    if step is None:
        step = 0.5 if unit == UnitOfTemperature.CELSIUS else 1
    return sel.NumberSelector(
        sel.NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit,
                                 mode=sel.NumberSelectorMode.BOX)
    )


def _delta(unit: str, hi: float = 15) -> sel.NumberSelector:
    return _temp(unit, 0, hi, 0.5)


def _minutes(hi: int = 360) -> sel.NumberSelector:
    return sel.NumberSelector(
        sel.NumberSelectorConfig(min=0, max=hi, step=5, unit_of_measurement="min",
                                 mode=sel.NumberSelectorMode.BOX)
    )


_TIME = sel.TimeSelector()
_BOOL = sel.BooleanSelector()
_DAYS = sel.SelectSelector(
    sel.SelectSelectorConfig(options=WEEKDAYS, multiple=True, translation_key="weekday",
                             mode=sel.SelectSelectorMode.LIST)
)


def _general_schema(unit: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_WEATHER): sel.EntitySelector(sel.EntitySelectorConfig(domain="weather")),
            vol.Optional(CONF_PRESENCE): sel.EntitySelector(
                sel.EntitySelectorConfig(
                    domain=["person", "device_tracker", "binary_sensor", "input_boolean", "zone"],
                    multiple=True,
                )
            ),
            vol.Required("presence_sets_away"): _BOOL,
            vol.Required(CONF_RESPECT_MANUAL): _BOOL,
            vol.Required("wake_lead"): _minutes(),
            vol.Required("sleep_lead"): _minutes(),
            vol.Required("return_lead"): _minutes(),
            vol.Required("idle_zone_offset"): _delta(unit),
            vol.Required("compensation_limit"): _delta(unit),
            vol.Required("sensor_max_age"): _minutes(1440),
        }
    )


def _safety_schema(unit: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("guard_enabled"): _BOOL,
            vol.Required("guard_min"): _temp(unit),
            vol.Required("guard_max"): _temp(unit),
            vol.Required("guard_margin"): _delta(unit, 10),
            vol.Required("guard_switch_mode"): _BOOL,
            vol.Required("failsafe_heat"): _temp(unit),
            vol.Required("failsafe_cool"): _temp(unit),
            vol.Required("safety_min"): _temp(unit),
            vol.Required("safety_max"): _temp(unit),
            vol.Optional("notify_service"): sel.TextSelector(),
        }
    )


_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("remote_days"): _DAYS,
        vol.Required("office_days"): _DAYS,
        vol.Required("wake_time"): _TIME,
        vol.Required("off_day_wake_time"): _TIME,
        vol.Required("sleep_time"): _TIME,
        vol.Required("work_start"): _TIME,
        vol.Required("work_end"): _TIME,
        vol.Required("leave_time"): _TIME,
        vol.Required("return_time"): _TIME,
    }
)


def _comfort_schema(unit: str, full: bool) -> vol.Schema:
    schema: dict[Any, Any] = {vol.Required(key): _temp(unit) for key in LIVE_SETPOINTS}
    if full:
        schema[vol.Required("vacation_offset")] = _delta(unit)
        schema[vol.Required("min_gap")] = _delta(unit)
    return vol.Schema(schema)


def _weather_schema(unit: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("hot_threshold"): _temp(unit),
            vol.Required("cold_threshold"): _temp(unit),
            vol.Required("precool_offset"): _delta(unit),
            vol.Required("precool_until"): _TIME,
            vol.Required("hot_away_cap"): _delta(unit, 20),
            vol.Required("cold_away_cap"): _delta(unit, 20),
            vol.Required("lead_per_degree"): sel.NumberSelector(
                sel.NumberSelectorConfig(min=0, max=60, step=1, unit_of_measurement="min",
                                         mode=sel.NumberSelectorMode.BOX)
            ),
            vol.Required("max_lead"): _minutes(600),
        }
    )


def _balancing_schema(unit: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("balance_enabled"): _BOOL,
            vol.Required("suppress_method"): sel.SelectSelector(
                sel.SelectSelectorConfig(options=["setback", "off"], translation_key="suppress_method")
            ),
            vol.Required("balance_trigger"): _delta(unit),
            vol.Required("balance_spread"): _delta(unit),
            vol.Required("balance_safety"): _delta(unit),
            vol.Required("balance_setback"): _delta(unit),
            vol.Required("balance_min_minutes"): _minutes(120),
            vol.Required("balance_max_minutes"): _minutes(240),
            vol.Required("balance_cooldown_minutes"): _minutes(120),
        }
    )


def _validate_safety(data: dict[str, Any]) -> dict[str, str]:
    if float(data["guard_min"]) >= float(data["guard_max"]):
        return {"base": "guard_range"}
    if float(data["failsafe_heat"]) >= float(data["failsafe_cool"]):
        return {"base": "failsafe_range"}
    if not float(data["guard_min"]) < float(data["failsafe_heat"]) or not float(data["failsafe_cool"]) < float(data["guard_max"]):
        return {"base": "failsafe_outside_guard"}
    if float(data["safety_min"]) >= float(data["safety_max"]):
        return {"base": "safety_range"}
    service = str(data.get("notify_service") or "").strip()
    if service and not service.removeprefix("notify.").replace("_", "").isalnum():
        return {"notify_service": "invalid_service"}
    return {}


def _clean_safety(user_input: dict[str, Any]) -> dict[str, Any]:
    return {"notify_service": "", **user_input}


def _validate_schedule(data: dict[str, Any]) -> dict[str, str]:
    if set(data.get("remote_days", [])) & set(data.get("office_days", [])):
        return {"base": "day_overlap"}
    return {}


def _clean_general(user_input: dict[str, Any]) -> dict[str, Any]:
    # Optional selectors drop their key when cleared; make clearing stick.
    return {CONF_WEATHER: None, CONF_PRESENCE: [], **user_input}


# ---------------------------------------------------------------- main flow


class RemoteComfortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: house, schedule and comfort targets."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @property
    def _unit(self) -> str:
        return self.hass.config.units.temperature_unit

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RemoteComfortOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ZONE: ZoneSubentryFlow, SUBENTRY_ROOM: RoomSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {**default_options(self._unit), **_clean_general(user_input)}
            self._name = self._data.pop(CONF_NAME, None) or "Home"
            return await self.async_step_schedule()
        schema = vol.Schema(
            {vol.Required(CONF_NAME, default="Home"): sel.TextSelector(), **_general_schema(self._unit).schema}
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or default_options(self._unit)),
            errors=errors,
        )

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_schedule(user_input)
            if not errors:
                self._data.update(user_input)
                return await self.async_step_comfort()
        return self.async_show_form(
            step_id="schedule",
            data_schema=self.add_suggested_values_to_schema(_SCHEDULE_SCHEMA, user_input or self._data),
            errors=errors,
        )

    async def async_step_comfort(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_safety()
        return self.async_show_form(
            step_id="comfort",
            data_schema=self.add_suggested_values_to_schema(_comfort_schema(self._unit, False), self._data),
        )

    async def async_step_safety(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_safety(user_input)
            if not errors:
                self._data.update(_clean_safety(user_input))
                return self.async_create_entry(title=self._name, data={}, options=self._data)
        return self.async_show_form(
            step_id="safety",
            data_schema=self.add_suggested_values_to_schema(_safety_schema(self._unit), user_input or self._data),
            errors=errors,
        )


class RemoteComfortOptionsFlow(OptionsFlow):
    """Edit everything after setup, grouped into pages."""

    @property
    def _unit(self) -> str:
        return self.hass.config.units.temperature_unit

    @property
    def _current(self) -> dict[str, Any]:
        return {**default_options(self._unit), **self.config_entry.options}

    def _save(self, user_input: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **user_input})

    def _form(self, step_id: str, schema: vol.Schema, errors: dict[str, str] | None = None,
              values: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, values or self._current),
            errors=errors or {},
        )

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["safety", "general", "schedule", "comfort", "weather", "balancing"],
        )

    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(_clean_general(user_input))
        return self._form("general", _general_schema(self._unit))

    async def async_step_safety(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            errors = _validate_safety(user_input)
            if not errors:
                return self._save(_clean_safety(user_input))
            return self._form("safety", _safety_schema(self._unit), errors, user_input)
        return self._form("safety", _safety_schema(self._unit))

    async def async_step_schedule(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            errors = _validate_schedule(user_input)
            if not errors:
                return self._save(user_input)
            return self._form("schedule", _SCHEDULE_SCHEMA, errors, user_input)
        return self._form("schedule", _SCHEDULE_SCHEMA)

    async def async_step_comfort(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        return self._form("comfort", _comfort_schema(self._unit, True))

    async def async_step_weather(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        return self._form("weather", _weather_schema(self._unit))

    async def async_step_balancing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)
        return self._form("balancing", _balancing_schema(self._unit))


# ----------------------------------------------------------- subentry flows


def _subentries(entry: ConfigEntry, kind: str):
    return [s for s in entry.subentries.values() if s.subentry_type == kind]


class ZoneSubentryFlow(ConfigSubentryFlow):
    """An HVAC zone: one thermostat (or zone thermostat on a zoning panel)."""

    def _schema(self) -> vol.Schema:
        units = sorted(
            {s.data.get(CONF_UNIT) for s in _subentries(self._get_entry(), SUBENTRY_ZONE) if s.data.get(CONF_UNIT)}
            | {"Main unit"}
        )
        return vol.Schema(
            {
                vol.Required(CONF_NAME): sel.TextSelector(),
                vol.Required(CONF_CLIMATE): sel.EntitySelector(sel.EntitySelectorConfig(domain="climate")),
                vol.Required(CONF_UNIT, default="Main unit"): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=units, custom_value=True)
                ),
                vol.Optional(CONF_FLOOR): sel.FloorSelector(),
                vol.Required(CONF_COMPENSATE, default=True): _BOOL,
            }
        )

    def _validate(self, user_input: dict[str, Any], current_id: str | None) -> dict[str, str]:
        for sub in _subentries(self._get_entry(), SUBENTRY_ZONE):
            if sub.subentry_id != current_id and sub.data.get(CONF_CLIMATE) == user_input[CONF_CLIMATE]:
                return {CONF_CLIMATE: "climate_in_use"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate(user_input, None)
            if not errors:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(self._schema(), user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = self._validate(user_input, subentry.subentry_id)
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=user_input[CONF_NAME], data=user_input
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(), user_input or {**subentry.data, CONF_NAME: subentry.title}
            ),
            errors=errors,
        )


class RoomSubentryFlow(ConfigSubentryFlow):
    """A room: temperature sensors, the zone that serves it, and what it's used for."""

    def _schema(self) -> vol.Schema:
        zones = [
            sel.SelectOptionDict(value=s.subentry_id, label=s.title)
            for s in _subentries(self._get_entry(), SUBENTRY_ZONE)
        ]
        return vol.Schema(
            {
                vol.Required(CONF_NAME): sel.TextSelector(),
                vol.Required(CONF_ZONE): sel.SelectSelector(sel.SelectSelectorConfig(options=zones)),
                vol.Required(CONF_SENSORS): sel.EntitySelector(
                    sel.EntitySelectorConfig(
                        domain="sensor", device_class=SensorDeviceClass.TEMPERATURE, multiple=True
                    )
                ),
                vol.Required(CONF_ROLE, default="living"): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=ROLES, translation_key="role")
                ),
                vol.Optional(CONF_FLOOR): sel.FloorSelector(),
            }
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if not _subentries(self._get_entry(), SUBENTRY_ZONE):
            return self.async_abort(reason="no_zones")
        if user_input is not None:
            return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(step_id="user", data_schema=self._schema())

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            return self.async_update_and_abort(
                self._get_entry(), subentry, title=user_input[CONF_NAME], data=user_input
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                self._schema(), {**subentry.data, CONF_NAME: subentry.title}
            ),
        )
