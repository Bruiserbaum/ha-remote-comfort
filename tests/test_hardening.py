"""Failure-mode tests: missing, broken, stale or misbehaving inputs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.remote_comfort.const import DOMAIN, default_options

pytest_plugins = ["pytest_homeassistant_custom_component"]

MONDAY_4PM = "2026-11-23 16:00:00-08:00"  # remote day, working hours


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def climate(hass, entity_id, temp, setpoint, mode="cool"):
    hass.states.async_set(entity_id, mode, {
        "current_temperature": temp, "temperature": setpoint,
        "min_temp": 45, "max_temp": 95, "target_temp_step": 1,
    })


def sensor(hass, entity_id, value, unit="°F"):
    hass.states.async_set(entity_id, str(value), {"device_class": "temperature", "unit_of_measurement": unit})


def zone(sid, title, entity_id, unit="Main unit", compensate=False):
    return {"subentry_id": sid, "subentry_type": "zone", "title": title, "unique_id": None,
            "data": {"name": title, "climate_entity": entity_id, "unit": unit, "compensate": compensate}}


def room(sid, title, zone_id, sensors, role="living"):
    return {"subentry_id": sid, "subentry_type": "room", "title": title, "unique_id": None,
            "data": {"name": title, "zone": zone_id, "sensors": sensors, "role": role}}


async def setup(hass, subentries, **options):
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home", entry_id="house",
        options={**default_options("°F"), **options},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def problems(hass):
    return hass.states.get("binary_sensor.remote_comfort_home_problem")


HOUSE = [
    zone("up", "Upstairs", "climate.up"),
    zone("down", "Downstairs", "climate.down"),
    room("office", "Office", "up", ["sensor.office_a", "sensor.office_b", "sensor.office_c"], "office"),
    room("living", "Living", "down", ["sensor.living"]),
]


def house_states(hass, up=79, down=73.5):
    climate(hass, "climate.up", up, 74)
    climate(hass, "climate.down", down, 74)
    for s in ("sensor.office_a", "sensor.office_b", "sensor.office_c"):
        sensor(hass, s, up)
    sensor(hass, "sensor.living", down)


async def test_bad_sensors_are_ignored_and_reported(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    house_states(hass, up=76, down=74)
    sensor(hass, "sensor.office_a", "unavailable")
    sensor(hass, "sensor.office_b", 150)  # implausible
    sensor(hass, "sensor.office_c", 76)
    entry = await setup(hass, HOUSE)
    coordinator = entry.runtime_data

    # Bad readings dropped; the office uses the one good sensor.
    assert hass.states.get("sensor.office_temperature").state == "76.0"
    attrs = hass.states.get("sensor.office_temperature").attributes["sensors"]
    assert attrs["sensor.office_a"] == "unavailable"
    assert attrs["sensor.office_b"].startswith("implausible")

    # Problems are suppressed during the startup grace period, then reported.
    assert problems(hass).state == "off"
    freezer.tick(timedelta(minutes=6))
    sensor(hass, "sensor.office_c", 76)
    sensor(hass, "sensor.living", 74)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert problems(hass).state == "on"
    assert any("office_a" in p for p in problems(hass).attributes["problems"])

    # After 30 minutes a Repair is raised; it clears itself on recovery.
    freezer.tick(timedelta(minutes=31))
    sensor(hass, "sensor.office_c", 76)
    sensor(hass, "sensor.living", 74)
    await coordinator.async_refresh()
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, "house_sensor.office_a") is not None
    sensor(hass, "sensor.office_a", 76)
    sensor(hass, "sensor.office_b", 76)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, "house_sensor.office_a") is None
    assert problems(hass).state == "off"


async def test_outlier_stale_and_celsius_sensors(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    house_states(hass, up=76, down=74)
    sensor(hass, "sensor.office_a", 24.5, "°C")  # 76.1 °F
    sensor(hass, "sensor.office_c", 90)  # outlier vs. the other two
    entry = await setup(hass, HOUSE)
    room = entry.runtime_data.rooms["office"]
    assert room.temperature == pytest.approx(76.05, abs=0.1)
    assert entry.runtime_data.sensor_status["sensor.office_c"].startswith("outlier")

    # Living-room sensor stops reporting (battery dead, value frozen).
    freezer.tick(timedelta(hours=5))
    for s in ("sensor.office_a", "sensor.office_b", "sensor.office_c"):
        sensor(hass, s, 76)
    climate(hass, "climate.down", 74, 74)
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.sensor_status["sensor.living"] == "stale"
    # Zone falls back to its thermostat reading instead.
    assert entry.runtime_data.data.zones["down"].comfort_source == "thermostat"


async def test_unavailable_thermostat_with_off_method_is_switched_back_on(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    house_states(hass)
    entry = await setup(hass, HOUSE, suppress_method="off")
    coordinator = entry.runtime_data
    assert set_mode[-1].data == {"entity_id": "climate.down", "hvac_mode": "off"}
    climate(hass, "climate.down", 73.5, 74, mode="off")

    # Thermostat goes offline: nothing sent, but the suppression is remembered.
    hass.states.async_set("climate.down", "unavailable")
    freezer.tick(timedelta(minutes=2))
    await coordinator.async_refresh()
    assert "down" in coordinator.engine.suppressed
    assert coordinator.unavailable_zones == ["Downstairs"]

    # It returns, still off, after the maximum closed time: turned back on.
    freezer.tick(timedelta(minutes=50))
    house_states(hass)
    climate(hass, "climate.down", 73.5, 74, mode="off")
    await coordinator.async_refresh()
    assert set_mode[-1].data == {"entity_id": "climate.down", "hvac_mode": "cool"}


async def test_closed_zone_survives_restart(hass: HomeAssistant, freezer, hass_storage) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    # HA restarted while Remote Comfort had downstairs switched off an hour ago.
    hass_storage["remote_comfort.house"] = {
        "version": 1, "minor_version": 1, "key": "remote_comfort.house",
        "data": {"suppressed": {"down": {"since": "2026-11-23T15:00:00-08:00", "prev_mode": "cool"}}},
    }
    house_states(hass, up=74, down=74)
    climate(hass, "climate.down", 74, 74, mode="off")
    await setup(hass, HOUSE, suppress_method="off")
    assert set_mode[-1].data == {"entity_id": "climate.down", "hvac_mode": "cool"}


async def test_one_failing_thermostat_does_not_block_others(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    sent: list[str] = []

    async def set_temperature(call: ServiceCall) -> None:
        if call.data["entity_id"] == "climate.up":
            raise HomeAssistantError("cloud API timeout")
        sent.append(call.data["entity_id"])

    hass.services.async_register("climate", "set_temperature", set_temperature)
    house_states(hass, up=76, down=76)
    climate(hass, "climate.up", 76, 70)
    climate(hass, "climate.down", 76, 70)
    entry = await setup(hass, HOUSE)
    assert sent == ["climate.down"]
    assert any("cloud API timeout" in p.message for p in entry.runtime_data.problems.values())
    assert problems(hass).state == "on"  # command failures are reported immediately


async def test_cloud_lag_does_not_trigger_manual_hold(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-11-24 12:00:00-08:00")  # office day: away, cool to 80
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    climate(hass, "climate.main", 74, 74)
    entry = await setup(hass, [zone("main", "Main", "climate.main")])
    assert set_temp[-1].data["temperature"] == 80
    # Thermostat never reflects the change (lagging cloud / lost command).
    freezer.tick(timedelta(minutes=6))
    await entry.runtime_data.async_refresh()
    assert "main" not in entry.runtime_data.holds
    assert len(set_temp) == 2  # re-sent rather than held


async def test_person_turning_closed_zone_back_on_wins(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    house_states(hass)
    entry = await setup(hass, HOUSE, suppress_method="off")
    coordinator = entry.runtime_data
    assert "down" in coordinator.engine.suppressed
    # Someone turns downstairs back on at the thermostat, 10 minutes later.
    freezer.tick(timedelta(minutes=10))
    house_states(hass)
    await coordinator.async_refresh()
    assert "down" not in coordinator.engine.suppressed
    assert "down" in coordinator.holds
    assert set_mode[-1].data["hvac_mode"] == "off"  # no new "off" sent
    assert len([c for c in set_mode if c.data["hvac_mode"] == "off"]) == 1


async def test_bad_options_fall_back_to_defaults(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    climate(hass, "climate.main", 74, 74)
    entry = await setup(
        hass, [zone("main", "Main", "climate.main"), room("r", "Orphan", "deleted-zone", ["sensor.x"])],
        wake_time="banana", comfort_cool="hot", remote_days=["mon", "funday"],
    )
    assert hass.states.get("sensor.remote_comfort_home_status").state == "working"
    keys = set(entry.runtime_data.problems)
    assert {"option_wake_time", "option_comfort_cool", "room_r"} <= keys


async def test_missing_weather_entity_is_tolerated(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    climate(hass, "climate.main", 74, 74)
    entry = await setup(hass, [zone("main", "Main", "climate.main")], weather_entity="weather.gone")
    assert entry.runtime_data.data.hot_day is False
    freezer.tick(timedelta(minutes=6))
    await entry.runtime_data.async_refresh()
    assert "weather_forecast" in entry.runtime_data.problems


async def test_failed_switch_back_on_is_retried(hass: HomeAssistant, freezer, hass_storage) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    attempts: list[str] = []

    async def set_hvac_mode(call: ServiceCall) -> None:
        attempts.append(call.data["hvac_mode"])
        if len(attempts) == 1:
            raise HomeAssistantError("thermostat busy")

    hass.services.async_register("climate", "set_hvac_mode", set_hvac_mode)
    hass_storage["remote_comfort.house"] = {
        "version": 1, "minor_version": 1, "key": "remote_comfort.house",
        "data": {"suppressed": {"down": {"since": "2026-11-23T15:00:00-08:00", "prev_mode": "cool"}}},
    }
    house_states(hass, up=74, down=74)
    climate(hass, "climate.down", 74, 74, mode="off")
    entry = await setup(hass, HOUSE, suppress_method="off")
    assert attempts == ["cool"]
    assert "down" in entry.runtime_data.engine.suppressed
    freezer.tick(timedelta(minutes=2))
    await entry.runtime_data.async_refresh()
    assert attempts == ["cool", "cool"]
