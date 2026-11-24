"""Remote Comfort: schedule-, weather- and zone-aware HVAC control."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant

from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .coordinator import STORAGE_VERSION, RemoteComfortCoordinator

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
    await coordinator.async_load_state()
    # First pass only computes; thermostats are not touched until the
    # select/switch entities have restored their previous state.
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_on_stop)
    )
    coordinator.start_control()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> bool:
    """Unload a config entry, handing back any zones we switched off."""
    coordinator = entry.runtime_data
    await coordinator.async_release_all()
    coordinator.async_clear_issues()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> None:
    """Delete saved state when the integration is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_update_listener(hass: HomeAssistant, entry: RemoteComfortConfigEntry) -> None:
    coordinator = entry.runtime_data
    if coordinator.consume_live_update():
        return
    # Options changed, or a zone/room was added, edited or removed.
    await hass.config_entries.async_reload(entry.entry_id)
