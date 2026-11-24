"""Binary sensors: whether a zone is currently closed to push air elsewhere."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemoteComfortConfigEntry
from .const import SUBENTRY_ZONE
from .coordinator import RemoteComfortCoordinator
from .entity import RemoteComfortEntity, ZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteComfortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ProblemSensor(entry.runtime_data)])
    for sub in entry.subentries.values():
        if sub.subentry_type == SUBENTRY_ZONE:
            async_add_entities(
                [BalancingSensor(entry.runtime_data, sub)], config_subentry_id=sub.subentry_id
            )


class ProblemSensor(RemoteComfortEntity, BinarySensorEntity):
    """On while any input is missing/bad or a thermostat command failed."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: RemoteComfortCoordinator) -> None:
        super().__init__(coordinator, "problem")

    @property
    def available(self) -> bool:
        return True  # report problems even when an update failed

    @property
    def is_on(self) -> bool:
        c = self.coordinator
        return bool(c.problems) or not c.last_update_success or c.failsafe_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        problems = [p.message for p in self.coordinator.problems.values()]
        if not self.coordinator.last_update_success and self.coordinator.last_exception:
            problems.insert(0, f"Update failed: {self.coordinator.last_exception}")
        if self.coordinator.failsafe_active:
            problems.insert(0, "FAILSAFE ACTIVE: thermostats are at the failsafe setpoints")
        return {
            "problems": problems,
            "unavailable_zones": self.coordinator.unavailable_zones,
        }


class BalancingSensor(ZoneEntity, BinarySensorEntity):
    _attr_translation_key = "balancing"

    def __init__(self, coordinator: RemoteComfortCoordinator, sub: ConfigSubentry) -> None:
        super().__init__(coordinator, sub, "balancing")

    @property
    def is_on(self) -> bool:
        dec = self.coordinator.data.zones.get(self.subentry.subentry_id)
        return bool(dec and dec.suppressed)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        dec = self.coordinator.data.zones.get(self.subentry.subentry_id)
        return {"reason": dec.reason} if dec else {}
