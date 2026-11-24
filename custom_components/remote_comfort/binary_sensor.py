"""Binary sensors: whether a zone is currently closed to push air elsewhere."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemoteComfortConfigEntry
from .const import SUBENTRY_ZONE
from .coordinator import RemoteComfortCoordinator
from .entity import ZoneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteComfortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for sub in entry.subentries.values():
        if sub.subentry_type == SUBENTRY_ZONE:
            async_add_entities(
                [BalancingSensor(entry.runtime_data, sub)], config_subentry_id=sub.subentry_id
            )


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
