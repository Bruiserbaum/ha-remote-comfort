"""Base entities for Remote Comfort."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FLOOR, CONF_UNIT, CONF_ZONE, DOMAIN, SUBENTRY_ROOM
from .coordinator import RemoteComfortCoordinator


class RemoteComfortEntity(CoordinatorEntity[RemoteComfortCoordinator]):
    """Entity on the main 'house' device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: RemoteComfortCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Remote Comfort {entry.title}",
            manufacturer="BaumHome",
            model="Remote Comfort",
        )


class ZoneEntity(CoordinatorEntity[RemoteComfortCoordinator]):
    """Entity on a zone's or room's own device (one device per subentry)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RemoteComfortCoordinator,
        subentry: ConfigSubentry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.subentry = subentry
        self._attr_unique_id = f"{entry.entry_id}_{subentry.subentry_id}_{key}"
        if subentry.subentry_type == SUBENTRY_ROOM:
            name, model = subentry.title, "Remote Comfort room"
        else:
            unit = subentry.data.get(CONF_UNIT)
            name = f"{subentry.title} zone"
            model = f"Remote Comfort zone ({unit})" if unit else "Remote Comfort zone"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{subentry.subentry_id}")},
            name=name,
            manufacturer="BaumHome",
            model=model,
        )

    def floor_name(self, *subentries: ConfigSubentry | None) -> str | None:
        registry = fr.async_get(self.hass)
        for sub in subentries:
            if sub and (floor_id := sub.data.get(CONF_FLOOR)):
                if floor := registry.async_get_floor(floor_id):
                    return floor.name
        return None


def zone_of(coordinator: RemoteComfortCoordinator, room: ConfigSubentry) -> ConfigSubentry | None:
    return coordinator.config_entry.subentries.get(room.data.get(CONF_ZONE, ""))
