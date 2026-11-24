"""Coordinator: gathers Home Assistant state, runs the engine, applies results.

Fault-tolerance rules (see README "Failure handling"):
- Every input is validated; anything unusable is dropped and reported as a problem,
  and the engine falls back (other rooms -> other sensors -> thermostat reading).
- A thermostat that is unavailable is skipped, but any zone we switched off stays
  remembered (also across restarts) so it is switched back on when it returns.
- Every service call is time-limited and isolated per zone.
- Problems surface on a diagnostic binary sensor; long-lasting ones become Repairs.
- House guard (engine): hard indoor limits that override everything.
- Failsafe (here, independent of the engine): if control stops working for
  FAILSAFE_AFTER, every thermostat is put at the failsafe setpoints.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta
from statistics import median
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
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_HOME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

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
    _hm,
    _Suppression,
)

if TYPE_CHECKING:
    from . import RemoteComfortConfigEntry

_BAD_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN, None, "")
_TOLERANCE = 0.6
HOLD_GRACE = 300  # s: time a (cloud) thermostat gets to reflect our command
CALL_TIMEOUT = 30  # s: per service call
SMALL_CHANGE_INTERVAL = 600  # s: min time between small setpoint nudges
FORECAST_RETRY = timedelta(minutes=5)
FORECAST_MAX_AGE = timedelta(hours=6)
STARTUP_GRACE = timedelta(minutes=5)  # entities may still be loading after boot
REPAIR_AFTER = timedelta(minutes=30)
OUTLIER_LIMIT = {UnitOfTemperature.FAHRENHEIT: 5.0, UnitOfTemperature.CELSIUS: 3.0}
PLAUSIBLE = {UnitOfTemperature.FAHRENHEIT: (-20.0, 130.0), UnitOfTemperature.CELSIUS: (-30.0, 55.0)}
STORAGE_VERSION = 1
FAILSAFE_AFTER = 600  # s without a successful cycle before the failsafe kicks in
FAILSAFE_REAPPLY = 1800  # s between failsafe re-applications while broken
WATCHDOG_INTERVAL = timedelta(minutes=1)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


@dataclass(slots=True)
class _Commanded:
    mode: str
    values: tuple[float | None, ...]
    before: tuple[float | None, ...]
    at: float  # monotonic time of the command, 0 if we only observed it
    target: float | None = None  # zone target behind the command (before compensation)


@dataclass(slots=True)
class Problem:
    key: str  # stable id (usually the entity id)
    message: str
    entity_id: str | None = None
    used_by: str | None = None
    missing: bool = False  # entity doesn't exist at all (vs. temporarily bad)


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
        self.weather_updated: datetime | None = None
        self.occupied: bool | None = None
        self.rooms: dict[str, Room] = {}
        self.sensor_status: dict[str, str] = {}  # entity_id -> "ok" / reason
        self.holds: dict[str, Period] = {}
        self.problems: dict[str, Problem] = {}
        self.unavailable_zones: list[str] = []
        self._commanded: dict[str, _Commanded] = {}
        self._turned_off_at: dict[str, float] = {}
        self._call_failures: dict[str, str] = {}
        self._problem_since: dict[str, datetime] = {}
        self._forecast_attempt: datetime | None = None
        self._control_active = False
        self._live_update = False
        self._started = dt_util.utcnow()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._saved_state: dict[str, Any] | None = None
        self._last_ok = time.monotonic()
        self.failsafe_active = False
        self._failsafe_at = 0.0

    # ------------------------------------------------------------ options
    @property
    def options(self) -> dict[str, Any]:
        return {**default_options(self.unit), **self.config_entry.options}

    def _prefs(self, opts: dict[str, Any], problems: list[Problem]) -> Prefs:
        kwargs: dict[str, Any] = {}
        for f in fields(Prefs):
            if f.name not in opts:
                continue
            value = opts[f.name]
            kind = str(f.type)
            try:
                if kind == "SuppressMethod":
                    value = SuppressMethod(value)
                elif kind == "str":
                    _hm(value)
                elif kind == "bool":
                    value = bool(value)
                elif kind == "int":
                    value = int(float(value))
                elif kind == "float":
                    value = _float(value)
                    if value is None:
                        raise ValueError
            except (TypeError, ValueError, IndexError):
                problems.append(Problem(f"option_{f.name}", f"Setting '{f.name}' is invalid ({value!r}); using default"))
                continue
            kwargs[f.name] = value
        prefs = Prefs(**kwargs)
        if prefs.safety_min >= prefs.safety_max:
            prefs.safety_min, prefs.safety_max = Prefs.safety_min, Prefs.safety_max
        return prefs

    def _schedule(self, opts: dict[str, Any], problems: list[Problem]) -> Schedule:
        defaults = default_options(self.unit)
        kwargs: dict[str, Any] = {}
        for f in fields(Schedule):
            if f.name in ("remote_days", "office_days"):
                kwargs[f.name] = {
                    WEEKDAYS.index(d) for d in (opts.get(f.name) or []) if d in WEEKDAYS
                }
                continue
            value = opts.get(f.name, defaults.get(f.name))
            try:
                _hm(value)
            except (TypeError, ValueError, IndexError):
                problems.append(Problem(f"option_{f.name}", f"Time '{f.name}' is invalid ({value!r}); using default"))
                value = defaults.get(f.name, f.default)
            kwargs[f.name] = value
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
    def set_day_override(self, value: DayType | None, for_date: date | None = None) -> None:
        self.day_override = value
        self._day_override_date = (for_date or dt_util.now().date()) if value else None

    @property
    def day_override_date(self) -> date | None:
        return self._day_override_date

    async def async_load_state(self) -> None:
        """Restore balancing state saved before a restart."""
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001 - corrupt storage must not block setup
            LOGGER.exception("Could not read saved state; starting fresh")
            data = None
        if data:
            self.engine.import_state(data)
        self._saved_state = self.engine.export_state()

    def _save_state(self) -> None:
        state = self.engine.export_state()
        if state != self._saved_state:
            self._saved_state = state
            self._store.async_delay_save(lambda: state, 1)

    def start_control(self) -> None:
        """Begin commanding thermostats (after entities restored their state)."""
        self._control_active = True
        self._last_ok = time.monotonic()
        self.config_entry.async_on_unload(
            async_track_time_interval(self.hass, self._async_watchdog, WATCHDOG_INTERVAL)
        )
        self.config_entry.async_create_task(
            self.hass, self.async_request_refresh(), "remote_comfort_start"
        )

    # -------------------------------------------------------------- inputs
    def _subentries(self, kind: str):
        return [s for s in self.config_entry.subentries.values() if s.subentry_type == kind]

    def climate_entity(self, zone_id: str) -> str | None:
        sub = self.config_entry.subentries.get(zone_id)
        return sub.data.get(CONF_CLIMATE) if sub else None

    def _in_startup(self) -> bool:
        return dt_util.utcnow() - self._started < STARTUP_GRACE

    def _entity_problem(self, entity_id: str, used_by: str, state: State | None) -> Problem:
        if state is None:
            return Problem(entity_id, f"{entity_id} ({used_by}) not found", entity_id, used_by, missing=True)
        return Problem(entity_id, f"{entity_id} ({used_by}) is {state.state}", entity_id, used_by)

    def _temperature(self, state: State, value: Any) -> float | None:
        """Parse a temperature, convert to the house unit, reject nonsense."""
        val = _float(value)
        if val is None:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit in TemperatureConverter.VALID_UNITS and unit != self.unit:
            val = TemperatureConverter.convert(val, unit, self.unit)
        lo, hi = PLAUSIBLE.get(self.unit, PLAUSIBLE[UnitOfTemperature.FAHRENHEIT])
        return round(val, 2) if lo <= val <= hi else None

    def _read_zones(self, problems: list[Problem]) -> list[Zone]:
        default_step = 0.5 if self.unit == UnitOfTemperature.CELSIUS else 1.0
        zones: list[Zone] = []
        unavailable: list[str] = []
        for sub in self._subentries(SUBENTRY_ZONE):
            entity_id = sub.data.get(CONF_CLIMATE)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in _BAD_STATES:
                unavailable.append(sub.title)
                if entity_id:
                    problems.append(self._entity_problem(entity_id, f"{sub.title} zone thermostat", state))
                continue
            attrs = state.attributes
            step = _float(attrs.get(ATTR_TARGET_TEMP_STEP))
            min_temp = _float(attrs.get(ATTR_MIN_TEMP))
            max_temp = _float(attrs.get(ATTR_MAX_TEMP))
            if min_temp is not None and max_temp is not None and min_temp >= max_temp:
                min_temp = max_temp = None
            zones.append(
                Zone(
                    zone_id=sub.subentry_id,
                    name=sub.title,
                    unit=(str(sub.data.get(CONF_UNIT) or "")).strip() or None,
                    hvac_mode=state.state,
                    thermostat_temp=self._temperature(state, attrs.get(ATTR_CURRENT_TEMPERATURE)),
                    compensate=bool(sub.data.get(CONF_COMPENSATE, True)),
                    min_temp=min_temp,
                    max_temp=max_temp,
                    step=step if step and step > 0 else default_step,
                    floor=sub.data.get(CONF_FLOOR),
                    hvac_modes=[str(m) for m in attrs.get("hvac_modes") or []] or None,
                )
            )
        self.unavailable_zones = unavailable
        return zones

    def _read_rooms(self, problems: list[Problem], max_age_min: float) -> list[Room]:
        rooms: dict[str, Room] = {}
        status: dict[str, str] = {}
        now = dt_util.utcnow()
        zone_ids = {s.subentry_id for s in self._subentries(SUBENTRY_ZONE)}
        outlier = OUTLIER_LIMIT.get(self.unit, 5.0)
        for sub in self._subentries(SUBENTRY_ROOM):
            used_by = f"{sub.title} room sensor"
            readings: dict[str, float] = {}
            for entity_id in sub.data.get(CONF_SENSORS) or []:
                state = self.hass.states.get(entity_id)
                if state is None or state.state in _BAD_STATES:
                    status[entity_id] = "missing" if state is None else state.state
                    problems.append(self._entity_problem(entity_id, used_by, state))
                    continue
                val = self._temperature(state, state.state)
                if val is None:
                    status[entity_id] = f"implausible reading {state.state!r}"
                    problems.append(Problem(entity_id, f"{entity_id} ({used_by}) reads {state.state!r}; ignored", entity_id, used_by))
                    continue
                seen = getattr(state, "last_reported", None) or state.last_updated
                if max_age_min and now - seen > timedelta(minutes=max_age_min):
                    status[entity_id] = "stale"
                    problems.append(
                        Problem(entity_id, f"{entity_id} ({used_by}) hasn't reported for over {int(max_age_min)} min; ignored", entity_id, used_by)
                    )
                    continue
                readings[entity_id] = val
                status[entity_id] = "ok"
            # With 3+ sensors, drop any that disagree wildly with the rest.
            if len(readings) >= 3:
                mid = median(readings.values())
                for entity_id, val in list(readings.items()):
                    if abs(val - mid) > outlier:
                        readings.pop(entity_id)
                        status[entity_id] = f"outlier ({val:g} vs median {mid:g})"
                        problems.append(Problem(entity_id, f"{entity_id} ({used_by}) disagrees with the room's other sensors; ignored", entity_id, used_by))
            zone_id = sub.data.get(CONF_ZONE, "")
            if zone_id not in zone_ids:
                problems.append(Problem(f"room_{sub.subentry_id}", f"Room {sub.title} is assigned to a zone that no longer exists"))
            elif not readings and sub.data.get(CONF_SENSORS):
                problems.append(Problem(f"room_{sub.subentry_id}", f"Room {sub.title} has no working sensors; using fallbacks"))
            try:
                role = Role(sub.data.get(CONF_ROLE, "other"))
            except ValueError:
                role = Role.OTHER
            values = list(readings.values())
            rooms[sub.subentry_id] = Room(
                room_id=sub.subentry_id,
                name=sub.title,
                zone_id=zone_id,
                role=role,
                temperature=round(sum(values) / len(values), 2) if values else None,
                floor=sub.data.get(CONF_FLOOR),
            )
        self.rooms = rooms
        self.sensor_status = status
        return list(rooms.values())

    def _read_presence(self, entities: list[str], problems: list[Problem]) -> bool | None:
        known = False
        home = False
        for entity_id in entities:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in _BAD_STATES:
                problems.append(self._entity_problem(entity_id, "presence", state))
                continue
            known = True
            if state.state in (STATE_HOME, STATE_ON):
                home = True
            elif entity_id.startswith("zone.") and (_float(state.state) or 0) > 0:
                home = True
        # Unknown presence (None) means "don't use presence", never "nobody home".
        return home if known else None

    async def _refresh_weather(self, entity_id: str | None, now: datetime, problems: list[Problem]) -> None:
        if not entity_id:
            self.weather = Weather()
            self.weather_updated = None
            return
        state = self.hass.states.get(entity_id)
        current = self._temperature(state, state.attributes.get(ATTR_TEMPERATURE)) if state else None
        if state is None or state.state in _BAD_STATES:
            problems.append(self._entity_problem(entity_id, "weather forecast", state))

        fresh = self.weather_updated and now - self.weather_updated < FORECAST_INTERVAL
        retried = self._forecast_attempt and now - self._forecast_attempt < FORECAST_RETRY
        if not fresh and not retried and state is not None and state.state not in _BAD_STATES:
            self._forecast_attempt = now
            result = await self._fetch_forecast(entity_id, state, now)
            if result is not None:
                self.weather = Weather(high=result[0], low=result[1], current=current)
                self.weather_updated = now

        if self.weather_updated and now - self.weather_updated > FORECAST_MAX_AGE:
            # Too old to act on; run without weather adjustments.
            self.weather = Weather(current=current)
            self.weather_updated = None
        self.weather.current = current
        if self.weather_updated is None:
            problems.append(
                Problem("weather_forecast", f"No usable forecast from {entity_id}; weather adjustments paused",
                        entity_id, "weather forecast")
            )

    async def _fetch_forecast(self, entity_id: str, state: State, now: datetime) -> tuple[float | None, float | None] | None:
        for kind in ("hourly", "daily"):
            try:
                async with asyncio.timeout(CALL_TIMEOUT):
                    resp = await self.hass.services.async_call(
                        "weather",
                        "get_forecasts",
                        {ATTR_ENTITY_ID: entity_id, "type": kind},
                        blocking=True,
                        return_response=True,
                    )
            except Exception as err:  # noqa: BLE001 - any failure just means "no forecast"
                LOGGER.debug("No %s forecast from %s: %s", kind, entity_id, err)
                continue
            try:
                forecast = list((resp or {}).get(entity_id, {}).get("forecast") or [])
            except (AttributeError, TypeError):
                continue
            if kind == "hourly":
                window = []
                for item in forecast:
                    when = dt_util.parse_datetime(str(item.get("datetime", "")))
                    if when and now - timedelta(hours=1) <= when <= now + FORECAST_HORIZON:
                        window.append(item)
            else:
                window = forecast[:2] if now.hour >= 15 else forecast[:1]
            highs = [t for i in window if (t := self._temperature(state, i.get("temperature"))) is not None]
            lows = [
                t
                for i in window
                if (t := self._temperature(state, i.get("templow", i.get("temperature")))) is not None
            ]
            if highs or lows:
                return (max(highs) if highs else None, min(lows) if lows else None)
        return None

    # -------------------------------------------------------------- update
    async def _async_update_data(self) -> Decision:
        try:
            return await self._update()
        except UpdateFailed:
            raise
        except Exception as err:
            LOGGER.exception("Remote Comfort update failed")
            raise UpdateFailed(f"Update failed: {err}") from err

    async def _update(self) -> Decision:
        now = dt_util.now()
        opts = self.options
        problems: list[Problem] = []
        if self._day_override_date and self._day_override_date != now.date():
            self.set_day_override(None)

        prefs = self._prefs(opts, problems)
        schedule = self._schedule(opts, problems)
        await self._refresh_weather(opts.get(CONF_WEATHER), now, problems)
        self.occupied = self._read_presence(opts.get(CONF_PRESENCE) or [], problems)
        zones = self._read_zones(problems)
        rooms = self._read_rooms(problems, _float(opts.get("sensor_max_age")) or 0)

        self._detect_turned_back_on(zones, prefs, now)

        previous = self.data.period if self.data else None
        # Until control starts, evaluate on a scratch copy: decisions that are
        # never sent (like "switch this zone back on") must not be forgotten.
        engine = self.engine if self._control_active else copy.deepcopy(self.engine)
        guarded_before = dict(self.engine.guarded)
        decision = engine.evaluate(
            Inputs(
                now=now,
                schedule=schedule,
                prefs=prefs,
                zones=zones,
                rooms=rooms,
                weather=self.weather,
                occupied=self.occupied,
                mode=self.mode if self.enabled else Mode.PAUSED,
                day_override=self.day_override,
                configured_zone_ids={s.subentry_id for s in self._subentries(SUBENTRY_ZONE)},
            )
        )
        if previous is not None and previous != decision.period:
            self.holds.clear()

        if self._control_active:
            await self._apply(decision, bool(opts.get(CONF_RESPECT_MANUAL, True)))
        for zone_id, err in self._call_failures.items():
            entity_id = self.climate_entity(zone_id) or zone_id
            problems.append(Problem(f"call_{zone_id}", f"Could not update {entity_id}: {err}", entity_id))
        for zone_id in engine.guarded:
            dec = decision.zones.get(zone_id)
            name = dec.name if dec else zone_id
            problems.append(Problem(f"guard_{zone_id}", f"House guard active for {name}: {dec.reason if dec else ''}"))
        self._save_state()
        self._update_problems(problems)
        if self._control_active:
            await self._announce_guard(guarded_before, decision)
            self._last_ok = time.monotonic()
            if self.failsafe_active:
                self.failsafe_active = False
                await self._notify(
                    "failsafe", "Remote Comfort recovered",
                    "Normal control has resumed; the failsafe setpoints are no longer in use.",
                    dismiss=True,
                )
        return decision

    async def _announce_guard(self, before: dict, decision: Decision) -> None:
        after = self.engine.guarded
        for zone_id in after.keys() - before.keys():
            dec = decision.zones.get(zone_id)
            name = dec.name if dec else zone_id
            limit = "too hot" if after[zone_id].direction == "cool" else "too cold"
            await self._notify(
                f"guard_{zone_id}",
                f"Remote Comfort: {name} is {limit}",
                f"{dec.reason if dec else ''}. The house guard is overriding the schedule, "
                "holds and pause until it recovers.",
            )
        for zone_id in before.keys() - after.keys():
            name = self.config_entry.subentries[zone_id].title if zone_id in self.config_entry.subentries else zone_id
            await self._notify(
                f"guard_{zone_id}", f"Remote Comfort: {name} is back in range",
                "The house guard has ended; normal control resumed.", dismiss=True,
            )

    async def _notify(self, key: str, title: str, message: str, dismiss: bool = False) -> None:
        """Persistent notification plus optional notify service. Never raises."""
        notification_id = f"{DOMAIN}_{self.config_entry.entry_id}_{key}"
        try:
            if dismiss:
                await self.hass.services.async_call(
                    "persistent_notification", "dismiss", {"notification_id": notification_id}, blocking=True
                )
            else:
                await self.hass.services.async_call(
                    "persistent_notification", "create",
                    {"title": title, "message": message, "notification_id": notification_id}, blocking=True,
                )
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("Persistent notification failed: %s", err)
        service = str(self.options.get("notify_service") or "").strip().removeprefix("notify.")
        if service:
            try:
                async with asyncio.timeout(CALL_TIMEOUT):
                    await self.hass.services.async_call(
                        "notify", service, {"title": title, "message": message}, blocking=True
                    )
            except Exception as err:  # noqa: BLE001
                LOGGER.warning("Could not send notification via notify.%s: %s", service, err)

    # ------------------------------------------------------------- failsafe
    async def _async_watchdog(self, _now: datetime | None = None) -> None:
        if not self._control_active or not self.enabled:
            return
        if time.monotonic() - self._last_ok < FAILSAFE_AFTER:
            return
        if self.failsafe_active and time.monotonic() - self._failsafe_at < FAILSAFE_REAPPLY:
            return
        error = self.last_exception or "no successful update"
        await self.async_failsafe(f"Remote Comfort hasn't completed an update for over {FAILSAFE_AFTER // 60} minutes ({error})")

    async def async_failsafe(self, reason: str) -> None:
        """Put every thermostat at the failsafe setpoints.

        Deliberately simple and independent of the engine: raw states, options
        with defaults, and per-zone isolation.
        """
        LOGGER.error("Failsafe: %s", reason)
        defaults = default_options(self.unit)
        opts = self.config_entry.options

        def num(key: str) -> float:
            value = _float(opts.get(key))
            return value if value is not None else float(defaults[key])

        cool, heat = num("failsafe_cool"), num("failsafe_heat")
        guard_min, guard_max = num("guard_min"), num("guard_max")
        guard_on = opts.get("guard_enabled", True) is not False and opts.get("guard_switch_mode", True) is not False
        if heat >= cool:
            cool, heat = float(defaults["failsafe_cool"]), float(defaults["failsafe_heat"])

        for sub in self._subentries(SUBENTRY_ZONE):
            entity_id = sub.data.get(CONF_CLIMATE)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in _BAD_STATES:
                continue
            try:
                attrs = state.attributes
                modes = [str(m) for m in attrs.get("hvac_modes") or []]
                mode = state.state
                sup = self.engine.suppressed.get(sub.subentry_id)
                if mode == "off" and sup is not None:
                    mode = sup.prev_mode  # we switched it off: switch it back on
                temp = self._temperature(state, attrs.get(ATTR_CURRENT_TEMPERATURE))
                if guard_on and temp is not None and (not modes or "heat" in modes) \
                        and temp <= guard_min and mode not in ("heat", "heat_cool", "auto"):
                    mode = "heat"
                elif guard_on and temp is not None and (not modes or "cool" in modes) \
                        and temp >= guard_max and mode not in ("cool", "heat_cool", "auto"):
                    mode = "cool"
                if mode != state.state:
                    await self._call("set_hvac_mode", entity_id, hvac_mode=mode)
                lo, hi = _float(attrs.get(ATTR_MIN_TEMP)), _float(attrs.get(ATTR_MAX_TEMP))

                def fit(value: float) -> float:
                    if lo is not None:
                        value = max(lo, value)
                    if hi is not None:
                        value = min(hi, value)
                    return value

                if mode == "cool":
                    await self._call("set_temperature", entity_id, **{ATTR_TEMPERATURE: fit(cool)})
                elif mode == "heat":
                    await self._call("set_temperature", entity_id, **{ATTR_TEMPERATURE: fit(heat)})
                elif mode in ("heat_cool", "auto"):
                    await self._call(
                        "set_temperature", entity_id,
                        **{ATTR_TARGET_TEMP_LOW: fit(heat), ATTR_TARGET_TEMP_HIGH: fit(cool)},
                    )
                self._commanded.pop(sub.subentry_id, None)
            except Exception as err:  # noqa: BLE001
                LOGGER.error("Failsafe could not set %s: %s", entity_id, err)
        try:
            self.engine.suppressed.clear()
        except Exception:  # noqa: BLE001
            pass
        self.failsafe_active = True
        self._failsafe_at = time.monotonic()
        await self._notify(
            "failsafe",
            "Remote Comfort failsafe active",
            f"{reason}. All thermostats were set to the failsafe setpoints "
            f"(heat {heat:g}°, cool {cool:g}°). Check Settings → System → Logs.",
        )

    async def async_on_stop(self, _event: Any = None) -> None:
        """HA is shutting down: don't leave zones switched off for balancing."""
        await self.async_release_all()

    def _detect_turned_back_on(self, zones: list[Zone], prefs: Prefs, now: datetime) -> None:
        """A person switched on a zone we had switched off: let go of it."""
        for zone in zones:
            sup = self.engine.suppressed.get(zone.zone_id)
            sent = self._turned_off_at.get(zone.zone_id)
            if sup is None or prefs.suppress_method is not SuppressMethod.OFF or zone.hvac_mode == "off":
                continue
            # Only if we really switched it off (and gave it time to report that).
            if sent is not None and time.monotonic() - sent > HOLD_GRACE:
                self.engine.release_zone(zone.zone_id, now, prefs.balance_cooldown_minutes)
                self._turned_off_at.pop(zone.zone_id, None)
                if self.data is not None:
                    self.holds.setdefault(zone.zone_id, self.data.period)

    # ------------------------------------------------------------- problems
    def _update_problems(self, problems: list[Problem]) -> None:
        now = dt_util.utcnow()
        startup = self._in_startup()
        current: dict[str, Problem] = {}
        for problem in problems:
            # Right after boot, entities may simply not be loaded yet.
            if startup and problem.entity_id and not problem.key.startswith("call_"):
                continue
            current.setdefault(problem.key, problem)
        for key in list(self._problem_since):
            if key not in current:
                self._problem_since.pop(key)
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(key))
        for key, problem in current.items():
            since = self._problem_since.setdefault(key, now)
            if problem.entity_id and now - since >= REPAIR_AFTER:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._issue_id(key),
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="entity_missing" if problem.missing else "entity_unavailable",
                    translation_placeholders={
                        "entity_id": problem.entity_id,
                        "used_by": problem.used_by or "Remote Comfort",
                        "name": self.config_entry.title,
                    },
                )
        self.problems = current

    def _issue_id(self, key: str) -> str:
        return f"{self.config_entry.entry_id}_{key}"

    def async_clear_issues(self) -> None:
        for key in list(self._problem_since):
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(key))
        self._problem_since.clear()

    # ---------------------------------------------------------------- apply
    async def _apply(self, decision: Decision, respect_manual: bool) -> None:
        for zone_id, dec in decision.zones.items():
            entity_id = self.climate_entity(zone_id)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in _BAD_STATES:
                continue
            try:
                await self._apply_zone(decision, dec, entity_id, state, respect_manual)
            except Exception as err:  # noqa: BLE001 - one bad thermostat must not stop the others
                LOGGER.warning("Could not update %s: %s", entity_id, err)
                self._call_failures[zone_id] = str(err) or type(err).__name__
                if dec.hvac_mode and dec.hvac_mode != "off" and zone_id not in self.engine.suppressed:
                    # Switching a zone back on failed: keep the record and retry.
                    self.engine.suppressed[zone_id] = _Suppression(
                        since=dt_util.now() - timedelta(days=1), prev_mode=dec.hvac_mode
                    )
            else:
                self._call_failures.pop(zone_id, None)
        for zone_id in list(self._call_failures):
            if zone_id not in decision.zones:
                self._call_failures.pop(zone_id)

    async def _apply_zone(self, decision, dec, entity_id, state, respect_manual) -> None:
        zone_id = dec.zone_id
        restoring = bool(dec.hvac_mode) and dec.hvac_mode != "off"

        # Manual-change detection runs before we touch anything.
        if respect_manual and not dec.guard:
            current = self._setpoints(state, dec)
            if current is not None and self._manual_change(zone_id, state.state, current):
                self.holds.setdefault(zone_id, decision.period)
                # Accept the manual value as the new baseline.
                self._commanded[zone_id] = _Commanded(state.state, current, current, 0)
        if zone_id in self.holds and not dec.guard:
            dec.reason = "Manual change detected; holding until the next period"
            if restoring and dec.hvac_mode != state.state:
                await self._call("set_hvac_mode", entity_id, hvac_mode=dec.hvac_mode)
            return

        if dec.hvac_mode and dec.hvac_mode != state.state:
            await self._call("set_hvac_mode", entity_id, hvac_mode=dec.hvac_mode)
            self._commanded.pop(zone_id, None)
            if dec.hvac_mode == "off":
                self._turned_off_at[zone_id] = time.monotonic()
                return
            self._turned_off_at.pop(zone_id, None)
            state = self.hass.states.get(entity_id) or state
        elif state.state == "off":
            return  # off and we're not changing that: nothing to set

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
            self._commanded[zone_id] = _Commanded(state.state, desired, current, 0, dec.target)
            return

        # Small nudges (from room compensation) at most every 10 minutes.
        last = self._commanded.get(zone_id)
        step = _float(state.attributes.get(ATTR_TARGET_TEMP_STEP)) or (0.5 if self.unit == UnitOfTemperature.CELSIUS else 1.0)
        if (
            last is not None
            and last.at
            and not dec.suppressed
            and not dec.guard
            and last.target == dec.target
            and not _differs(current, last.values)  # thermostat applied our last command
            and time.monotonic() - last.at < SMALL_CHANGE_INTERVAL
            and len(last.values) == len(desired)
            and not _differs(last.values, desired, step + 0.01)
        ):
            return
        await self._call("set_temperature", entity_id, **payload)
        self._commanded[zone_id] = _Commanded(state.state, desired, current, time.monotonic(), dec.target)

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
        if any(c is None for c in current):
            return False
        if last.at and time.monotonic() - last.at < HOLD_GRACE:
            return False
        if not _differs(current, last.values):
            return False
        # Still showing the value from before our command: the thermostat
        # never applied it (or the cloud is lagging). Re-send, don't hold.
        if len(last.before) == len(current) and not _differs(current, last.before):
            return False
        return True

    async def _call(self, service: str, entity_id: str, **data: Any) -> None:
        LOGGER.debug("climate.%s %s %s", service, entity_id, data)
        async with asyncio.timeout(CALL_TIMEOUT):
            await self.hass.services.async_call(
                "climate", service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=True
            )

    async def async_release_all(self) -> None:
        """Turn zones we switched off back on (used on unload)."""
        for zone_id, sup in list(self.engine.suppressed.items()):
            entity_id = self.climate_entity(zone_id)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in _BAD_STATES:
                continue  # keep the record; restored when it comes back
            if state.state == "off" and sup.prev_mode != "off":
                try:
                    await self._call("set_hvac_mode", entity_id, hvac_mode=sup.prev_mode)
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("Could not restore %s: %s", entity_id, err)
                    continue
            self.engine.suppressed.pop(zone_id, None)
        state = self.engine.export_state()
        try:
            await self._store.async_save(state)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Could not save state")


def _differs(current: tuple, desired: tuple, tolerance: float = _TOLERANCE) -> bool:
    return any(
        c is None or d is None or abs(c - d) > tolerance
        for c, d in zip(current, desired, strict=False)
    )
