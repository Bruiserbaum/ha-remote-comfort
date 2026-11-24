"""Switch: master enable for thermostat control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RemoteComfortConfigEntry
from .coordinator import RemoteComfortCoordinator
from .entity import RemoteComfortEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteComfortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ControlSwitch(entry.runtime_data)])


class ControlSwitch(RemoteComfortEntity, SwitchEntity, RestoreEntity):
    """When off, Remote Comfort computes but never touches thermostats."""

    def __init__(self, coordinator: RemoteComfortCoordinator) -> None:
        super().__init__(coordinator, "control")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state == STATE_OFF:
            self.coordinator.enabled = False

    @property
    def is_on(self) -> bool:
        return self.coordinator.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.enabled = True
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.enabled = False
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
