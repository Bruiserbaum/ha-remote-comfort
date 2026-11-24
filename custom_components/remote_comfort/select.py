"""Selects: house mode override and 'today is...' override."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RemoteComfortConfigEntry
from .const import DAY_OVERRIDES, SELECTABLE_MODES
from .coordinator import RemoteComfortCoordinator
from .engine import DayType, Mode
from .entity import RemoteComfortEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteComfortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([ModeSelect(coordinator), DayOverrideSelect(coordinator)])


class ModeSelect(RemoteComfortEntity, SelectEntity, RestoreEntity):
    _attr_options = SELECTABLE_MODES

    def __init__(self, coordinator: RemoteComfortCoordinator) -> None:
        super().__init__(coordinator, "mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) and last.state in SELECTABLE_MODES:
            self.coordinator.mode = Mode(last.state)

    @property
    def current_option(self) -> str:
        return self.coordinator.mode.value

    async def async_select_option(self, option: str) -> None:
        self.coordinator.mode = Mode(option)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class DayOverrideSelect(RemoteComfortEntity, SelectEntity):
    """Treat today as a remote/office/off day. Resets to auto at midnight."""

    _attr_options = DAY_OVERRIDES

    def __init__(self, coordinator: RemoteComfortCoordinator) -> None:
        super().__init__(coordinator, "today")

    @property
    def current_option(self) -> str:
        value = self.coordinator.day_override
        return value.value if value else "auto"

    async def async_select_option(self, option: str) -> None:
        self.coordinator.set_day_override(None if option == "auto" else DayType(option))
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
