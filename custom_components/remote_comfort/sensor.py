"""Sensors: house status, targets, zone comfort and room temperatures."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemoteComfortConfigEntry
from .const import CONF_ROLE, CONF_SENSORS, CONF_UNIT, SUBENTRY_ROOM, SUBENTRY_ZONE
from .coordinator import RemoteComfortCoordinator
from .engine import Period
from .entity import RemoteComfortEntity, ZoneEntity, zone_of


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteComfortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            StatusSensor(coordinator),
            TargetSensor(coordinator, "cool_target"),
            TargetSensor(coordinator, "heat_target"),
        ]
    )
    for sub in entry.subentries.values():
        if sub.subentry_type == SUBENTRY_ZONE:
            async_add_entities([ZoneComfortSensor(coordinator, sub)], config_subentry_id=sub.subentry_id)
        elif sub.subentry_type == SUBENTRY_ROOM:
            async_add_entities([RoomSensor(coordinator, sub)], config_subentry_id=sub.subentry_id)


class StatusSensor(RemoteComfortEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [p.value for p in Period]

    def __init__(self, coordinator: RemoteComfortCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        return self.coordinator.data.period.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        w = self.coordinator.weather
        return {
            "day_type": d.day_type.value,
            "reasons": d.reasons,
            "hot_day": d.hot_day,
            "cold_day": d.cold_day,
            "forecast_high": w.high,
            "forecast_low": w.low,
            "outdoor_temperature": w.current,
            "return_lead_minutes": d.return_lead,
            "occupied": self.coordinator.occupied,
            "held_zones": [
                self.coordinator.config_entry.subentries[z].title
                for z in self.coordinator.holds
                if z in self.coordinator.config_entry.subentries
            ],
        }


class _Temperature(SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1


class TargetSensor(RemoteComfortEntity, _Temperature):
    def __init__(self, coordinator: RemoteComfortCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_native_unit_of_measurement = coordinator.unit

    @property
    def native_value(self) -> float:
        return getattr(self.coordinator.data, self._key)


class ZoneComfortSensor(ZoneEntity, _Temperature):
    _attr_translation_key = "zone_comfort"

    def __init__(self, coordinator: RemoteComfortCoordinator, sub: ConfigSubentry) -> None:
        super().__init__(coordinator, sub, "comfort")
        self._attr_native_unit_of_measurement = coordinator.unit

    @property
    def _dec(self):
        return self.coordinator.data.zones.get(self.subentry.subentry_id)

    @property
    def available(self) -> bool:
        return super().available and self._dec is not None

    @property
    def native_value(self) -> float | None:
        return self._dec.comfort_temp if self._dec else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dec = self._dec
        if dec is None:
            return {}
        return {
            "hvac_unit": self.subentry.data.get(CONF_UNIT),
            "floor": self.floor_name(self.subentry),
            "source": dec.comfort_source,
            "active_rooms": dec.active_rooms,
            "direction": dec.direction,
            "target": dec.target,
            "demand": dec.demand,
            "setpoint": dec.temperature,
            "setpoint_low": dec.target_low,
            "setpoint_high": dec.target_high,
            "balancing": dec.suppressed,
            "manual_hold": self.subentry.subentry_id in self.coordinator.holds,
            "reason": dec.reason,
        }


class RoomSensor(ZoneEntity, _Temperature):
    def __init__(self, coordinator: RemoteComfortCoordinator, sub: ConfigSubentry) -> None:
        self._zone = zone_of(coordinator, sub)
        super().__init__(coordinator, sub, "temperature")
        self._attr_translation_key = "room_temperature"
        self._attr_native_unit_of_measurement = coordinator.unit

    @property
    def native_value(self) -> float | None:
        room = self.coordinator.rooms.get(self.subentry.subentry_id)
        return room.temperature if room else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "floor": self.floor_name(self.subentry, self._zone),
            "role": self.subentry.data.get(CONF_ROLE),
            "zone": self._zone.title if self._zone else None,
            "hvac_unit": self._zone.data.get(CONF_UNIT) if self._zone else None,
            "sensors": {
                entity_id: self.coordinator.sensor_status.get(entity_id, "unknown")
                for entity_id in self.subentry.data.get(CONF_SENSORS, [])
            },
        }
