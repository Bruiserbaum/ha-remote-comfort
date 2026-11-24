"""Coordinator: gathers Home Assistant state, runs the engine, applies results."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_HOME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

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
    FORECAST_HORIZON,
    FORECAST_INTERVAL,
    LOGGER,
    SUBENTRY_ROOM,
    SUBENTRY_ZONE,
    UPDATE_INTERVAL,
    WEEKDAYS,
    default_options,
)
from .engine import (
    DayType,
    Decision,
    Engine,
    Inputs,
    Mode,
    Period,
    Prefs,
    Role,
    Room,
    Schedule,
    SuppressMethod,
    Weather,
    Zone,
)

if TYPE_CHECKING:
    from . import RemoteComfortConfigEntry

_BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN, None)
_HOLD_GRACE_SECONDS = 90
_TOLERANCE = 0.6


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class _Commanded:
    mode: str
    values: tuple[float | None, ...]
    at: float


class RemoteComfortCoordinator(DataUpdateCoordinator[Decision]):
    """Runs the comfort engine on a timer."""

    config_entry: RemoteComfortConfigEntry

    def __init__(self, hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.engine = Engine()
        self.unit: str = hass.config.units.temperature_unit
        self.mode = Mode.AUTO
        self.enabled = True
        self.day_override: DayType | None = None
        self._day_override_date: date | None = None
        self.weather = Weather()
        self.occupied: bool | None = None
        self.rooms: dict[str, Room] = {}
        self.holds: dict[str, Period] = {}
        self._commanded: dict[str, _Commanded] = {}
        self._forecast_at: datetime | None = None
        self._control_active = False
        self._live_update = False

    # ------------------------------------------------------------ options
    @property
    def options(self) -> dict[str, Any]:
        return {**default_options(self.unit), **self.config_entry.options}

    def _prefs(self, opts: dict[str, Any]) -> Prefs:
        names = {f.name for f in fields(Prefs)}
        kwargs = {k: v for k, v in opts.items() if k in names}
        kwargs["suppress_method"] = SuppressMethod(kwargs.get("suppress_method", "setback"))
        for key in ("wake_lead", "sleep_lead", "return_lead", "max_lead",
                    "balance_min_minutes", "balance_max_minutes", "balance_cooldown_minutes"):
            if key in kwargs:
                kwargs[key] = int(kwargs[key])
        return Prefs(**kwargs)

    @staticmethod
    def _schedule(opts: dict[str, Any]) -> Schedule:
        names = {f.name for f in fields(Schedule)}
        kwargs = {k: v for k, v in opts.items() if k in names}
        kwargs["remote_days"] = {WEEKDAYS.index(d) for d in opts.get("remote_days", [])}
        kwargs["office_days"] = {WEEKDAYS.index(d) for d in opts.get("office_days", [])}
        return Schedule(**kwargs)

    async def async_set_option(self, key: str, value: Any) -> None:
        """Persist a live-adjustable option without reloading the entry."""
        self._live_update = True
        self.hass.config_entries.async_update_entry(
            self.config_entry, options={**self.config_entry.options, key: value}
        )
        await self.async_request_refresh()

    def consume_live_update(self) -> bool:
        was, self._live_update = self._live_update, False
        return was

    # ------------------------------------------------------- user controls
    def set_day_override(self, value: DayType | None) -> None:
        self.day_override = value
        self._day_override_date = dt_util.now().date() if value else None

    def start_control(self) -> None:
        """Begin commanding thermostats (after entities restored their state)."""
        self._control_active = True
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), "remote_comfort_start"
        )

    # -------------------------------------------------------------- inputs
    def _zone_subentries(self):
        return [s for s in self.config_entry.subentries.values() if s.subentry_type == SUBENTRY_ZONE]

    def _room_subentries(self):
        return [s for s in self.config_entry.subentries.values() if s.subentry_type == SUBENTRY_ROOM]

    def climate_entity(self, zone_id: str) -> str | None:
        sub = self.config_entry.subentries.get(zone_id)
        return sub.data.get(CONF_CLIMATE) if sub else None

    def _read_zones(self) -> list[Zone]:
        default_step = 0.5 if self.unit == UnitOfTemperature.CELSIUS else 1.0
        zones = []
        for sub in self._zone_subentries():
            state = self.hass.states.get(sub.data[CONF_CLIMATE])
            if state is None or state.state in _BAD_STATES:
                continue
            attrs = state.attributes
            zones.append(
                Zone(
                    zone_id=sub.subentry_id,
                    name=sub.title,
                    unit=(sub.data.get(CONF_UNIT) or "").strip() or None,
                    hvac_mode=state.state,
                    thermostat_temp=_float(attrs.get(ATTR_CURRENT_TEMPERATURE)),
                    compensate=sub.data.get(CONF_COMPENSATE, True),
                    min_temp=_float(attrs.get(ATTR_MIN_TEMP)),
                    max_temp=_float(attrs.get(ATTR_MAX_TEMP)),
                    step=_float(attrs.get(ATTR_TARGET_TEMP_STEP)) or default_step,
                    floor=sub.data.get(CONF_FLOOR),
                )
            )
        return zones

    def _read_rooms(self) -> list[Room]:
        rooms: dict[str, Room] = {}
        for sub in self._room_subentries():
            readings = []
            for entity_id in sub.data.get(CONF_SENSORS, []):
                state = self.hass.states.get(entity_id)
                if state is not None and (val := _float(state.state)) is not None:
                    readings.append(val)
            rooms[sub.subentry_id] = Room(
                room_id=sub.subentry_id,
                name=sub.title,
                zone_id=sub.data.get(CONF_ZONE, ""),
                role=Role(sub.data.get(CONF_ROLE, "other")),
                temperature=round(sum(readings) / len(readings), 2) if readings else None,
                floor=sub.data.get(CONF_FLOOR),
            )
        self.rooms = rooms
        return list(rooms.values())

    def _read_presence(self, entities: list[str]) -> bool | None:
        known = False
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _BAD_STATES:
                continue
            known = True
            if state.state in (STATE_HOME, STATE_ON):
                return True
            if entity_id.startswith("zone.") and (_float(state.state) or 0) > 0:
                return True
        return False if known else None

    async def _refresh_weather(self, entity_id: str | None, now: datetime) -> None:
        if not entity_id:
            self.weather = Weather()
            return
        state = self.hass.states.get(entity_id)
        current = _float(state.attributes.get(ATTR_TEMPERATURE)) if state else None
        if self._forecast_at and now - self._forecast_at < FORECAST_INTERVAL:
            self.weather.current = current
            return

        high = low = None
        for kind in ("hourly", "daily"):
            try:
                resp = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {ATTR_ENTITY_ID: entity_id, "type": kind},
                    blocking=True,
                    return_response=True,
                )
            except (HomeAssistantError, ValueError) as err:
                LOGGER.debug("No %s forecast from %s: %s", kind, entity_id, err)
                continue
            forecast = (resp or {}).get(entity_id, {}).get("forecast") or []
            if not forecast:
                continue
            if kind == "hourly":
                window = []
                for item in forecast:
                    when = dt_util.parse_datetime(str(item.get("datetime")))
                    if when and now - timedelta(hours=1) <= when <= now + FORECAST_HORIZON:
                        window.append(item)
            else:
                window = forecast[:2] if now.hour >= 15 else forecast[:1]
            highs = [t for i in window if (t := _float(i.get("temperature"))) is not None]
            lows = [
                t
                for i in window
                if (t := _float(i.get("templow", i.get("temperature")))) is not None
            ]
            high = max(highs) if highs else None
            low = min(lows) if lows else None
            break
        self.weather = Weather(high=high, low=low, current=current)
        self._forecast_at = now

    # -------------------------------------------------------------- update
    async def _async_update_data(self) -> Decision:
        now = dt_util.now()
        opts = self.options
        if self._day_override_date and self._day_override_date != now.date():
            self.set_day_override(None)

        await self._refresh_weather(opts.get(CONF_WEATHER), now)
        self.occupied = self._read_presence(opts.get(CONF_PRESENCE) or [])

        previous = self.data.period if self.data else None
        decision = self.engine.evaluate(
            Inputs(
                now=now,
                schedule=self._schedule(opts),
                prefs=self._prefs(opts),
                zones=self._read_zones(),
                rooms=self._read_rooms(),
                weather=self.weather,
                occupied=self.occupied,
                mode=self.mode if self.enabled else Mode.PAUSED,
                day_override=self.day_override,
            )
        )
        if previous is not None and previous != decision.period:
            self.holds.clear()

        if self._control_active:
            await self._apply(decision, opts.get(CONF_RESPECT_MANUAL, True))
        return decision

    async def _apply(self, decision: Decision, respect_manual: bool) -> None:
        for zone_id, dec in decision.zones.items():
            entity_id = self.climate_entity(zone_id)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in _BAD_STATES:
                continue
            try:
                await self._apply_zone(decision, dec, entity_id, state, respect_manual)
            except HomeAssistantError as err:
                LOGGER.warning("Could not update %s: %s", entity_id, err)

    async def _apply_zone(self, decision, dec, entity_id, state, respect_manual) -> None:
        zone_id = dec.zone_id
        restoring = bool(dec.hvac_mode) and dec.hvac_mode != "off"

        # Manual-change detection runs before we touch anything.
        if respect_manual:
            current = self._setpoints(state, dec)
            if current is not None and self._manual_change(zone_id, state.state, current):
                self.holds.setdefault(zone_id, decision.period)
                # Accept the manual value as the new baseline.
                self._commanded[zone_id] = _Commanded(state.state, current, 0)
        if zone_id in self.holds:
            dec.reason = "Manual change detected; holding until the next period"
            if restoring and dec.hvac_mode != state.state:
                await self._call("set_hvac_mode", entity_id, hvac_mode=dec.hvac_mode)
            return

        if dec.hvac_mode and dec.hvac_mode != state.state:
            await self._call("set_hvac_mode", entity_id, hvac_mode=dec.hvac_mode)
            self._commanded.pop(zone_id, None)
            if dec.hvac_mode == "off":
                return
            state = self.hass.states.get(entity_id) or state

        if dec.temperature is not None:
            desired: tuple[float | None, ...] = (dec.temperature,)
            payload: dict[str, float] = {ATTR_TEMPERATURE: dec.temperature}
        elif dec.target_low is not None and dec.target_high is not None:
            desired = (dec.target_low, dec.target_high)
            payload = {ATTR_TARGET_TEMP_LOW: dec.target_low, ATTR_TARGET_TEMP_HIGH: dec.target_high}
        else:
            return
        current = self._setpoints(state, dec) or ()
        if current and not _differs(current, desired):
            self._commanded[zone_id] = _Commanded(state.state, desired, 0)
            return
        await self._call("set_temperature", entity_id, **payload)
        self._commanded[zone_id] = _Commanded(state.state, desired, time.monotonic())

    @staticmethod
    def _setpoints(state, dec) -> tuple[float | None, ...] | None:
        if dec.temperature is not None:
            return (_float(state.attributes.get(ATTR_TEMPERATURE)),)
        if dec.target_low is not None:
            return (
                _float(state.attributes.get(ATTR_TARGET_TEMP_LOW)),
                _float(state.attributes.get(ATTR_TARGET_TEMP_HIGH)),
            )
        return None

    def _manual_change(self, zone_id: str, mode: str, current: tuple) -> bool:
        last = self._commanded.get(zone_id)
        if last is None:
            return False
        if last.mode != mode or len(last.values) != len(current):
            # Mode changed (by someone else); start fresh rather than holding.
            self._commanded.pop(zone_id, None)
            return False
        if last.at and time.monotonic() - last.at < _HOLD_GRACE_SECONDS:
            return False
        return _differs(current, last.values)

    async def _call(self, service: str, entity_id: str, **data: Any) -> None:
        LOGGER.debug("climate.%s %s %s", service, entity_id, data)
        await self.hass.services.async_call(
            "climate", service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
        )

    async def async_release_all(self) -> None:
        """Turn zones we switched off back on (used on unload)."""
        for zone_id, sup in list(self.engine.suppressed.items()):
            entity_id = self.climate_entity(zone_id)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is not None and state.state == "off" and sup.prev_mode != "off":
                try:
                    await self._call("set_hvac_mode", entity_id, hvac_mode=sup.prev_mode)
                except HomeAssistantError as err:
                    LOGGER.warning("Could not restore %s: %s", entity_id, err)
        self.engine.suppressed.clear()


def _differs(current: tuple, desired: tuple) -> bool:
    return any(c is None or d is None or abs(c - d) > _TOLERANCE for c, d in zip(current, desired, strict=False))
