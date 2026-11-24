"""Remote Comfort: schedule-, weather- and zone-aware HVAC control."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import RemoteComfortCoordinator

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type RemoteComfortConfigEntry = ConfigEntry[RemoteComfortCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> bool:
    """Set up Remote Comfort from a config entry."""
    coordinator = RemoteComfortCoordinator(hass, entry)
    # First pass only computes; thermostats are not touched until the
    # select/switch entities have restored their previous state.
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    coordinator.start_control()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> bool:
    """Unload a config entry, handing back any zones we switched off."""
    await entry.runtime_data.async_release_all()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> None:
    coordinator = entry.runtime_data
    if coordinator.consume_live_update():
        return
    # Options changed, or a zone/room was added, edited or removed.
    await hass.config_entries.async_reload(entry.entry_id)
