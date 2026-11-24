"""Decision engine for Remote Comfort.

This module is deliberately free of Home Assistant imports so the comfort
logic can be unit tested in isolation. The coordinator gathers state from
Home Assistant, feeds it in as plain dataclasses, and applies the result.

All temperatures (setpoints, offsets, thresholds) are in the house's unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import fmean


class Period(StrEnum):
    """What the house is doing right now."""

    HOME = "home"
    WORKING = "working"
    AWAY = "away"
    RETURNING = "returning"
    SLEEP = "sleep"
    VACATION = "vacation"
    PAUSED = "paused"


class DayType(StrEnum):
    REMOTE = "remote"
    OFFICE = "office"
    OFF = "off"


class Mode(StrEnum):
    """User override for the whole house."""

    AUTO = "auto"
    HOME = "home"
    AWAY = "away"
    SLEEP = "sleep"
    VACATION = "vacation"
    PAUSED = "paused"


class Role(StrEnum):
    BEDROOM = "bedroom"
    OFFICE = "office"
    LIVING = "living"
    OTHER = "other"


class SuppressMethod(StrEnum):
    SETBACK = "setback"
    OFF = "off"


OCCUPIED = {Period.HOME, Period.WORKING, Period.RETURNING, Period.SLEEP}

# Which rooms "count" when judging comfort in each period.
ACTIVE_ROLES: dict[Period, set[Role]] = {
    Period.SLEEP: {Role.BEDROOM},
    Period.WORKING: {Role.OFFICE, Role.LIVING},
    Period.HOME: {Role.LIVING, Role.OFFICE, Role.OTHER},
    Period.RETURNING: {Role.LIVING, Role.OFFICE, Role.OTHER},
}


def _hm(value: str) -> int:
    """'HH:MM[:SS]' -> minutes after midnight."""
    parts = value.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _in_window(minute: int, start: int, end: int) -> bool:
    """Half-open window [start, end) that may wrap past midnight."""
    start %= 1440
    end %= 1440
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


@dataclass(slots=True)
class Schedule:
    remote_days: set[int]  # 0 = Monday
    office_days: set[int]
    wake_time: str = "06:30"
    off_day_wake_time: str = "08:00"
    sleep_time: str = "22:30"
    work_start: str = "08:00"
    work_end: str = "17:00"
    leave_time: str = "07:30"
    return_time: str = "17:30"

    def day_type(self, weekday: int) -> DayType:
        if weekday in self.remote_days:
            return DayType.REMOTE
        if weekday in self.office_days:
            return DayType.OFFICE
        return DayType.OFF


@dataclass(slots=True)
class Prefs:
    comfort_cool: float = 74
    comfort_heat: float = 69
    sleep_cool: float = 70
    sleep_heat: float = 66
    away_cool: float = 80
    away_heat: float = 62
    vacation_offset: float = 4
    min_gap: float = 3  # minimum distance between heat and cool targets

    # Lead times (minutes) for reaching the next period's targets.
    wake_lead: int = 30
    sleep_lead: int = 30
    return_lead: int = 45
    lead_per_degree: float = 6  # extra minutes per degree beyond the weather threshold
    max_lead: int = 180

    # Weather
    hot_threshold: float = 90
    cold_threshold: float = 25
    precool_offset: float = 2
    precool_until: str = "14:00"
    hot_away_cap: float = 3  # on hot days, away cool target is at most comfort + this
    cold_away_cap: float = 4  # on cold days, away heat target is at least comfort - this

    # Zones
    idle_zone_offset: float = 2  # relax zones with no active rooms while occupied
    compensation_limit: float = 3

    # Balancing (zones that share one HVAC unit)
    balance_enabled: bool = True
    balance_trigger: float = 1  # leading zone must be this far from target
    balance_spread: float = 3  # ...and this much worse than the zone we close
    balance_safety: float = 3  # never let a closed zone drift further than this
    balance_setback: float = 5
    balance_min_minutes: int = 10
    balance_max_minutes: int = 45
    balance_cooldown_minutes: int = 15
    suppress_method: SuppressMethod = SuppressMethod.SETBACK

    # Presence
    presence_sets_away: bool = False


@dataclass(slots=True)
class Room:
    room_id: str
    name: str
    zone_id: str
    role: Role
    temperature: float | None
    floor: str | None = None


@dataclass(slots=True)
class Zone:
    zone_id: str
    name: str
    unit: str | None
    hvac_mode: str  # as reported by the thermostat
    thermostat_temp: float | None
    compensate: bool = True
    min_temp: float | None = None
    max_temp: float | None = None
    step: float = 1.0
    floor: str | None = None


@dataclass(slots=True)
class Weather:
    high: float | None = None  # forecast high over the next ~18 hours
    low: float | None = None  # forecast low over the next ~18 hours
    current: float | None = None


@dataclass(slots=True)
class Inputs:
    now: datetime
    schedule: Schedule
    prefs: Prefs
    zones: list[Zone]
    rooms: list[Room]
    weather: Weather = field(default_factory=Weather)
    occupied: bool | None = None
    mode: Mode = Mode.AUTO
    day_override: DayType | None = None


@dataclass(slots=True)
class ZoneDecision:
    zone_id: str
    name: str
    comfort_temp: float | None
    comfort_source: str  # "rooms", "all rooms", "thermostat", "none"
    active_rooms: list[str]
    direction: str | None  # "cool", "heat", "dual" or None when not controlled
    target: float | None  # target in the active direction
    demand: float | None  # degrees away from target (positive = needs conditioning)
    temperature: float | None = None  # setpoint to send (cool/heat modes)
    target_low: float | None = None  # setpoints to send (heat_cool mode)
    target_high: float | None = None
    hvac_mode: str | None = None  # mode to set, when we change it
    suppressed: bool = False
    reason: str = ""


@dataclass(slots=True)
class Decision:
    period: Period
    day_type: DayType
    cool_target: float
    heat_target: float
    hot_day: bool
    cold_day: bool
    return_lead: int
    reasons: list[str]
    zones: dict[str, ZoneDecision]


@dataclass(slots=True)
class _Suppression:
    since: datetime
    prev_mode: str


class Engine:
    """Stateful decision engine (state = active balancing suppressions)."""

    def __init__(self) -> None:
        self.suppressed: dict[str, _Suppression] = {}
        self.cooldown_until: dict[str, datetime] = {}

    # ------------------------------------------------------------------ period
    @staticmethod
    def _leads(prefs: Prefs, weather: Weather) -> tuple[bool, bool, int, int]:
        hot = weather.high is not None and weather.high >= prefs.hot_threshold
        cold = weather.low is not None and weather.low <= prefs.cold_threshold
        extra = 0.0
        if hot:
            extra = max(extra, (weather.high - prefs.hot_threshold) * prefs.lead_per_degree)
        if cold:
            extra = max(extra, (prefs.cold_threshold - weather.low) * prefs.lead_per_degree)
        return_lead = min(prefs.max_lead, int(prefs.return_lead + extra))
        wake_lead = min(prefs.max_lead, int(prefs.wake_lead + extra / 2))
        return hot, cold, return_lead, wake_lead

    @staticmethod
    def scheduled_period(
        now: datetime,
        schedule: Schedule,
        prefs: Prefs,
        day_type: DayType,
        return_lead: int,
        wake_lead: int,
    ) -> Period:
        minute = now.hour * 60 + now.minute
        wake = _hm(schedule.off_day_wake_time if day_type is DayType.OFF else schedule.wake_time)
        sleep = _hm(schedule.sleep_time)
        if _in_window(minute, sleep - prefs.sleep_lead, wake - wake_lead):
            return Period.SLEEP
        if day_type is DayType.REMOTE:
            if _in_window(minute, _hm(schedule.work_start), _hm(schedule.work_end)):
                return Period.WORKING
            return Period.HOME
        if day_type is DayType.OFFICE:
            leave = _hm(schedule.leave_time)
            back = _hm(schedule.return_time)
            if _in_window(minute, back - return_lead, back):
                return Period.RETURNING
            if _in_window(minute, leave, back - return_lead):
                return Period.AWAY
        return Period.HOME

    # ----------------------------------------------------------------- evaluate
    def evaluate(self, inp: Inputs) -> Decision:
        prefs = inp.prefs
        reasons: list[str] = []
        hot, cold, return_lead, wake_lead = self._leads(prefs, inp.weather)

        # Day type: before wake-up we are still on "yesterday's" night, but the
        # sleep window covers that, so today's weekday is fine.
        day_type = inp.day_override or inp.schedule.day_type(inp.now.weekday())
        period = self.scheduled_period(
            inp.now, inp.schedule, prefs, day_type, return_lead, wake_lead
        )

        if inp.mode is Mode.PAUSED:
            period = Period.PAUSED
            reasons.append("Paused by user")
        elif inp.mode is Mode.VACATION:
            period = Period.VACATION
            reasons.append("Vacation mode")
        elif inp.mode is Mode.AWAY:
            period = Period.AWAY
            reasons.append("Forced away by user")
        elif inp.mode is Mode.SLEEP:
            period = Period.SLEEP
            reasons.append("Forced sleep by user")
        elif inp.mode is Mode.HOME:
            if period in (Period.AWAY, Period.RETURNING):
                period = Period.HOME
            reasons.append("Forced home by user")
        else:
            if inp.occupied and period in (Period.AWAY, Period.RETURNING):
                period = Period.HOME
                reasons.append("Someone is home during a scheduled away period")
            elif (
                inp.occupied is False
                and prefs.presence_sets_away
                and period in (Period.HOME, Period.WORKING)
            ):
                period = Period.AWAY
                reasons.append("Nobody is home")

        cool, heat = self._house_targets(inp, period, hot, cold, reasons)

        zones: dict[str, ZoneDecision] = {}
        if period is not Period.PAUSED:
            zones = self._zone_targets(inp, period, cool, heat)
            self._balance(inp, period, zones, reasons)
        else:
            # Hand back anything we closed.
            for zone in inp.zones:
                if (sup := self.suppressed.pop(zone.zone_id, None)) is not None:
                    zones[zone.zone_id] = ZoneDecision(
                        zone.zone_id, zone.name, None, "none", [], None, None, None,
                        hvac_mode=sup.prev_mode if zone.hvac_mode == "off" else None,
                        reason="Released on pause",
                    )

        return Decision(
            period=period,
            day_type=day_type,
            cool_target=cool,
            heat_target=heat,
            hot_day=hot,
            cold_day=cold,
            return_lead=return_lead,
            reasons=reasons,
            zones=zones,
        )

    def _house_targets(
        self, inp: Inputs, period: Period, hot: bool, cold: bool, reasons: list[str]
    ) -> tuple[float, float]:
        p = inp.prefs
        if period is Period.SLEEP:
            cool, heat = p.sleep_cool, p.sleep_heat
        elif period is Period.AWAY:
            cool, heat = p.away_cool, p.away_heat
        elif period is Period.VACATION:
            cool, heat = p.away_cool + p.vacation_offset, p.away_heat - p.vacation_offset
        else:
            cool, heat = p.comfort_cool, p.comfort_heat

        minute = inp.now.hour * 60 + inp.now.minute
        if hot:
            reasons.append(f"Hot day ahead (forecast high {inp.weather.high:g})")
            if period is Period.AWAY and cool > p.comfort_cool + p.hot_away_cap:
                cool = p.comfort_cool + p.hot_away_cap
                reasons.append("Limiting away drift so the house doesn't heat-soak")
            if period in (Period.HOME, Period.WORKING) and minute < _hm(p.precool_until):
                cool -= p.precool_offset
                reasons.append(f"Pre-cooling until {p.precool_until}")
        if cold:
            reasons.append(f"Cold day ahead (forecast low {inp.weather.low:g})")
            if period is Period.AWAY and heat < p.comfort_heat - p.cold_away_cap:
                heat = p.comfort_heat - p.cold_away_cap
                reasons.append("Limiting away setback so recovery stays short")

        return cool, heat

    @staticmethod
    def _direction(mode: str) -> str | None:
        if mode == "cool":
            return "cool"
        if mode == "heat":
            return "heat"
        if mode in ("heat_cool", "auto"):
            return "dual"
        return None

    def _zone_targets(
        self, inp: Inputs, period: Period, cool: float, heat: float
    ) -> dict[str, ZoneDecision]:
        p = inp.prefs
        roles = ACTIVE_ROLES.get(period)
        out: dict[str, ZoneDecision] = {}
        for zone in inp.zones:
            rooms = [r for r in inp.rooms if r.zone_id == zone.zone_id]
            active = [r for r in rooms if roles is None or r.role in roles]
            readings = [r.temperature for r in active if r.temperature is not None]
            source = "rooms"
            names = [r.name for r in active]
            if not readings:
                readings = [r.temperature for r in rooms if r.temperature is not None]
                source = "all rooms"
                names = [r.name for r in rooms]
            if not readings and zone.thermostat_temp is not None:
                readings = [zone.thermostat_temp]
                source = "thermostat"
                names = []
            comfort = round(fmean(readings), 2) if readings else None
            if comfort is None:
                source = "none"

            sup = self.suppressed.get(zone.zone_id)
            mode = sup.prev_mode if sup and zone.hvac_mode == "off" else zone.hvac_mode
            direction = self._direction(mode)

            z_cool, z_heat = cool, heat
            idle = roles is not None and rooms and not active
            if idle:
                z_cool += p.idle_zone_offset
                z_heat -= p.idle_zone_offset

            # Bias the thermostat so the rooms that matter hit the target,
            # not wherever the thermostat happens to hang on the wall.
            bias = 0.0
            if (
                zone.compensate
                and source in ("rooms", "all rooms")
                and zone.thermostat_temp is not None
                and comfort is not None
            ):
                bias = max(
                    -p.compensation_limit,
                    min(p.compensation_limit, zone.thermostat_temp - comfort),
                )

            reason = f"{'Idle zone, relaxed' if idle else 'Comfort'} from {source}"
            dec = ZoneDecision(
                zone_id=zone.zone_id,
                name=zone.name,
                comfort_temp=comfort,
                comfort_source=source,
                active_rooms=names,
                direction=direction,
                target=None,
                demand=None,
                reason=reason,
            )
            if direction == "cool":
                dec.target = z_cool
                dec.temperature = self._fit(zone, z_cool + bias)
                if comfort is not None:
                    dec.demand = round(comfort - z_cool, 2)
            elif direction == "heat":
                dec.target = z_heat
                dec.temperature = self._fit(zone, z_heat + bias)
                if comfort is not None:
                    dec.demand = round(z_heat - comfort, 2)
            elif direction == "dual":
                # Dual-setpoint thermostats need a gap; the cool side wins.
                z_heat = min(z_heat, z_cool - p.min_gap)
                dec.target_low = self._fit(zone, z_heat + bias)
                dec.target_high = self._fit(zone, z_cool + bias)
            else:
                dec.reason = f"Thermostat is {zone.hvac_mode}; not controlling"
            out[zone.zone_id] = dec
        return out

    @staticmethod
    def _fit(zone: Zone, value: float) -> float:
        step = zone.step or 1.0
        value = round(value / step) * step
        if zone.min_temp is not None:
            value = max(zone.min_temp, value)
        if zone.max_temp is not None:
            value = min(zone.max_temp, value)
        return round(value, 2)

    # ---------------------------------------------------------------- balancing
    def _balance(
        self,
        inp: Inputs,
        period: Period,
        decisions: dict[str, ZoneDecision],
        reasons: list[str],
    ) -> None:
        p = inp.prefs
        now = inp.now
        zones_by_id = {z.zone_id: z for z in inp.zones}

        def release(zone_id: str, why: str) -> None:
            sup = self.suppressed.pop(zone_id)
            self.cooldown_until[zone_id] = now + timedelta(minutes=p.balance_cooldown_minutes)
            dec = decisions.get(zone_id)
            zone = zones_by_id.get(zone_id)
            if dec is not None and zone is not None and zone.hvac_mode == "off":
                dec.hvac_mode = sup.prev_mode
            if dec is not None:
                dec.reason = f"Released: {why}"

        # Drop suppressions for zones that disappeared.
        for zone_id in list(self.suppressed):
            if zone_id not in zones_by_id:
                self.suppressed.pop(zone_id)

        allowed = p.balance_enabled and period in OCCUPIED
        groups: dict[str, list[Zone]] = {}
        for zone in inp.zones:
            if zone.unit:
                groups.setdefault(zone.unit, []).append(zone)

        for unit, members in groups.items():
            decs = [decisions[z.zone_id] for z in members]
            directions = {d.direction for d in decs if d.direction}
            usable = (
                allowed
                and len(members) >= 2
                and len(directions) == 1
                and next(iter(directions)) in ("cool", "heat")
            )
            if not usable:
                for z in members:
                    if z.zone_id in self.suppressed:
                        release(z.zone_id, "balancing not applicable")
                continue

            open_decs = [
                d for d in decs if d.zone_id not in self.suppressed and d.demand is not None
            ]
            leader = max(open_decs, key=lambda d: d.demand, default=None)
            lead_demand = leader.demand if leader else None

            for dec in decs:
                if dec is leader or dec.demand is None:
                    continue
                sup = self.suppressed.get(dec.zone_id)
                if sup is not None:
                    elapsed = (now - sup.since).total_seconds() / 60
                    if dec.demand >= p.balance_safety:
                        release(dec.zone_id, "zone drifted too far")
                    elif elapsed >= p.balance_max_minutes:
                        release(dec.zone_id, "maximum closed time reached")
                    elif (lead_demand is None or lead_demand <= 0) and elapsed >= p.balance_min_minutes:
                        release(dec.zone_id, "other zones caught up")
                    else:
                        self._apply_suppression(dec, p, leader)
                    continue

                if now < self.cooldown_until.get(dec.zone_id, now):
                    continue
                if lead_demand is None or lead_demand < p.balance_trigger:
                    continue
                if lead_demand - dec.demand < p.balance_spread:
                    continue
                # Occupied zones are only closed once they're at/below target;
                # idle zones may drift up to the safety margin.
                limit = 0 if dec.active_rooms and dec.comfort_source == "rooms" else p.balance_safety - 1
                if dec.demand > limit:
                    continue
                zone = zones_by_id[dec.zone_id]
                self.suppressed[dec.zone_id] = _Suppression(since=now, prev_mode=zone.hvac_mode)
                self._apply_suppression(dec, p, leader)

            if any(d.suppressed for d in decs) and leader is not None:
                reasons.append(
                    f"Balancing {unit}: pushing air to {leader.name}"
                )

    @staticmethod
    def _apply_suppression(dec: ZoneDecision, p: Prefs, leader: ZoneDecision | None) -> None:
        dec.suppressed = True
        to = f" to favor {leader.name}" if leader else ""
        if p.suppress_method is SuppressMethod.OFF:
            dec.hvac_mode = "off"
            dec.temperature = None
            dec.reason = f"Closed{to} (zone off)"
        else:
            if dec.direction == "cool":
                dec.temperature = (dec.target or 0) + p.balance_setback
            else:
                dec.temperature = (dec.target or 0) - p.balance_setback
            dec.reason = f"Closed{to} (setback)"
