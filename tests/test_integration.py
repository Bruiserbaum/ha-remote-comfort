"""End-to-end tests against a real Home Assistant instance (pytest-homeassistant-custom-component)."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.remote_comfort.const import DOMAIN

pytest_plugins = ["pytest_homeassistant_custom_component"]

# Monday, 4pm, remote work day in default schedule (mon, fri) -> "working"
MONDAY_4PM = "2026-11-23 16:00:00-08:00"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _climate(hass: HomeAssistant, entity_id: str, temp: float, setpoint: float, mode: str = "cool"):
    hass.states.async_set(
        entity_id,
        mode,
        {
            "current_temperature": temp,
            "temperature": setpoint,
            "min_temp": 45,
            "max_temp": 95,
            "target_temp_step": 1,
            "hvac_modes": ["off", "cool", "heat", "heat_cool"],
        },
    )


def _temp_sensor(hass: HomeAssistant, entity_id: str, value: float):
    hass.states.async_set(entity_id, str(value), {"device_class": "temperature", "unit_of_measurement": "°F"})


async def _setup(hass: HomeAssistant, weather: bool = False):
    hass.config.units = US_CUSTOMARY_SYSTEM
    user = {"name": "Home", "presence_sets_away": False, "respect_manual_changes": True,
            "wake_lead": 30, "sleep_lead": 30, "return_lead": 45,
            "idle_zone_offset": 2, "compensation_limit": 3}
    if weather:
        user["weather_entity"] = "weather.home"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["step_id"] == "schedule"

    # overlapping days are rejected
    sched = {
        "remote_days": ["mon", "fri"], "office_days": ["mon"],
        "wake_time": "06:30:00", "off_day_wake_time": "08:00:00", "sleep_time": "22:30:00",
        "work_start": "08:00:00", "work_end": "17:00:00",
        "leave_time": "07:30:00", "return_time": "17:30:00",
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sched)
    assert result["errors"] == {"base": "day_overlap"}
    sched["office_days"] = ["tue", "wed", "thu"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], sched)
    assert result["step_id"] == "comfort"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"comfort_cool": 74, "comfort_heat": 69, "sleep_cool": 68, "sleep_heat": 65,
         "away_cool": 80, "away_heat": 62},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return hass.config_entries.async_entries(DOMAIN)[0]


async def _add(hass: HomeAssistant, entry, kind: str, data: dict):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": SOURCE_USER}
    )
    if result["type"] is FlowResultType.ABORT:
        return result
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], data)
    await hass.async_block_till_done()
    return result


def _zone_id(entry, title: str) -> str:
    return next(s.subentry_id for s in entry.subentries.values() if s.title == title)


async def test_full_house_balances_single_unit(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    set_mode = async_mock_service(hass, "climate", "set_hvac_mode")
    _climate(hass, "climate.upstairs", 77, 74)
    _climate(hass, "climate.downstairs", 73, 74)
    _temp_sensor(hass, "sensor.office", 79)
    _temp_sensor(hass, "sensor.bedroom", 78)
    _temp_sensor(hass, "sensor.living", 73.5)

    entry = await _setup(hass)

    # Rooms need a zone first
    res = await _add(hass, entry, "room", {})
    assert res["type"] is FlowResultType.ABORT and res["reason"] == "no_zones"

    await _add(hass, entry, "zone", {"name": "Upstairs", "climate_entity": "climate.upstairs",
                                     "unit": "Main unit", "compensate": True})
    res = await _add(hass, entry, "zone", {"name": "Dup", "climate_entity": "climate.upstairs",
                                           "unit": "Main unit", "compensate": True})
    assert res["errors"] == {"climate_entity": "climate_in_use"}
    await _add(hass, entry, "zone", {"name": "Downstairs", "climate_entity": "climate.downstairs",
                                     "unit": "Main unit", "compensate": False})
    up, down = _zone_id(entry, "Upstairs"), _zone_id(entry, "Downstairs")
    await _add(hass, entry, "room", {"name": "Office", "zone": up, "sensors": ["sensor.office"], "role": "office"})
    await _add(hass, entry, "room", {"name": "Bedroom", "zone": up, "sensors": ["sensor.bedroom"], "role": "bedroom"})
    await _add(hass, entry, "room", {"name": "Living room", "zone": down, "sensors": ["sensor.living"], "role": "living"})
    await hass.async_block_till_done()

    status = hass.states.get("sensor.remote_comfort_home_status")
    assert status.state == "working"
    assert hass.states.get("sensor.remote_comfort_home_cool_target").state == "74.0"
    assert hass.states.get("sensor.upstairs_zone_comfort_temperature").state == "79.0"
    assert hass.states.get("sensor.office_temperature").state == "79.0"
    assert hass.states.get("binary_sensor.downstairs_zone_balancing").state == "on"
    assert any("pushing air to Upstairs" in r for r in status.attributes["reasons"])

    calls = {c.data["entity_id"]: c.data["temperature"] for c in set_temp}
    # Upstairs: target 74, thermostat reads 2 below the office -> 72
    assert calls["climate.upstairs"] == 72
    # Downstairs closed with a +5 setback
    assert calls["climate.downstairs"] == 79
    assert not set_mode


async def test_controls_and_manual_hold(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    set_temp = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_hvac_mode")
    _climate(hass, "climate.main", 74, 74)
    entry = await _setup(hass)
    await _add(hass, entry, "zone", {"name": "Main", "climate_entity": "climate.main",
                                     "unit": "Main unit", "compensate": True})
    coordinator = entry.runtime_data

    # Already at target: nothing sent
    assert not set_temp

    # Mode select -> away targets
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.remote_comfort_home_mode", "option": "away"}, blocking=True)
    await coordinator.async_refresh()
    assert set_temp[-1].data["temperature"] == 80
    _climate(hass, "climate.main", 74, 80)

    # Someone changes the thermostat by hand -> held until the period changes
    freezer.tick(300)
    _climate(hass, "climate.main", 74, 76)
    n = len(set_temp)
    await coordinator.async_refresh()
    assert len(set_temp) == n
    assert hass.states.get("sensor.main_zone_comfort_temperature").attributes["manual_hold"]

    # Period change clears the hold
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.remote_comfort_home_mode", "option": "auto"}, blocking=True)
    await coordinator.async_refresh()
    assert set_temp[-1].data["temperature"] == 74

    # Number entity updates the live target without a reload
    await hass.services.async_call("number", "set_value",
                                   {"entity_id": "number.remote_comfort_home_home_cool_target", "value": 72},
                                   blocking=True)
    await hass.async_block_till_done()
    assert entry.runtime_data is coordinator
    await coordinator.async_refresh()
    assert set_temp[-1].data["temperature"] == 72

    # Switch off -> no more commands
    await hass.services.async_call("switch", "turn_off",
                                   {"entity_id": "switch.remote_comfort_home_thermostat_control"}, blocking=True)
    n = len(set_temp)
    _climate(hass, "climate.main", 74, 70)
    await coordinator.async_refresh()
    assert len(set_temp) == n
    assert hass.states.get("sensor.remote_comfort_home_status").state == "paused"


async def test_weather_forecast_precools(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-07-20 10:00:00-07:00")  # Monday morning, remote day
    set_temp = async_mock_service(hass, "climate", "set_temperature")

    async def forecasts(call):
        return {"weather.home": {"forecast": [
            {"datetime": "2026-07-20T13:00:00-07:00", "temperature": 97},
            {"datetime": "2026-07-20T16:00:00-07:00", "temperature": 101},
        ]}}

    hass.services.async_register("weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY)
    hass.states.async_set("weather.home", "sunny", {"temperature": 88})
    _climate(hass, "climate.main", 74, 74)
    entry = await _setup(hass, weather=True)
    await _add(hass, entry, "zone", {"name": "Main", "climate_entity": "climate.main",
                                     "unit": "Main unit", "compensate": True})
    status = hass.states.get("sensor.remote_comfort_home_status")
    assert status.attributes["hot_day"] is True
    assert status.attributes["forecast_high"] == 101
    assert set_temp[-1].data["temperature"] == 72  # 74 - 2 pre-cool


async def test_options_flow_and_unload(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(MONDAY_4PM)
    async_mock_service(hass, "climate", "set_temperature")
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "balancing"})
    assert result["step_id"] == "balancing"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"balance_enabled": True, "suppress_method": "off", "balance_trigger": 1, "balance_spread": 2,
         "balance_safety": 3, "balance_setback": 5, "balance_min_minutes": 10,
         "balance_max_minutes": 45, "balance_cooldown_minutes": 15},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["suppress_method"] == "off"
    assert entry.options["comfort_cool"] == 74
    assert await hass.config_entries.async_unload(entry.entry_id)
