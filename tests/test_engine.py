"""Tests for the pure decision engine (no Home Assistant needed)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components" / "remote_comfort"))

from engine import (  # noqa: E402
    DayType,
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

# 2026-11-23 is a Monday.
MON = datetime(2026, 11, 23)
SCHEDULE = Schedule(remote_days={0, 4}, office_days={1, 2, 3})


def at(day_offset: int, hh: int, mm: int = 0) -> datetime:
    return MON + timedelta(days=day_offset, hours=hh, minutes=mm)


def two_zone_house(up: float, down: float, mode: str = "cool") -> tuple[list[Zone], list[Room]]:
    zones = [
        Zone("up", "Upstairs", "Main unit", mode, thermostat_temp=up, compensate=False),
        Zone("down", "Downstairs", "Main unit", mode, thermostat_temp=down, compensate=False),
    ]
    rooms = [
        Room("office", "Office", "up", Role.OFFICE, up),
        Room("bed", "Primary bedroom", "up", Role.BEDROOM, up),
        Room("living", "Living room", "down", Role.LIVING, down),
    ]
    return zones, rooms


def run(engine: Engine, now: datetime, zones, rooms, **kw) -> object:
    return engine.evaluate(
        Inputs(now=now, schedule=kw.pop("schedule", SCHEDULE), prefs=kw.pop("prefs", Prefs()),
               zones=zones, rooms=rooms, **kw)
    )


# ------------------------------------------------------------------ periods
def test_remote_day_periods():
    zones, rooms = two_zone_house(74, 74)
    e = Engine()
    assert run(e, at(0, 3), zones, rooms).period is Period.SLEEP
    assert run(e, at(0, 7), zones, rooms).period is Period.HOME
    assert run(e, at(0, 10), zones, rooms).period is Period.WORKING
    assert run(e, at(0, 18), zones, rooms).period is Period.HOME
    # sleep lead: 30 minutes before 22:30
    assert run(e, at(0, 22, 5), zones, rooms).period is Period.SLEEP
    # wake lead: 30 minutes before 06:30
    assert run(e, at(0, 6, 5), zones, rooms).period is Period.HOME


def test_office_day_away_and_return_lead():
    zones, rooms = two_zone_house(74, 74)
    e = Engine()
    d = run(e, at(1, 12), zones, rooms)
    assert d.day_type is DayType.OFFICE
    assert d.period is Period.AWAY
    assert d.cool_target == Prefs().away_cool
    # return at 17:30 with 45 min base lead
    assert run(e, at(1, 16, 50), zones, rooms).period is Period.RETURNING
    assert run(e, at(1, 16, 40), zones, rooms).period is Period.AWAY


def test_hot_day_starts_recovery_earlier_and_caps_drift():
    zones, rooms = two_zone_house(74, 74)
    e = Engine()
    hot = Weather(high=100)
    d = run(e, at(1, 15, 40), zones, rooms, weather=hot)
    # 10 degrees over threshold * 6 min = +60 min -> 105 min lead -> starts 15:45
    assert d.return_lead == 105
    assert d.period is Period.AWAY
    assert d.cool_target == Prefs().comfort_cool + Prefs().hot_away_cap
    assert run(e, at(1, 15, 50), zones, rooms, weather=hot).period is Period.RETURNING


def test_precool_on_hot_remote_morning():
    zones, rooms = two_zone_house(74, 74)
    p = Prefs()
    d = run(Engine(), at(0, 10), zones, rooms, weather=Weather(high=95))
    assert d.cool_target == p.comfort_cool - p.precool_offset
    d = run(Engine(), at(0, 15), zones, rooms, weather=Weather(high=95))
    assert d.cool_target == p.comfort_cool


def test_sleep_cold_targets():
    zones, rooms = two_zone_house(74, 74)
    p = Prefs(sleep_cool=67)
    d = run(Engine(), at(0, 2), zones, rooms, prefs=p)
    assert d.cool_target == 67


def test_presence_overrides_away():
    zones, rooms = two_zone_house(74, 74)
    d = run(Engine(), at(1, 12), zones, rooms, occupied=True)
    assert d.period is Period.HOME


def test_presence_sets_away_only_when_enabled():
    zones, rooms = two_zone_house(74, 74)
    assert run(Engine(), at(0, 10), zones, rooms, occupied=False).period is Period.WORKING
    p = Prefs(presence_sets_away=True)
    assert run(Engine(), at(0, 10), zones, rooms, occupied=False, prefs=p).period is Period.AWAY


def test_day_override_and_modes():
    zones, rooms = two_zone_house(74, 74)
    # Tuesday forced to remote
    assert run(Engine(), at(1, 12), zones, rooms, day_override=DayType.REMOTE).period is Period.WORKING
    assert run(Engine(), at(0, 12), zones, rooms, mode=Mode.VACATION).period is Period.VACATION
    d = run(Engine(), at(0, 12), zones, rooms, mode=Mode.PAUSED)
    assert d.period is Period.PAUSED and d.zones == {}


def test_min_gap_enforced_for_dual_setpoint_thermostats():
    zones = [Zone("z", "Main", None, "heat_cool", 70, compensate=False)]
    p = Prefs(comfort_cool=70, comfort_heat=70)
    d = run(Engine(), at(0, 12), zones, [], prefs=p)
    assert d.zones["z"].target_high == 70
    assert d.zones["z"].target_low == 70 - p.min_gap


# --------------------------------------------------------------- zone logic
def test_working_period_uses_office_and_relaxes_bedroom_only_zones():
    zones = [
        Zone("up", "Upstairs", None, "cool", 75, compensate=False),
        Zone("bedwing", "Bedrooms", None, "cool", 75, compensate=False),
    ]
    rooms = [
        Room("office", "Office", "up", Role.OFFICE, 77),
        Room("bed", "Bedroom", "bedwing", Role.BEDROOM, 76),
    ]
    d = run(Engine(), at(0, 12), zones, rooms)
    assert d.zones["up"].active_rooms == ["Office"]
    assert d.zones["up"].temperature == Prefs().comfort_cool
    assert d.zones["bedwing"].temperature == Prefs().comfort_cool + Prefs().idle_zone_offset


def test_compensation_biases_setpoint_toward_room_readings():
    zones = [Zone("up", "Upstairs", None, "cool", thermostat_temp=74)]
    rooms = [Room("office", "Office", "up", Role.OFFICE, 77)]
    d = run(Engine(), at(0, 15), zones, rooms)
    # target 74, thermostat reads 3 lower than the office -> set 71
    assert d.zones["up"].temperature == 71


def test_heat_cool_mode_gets_both_setpoints():
    zones = [Zone("z", "Main", None, "heat_cool", 70, compensate=False)]
    d = run(Engine(), at(0, 15), zones, [])
    assert d.zones["z"].target_low == Prefs().comfort_heat
    assert d.zones["z"].target_high == Prefs().comfort_cool


def test_off_thermostat_is_left_alone():
    zones = [Zone("z", "Main", None, "off", 70)]
    d = run(Engine(), at(0, 15), zones, [])
    assert d.zones["z"].temperature is None and d.zones["z"].hvac_mode is None


# ---------------------------------------------------------------- balancing
def test_balancing_closes_cool_downstairs_when_upstairs_is_hot():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    d = run(e, at(0, 16), zones, rooms)
    assert d.zones["down"].suppressed
    assert d.zones["down"].temperature == Prefs().comfort_cool + Prefs().balance_setback
    assert not d.zones["up"].suppressed
    assert any("pushing air to Upstairs" in r for r in d.reasons)


def test_balancing_not_triggered_for_small_spread():
    zones, rooms = two_zone_house(up=75.5, down=74)
    d = run(Engine(), at(0, 16), zones, rooms)
    assert not d.zones["down"].suppressed


def test_balancing_releases_on_safety_and_cools_down():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms)
    zones, rooms = two_zone_house(up=78, down=77.5)  # downstairs drifted 3.5 over
    d = run(e, at(0, 16, 5), zones, rooms)
    assert not d.zones["down"].suppressed
    assert "drifted" in d.zones["down"].reason
    # cooldown prevents immediately re-closing
    zones, rooms = two_zone_house(up=79, down=73.5)
    assert not run(e, at(0, 16, 10), zones, rooms).zones["down"].suppressed
    assert run(e, at(0, 16, 25), zones, rooms).zones["down"].suppressed


def test_balancing_min_and_max_duration():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms)
    # upstairs satisfied after 5 minutes, but min time is 10
    zones, rooms = two_zone_house(up=73.5, down=74)
    assert run(e, at(0, 16, 5), zones, rooms).zones["down"].suppressed
    assert not run(e, at(0, 16, 11), zones, rooms).zones["down"].suppressed
    # max duration
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms)
    assert run(e, at(0, 16, 44), zones, rooms).zones["down"].suppressed
    assert not run(e, at(0, 16, 46), zones, rooms).zones["down"].suppressed


def test_balancing_off_method_restores_previous_mode():
    e = Engine()
    p = Prefs(suppress_method=SuppressMethod.OFF)
    zones, rooms = two_zone_house(up=79, down=73.5)
    d = run(e, at(0, 16), zones, rooms, prefs=p)
    assert d.zones["down"].hvac_mode == "off"
    # Thermostat now reports off; upstairs caught up after min time
    zones, rooms = two_zone_house(up=73, down=74)
    zones[1].hvac_mode = "off"
    d = run(e, at(0, 16, 15), zones, rooms, prefs=p)
    assert not d.zones["down"].suppressed
    assert d.zones["down"].hvac_mode == "cool"


def test_balancing_in_heat_favors_cold_downstairs():
    e = Engine()
    zones, rooms = two_zone_house(up=72, down=65, mode="heat")
    d = run(e, at(0, 16), zones, rooms)
    assert d.zones["up"].suppressed
    assert d.zones["up"].temperature == Prefs().comfort_heat - Prefs().balance_setback


def test_balancing_disabled_when_away_and_releases():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms)
    d = run(e, at(0, 16, 2), zones, rooms, mode=Mode.AWAY)
    assert not d.zones["down"].suppressed


def test_zones_on_different_units_never_balance():
    zones, rooms = two_zone_house(up=79, down=73.5)
    zones[1].unit = "Basement unit"
    d = run(Engine(), at(0, 16), zones, rooms)
    assert not d.zones["down"].suppressed


# ---------------------------------------------------------------- hardening
def test_closed_zone_is_released_when_its_temperature_data_disappears():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    assert run(e, at(0, 16), zones, rooms).zones["down"].suppressed
    zones, rooms = two_zone_house(up=79, down=73.5)
    zones[1].thermostat_temp = None
    rooms[2].temperature = None
    d = run(e, at(0, 16, 2), zones, rooms)
    assert not d.zones["down"].suppressed
    assert "lost temperature data" in d.zones["down"].reason


def test_unavailable_zone_keeps_its_suppression_and_is_restored_later():
    e = Engine()
    p = Prefs(suppress_method=SuppressMethod.OFF)
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms, prefs=p)
    # Downstairs thermostat drops off the network for a while
    d = run(e, at(0, 16, 2), zones[:1], rooms, prefs=p, configured_zone_ids={"up", "down"})
    assert "down" in e.suppressed
    # It comes back reporting "off" after the max closed time: switched back on
    zones, rooms = two_zone_house(up=79, down=73.5)
    zones[1].hvac_mode = "off"
    d = run(e, at(0, 17), zones, rooms, prefs=p, configured_zone_ids={"up", "down"})
    assert d.zones["down"].hvac_mode == "cool"


def test_removed_zone_suppression_is_forgotten():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms)
    run(e, at(0, 16, 2), zones[:1], rooms, configured_zone_ids={"up"})
    assert "down" not in e.suppressed


def test_safety_limits_clamp_everything_sent():
    zones = [Zone("z", "Main", None, "heat", 60, compensate=False)]
    p = Prefs(away_heat=30, safety_min=50)
    d = run(Engine(), at(1, 12), zones, [], prefs=p)
    assert d.zones["z"].temperature == 50


def test_setback_respects_thermostat_max():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    zones[1].max_temp = 76
    d = run(e, at(0, 16), zones, rooms)
    assert d.zones["down"].suppressed and d.zones["down"].temperature == 76


def test_state_export_import_roundtrip():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    run(e, at(0, 16), zones, rooms)
    e2 = Engine()
    e2.import_state(e.export_state())
    assert e2.suppressed["down"].prev_mode == "cool"
    e2.import_state({"suppressed": {"bad": {"since": "nope"},
                                    "off": {"since": at(0, 1).isoformat(), "prev_mode": "off"}},
                     "guarded": {"g": {"direction": "sideways", "since": at(0, 1).isoformat()}}})
    assert set(e2.suppressed) == {"down"} and not e2.guarded
    e2.import_state("garbage")  # corrupt storage must not raise


# -------------------------------------------------------------------- guard
def test_guard_cools_hot_zone_even_when_away_and_paused():
    zones, rooms = two_zone_house(up=86, down=74)
    for mode in (Mode.AWAY, Mode.VACATION, Mode.PAUSED):
        e = Engine()
        d = run(e, at(1, 12), zones, rooms, mode=mode)
        up = d.zones["up"]
        assert up.guard and up.temperature == Prefs().comfort_cool
        assert d.guarded == ["Upstairs"]


def test_guard_turns_heat_on_when_system_off_and_restores_after():
    e = Engine()
    zones = [Zone("z", "Main", None, "off", 54, compensate=False, hvac_modes=["off", "heat", "cool"])]
    d = run(e, at(0, 3), zones, [])
    assert d.zones["z"].hvac_mode == "heat"
    assert d.zones["z"].temperature == Prefs().comfort_heat
    # Still recovering: stays on
    zones[0].hvac_mode = "heat"
    zones[0].thermostat_temp = 56
    assert run(e, at(0, 3, 30), zones, []).zones["z"].guard
    # Recovered past limit + margin: back to off, no setpoint sent in the old direction
    zones[0].thermostat_temp = 58
    d = run(e, at(0, 4), zones, [])
    assert d.zones["z"].hvac_mode == "off" and d.zones["z"].temperature is None
    assert "z" not in e.guarded


def test_guard_respects_switch_mode_setting_and_supported_modes():
    zones = [Zone("z", "Main", None, "off", 54, hvac_modes=["off", "cool"])]
    d = run(Engine(), at(0, 3), zones, [])
    assert d.zones["z"].hvac_mode is None and "can't be corrected" in " ".join(d.reasons)
    zones = [Zone("z", "Main", None, "off", 54)]
    d = run(Engine(), at(0, 3), zones, [], prefs=Prefs(guard_switch_mode=False))
    assert d.zones["z"].hvac_mode is None


def test_guard_overrides_balancing_and_uses_any_room():
    e = Engine()
    zones, rooms = two_zone_house(up=79, down=73.5)
    assert run(e, at(0, 16), zones, rooms).zones["down"].suppressed
    # A downstairs room (not one that "counts" right now) bakes past the limit.
    rooms.append(Room("sun", "Sunroom", "down", Role.OTHER, 88))
    d = run(e, at(0, 16, 2), zones, rooms)
    assert d.zones["down"].guard and not d.zones["down"].suppressed
    assert "down" not in e.suppressed


def test_guard_targets_never_outside_guard_limits():
    zones = [Zone("z", "Main", None, "cool", 86, compensate=False)]
    d = run(Engine(), at(0, 12), zones, [], prefs=Prefs(comfort_cool=88))
    assert d.zones["z"].temperature == 83  # guard_max 85 - margin 2
