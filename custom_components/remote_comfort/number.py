"""Numbers: the everyday setpoints, adjustable from a dashboard."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemoteComfortConfigEntry
from .const import LIVE_SETPOINTS
from .coordinator import RemoteComfortCoordinator
from .entity import RemoteComfortEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteComfortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(SetpointNumber(coordinator, key) for key in LIVE_SETPOINTS)


class SetpointNumber(RemoteComfortEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: RemoteComfortCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key
        self._attr_native_unit_of_measurement = coordinator.unit
        if coordinator.unit == UnitOfTemperature.CELSIUS:
            self._attr_native_min_value, self._attr_native_max_value, self._attr_native_step = 5, 35, 0.5
        else:
            self._attr_native_min_value, self._attr_native_max_value, self._attr_native_step = 40, 95, 1

    @property
    def native_value(self) -> float:
        return float(self.coordinator.options[self._key])

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_option(self._key, value)
        self.async_write_ha_state()
