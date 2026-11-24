"""Diagnostics download: everything needed to understand a decision."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import RemoteComfortConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: RemoteComfortConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    decision = coordinator.data
    return {
        "options": dict(entry.options),
        "subentries": [
            {"id": s.subentry_id, "type": s.subentry_type, "title": s.title, "data": dict(s.data)}
            for s in entry.subentries.values()
        ],
        "decision": asdict(decision) if decision else None,
        "last_update_success": coordinator.last_update_success,
        "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
        "problems": [asdict(p) for p in coordinator.problems.values()],
        "sensor_status": coordinator.sensor_status,
        "rooms": {k: asdict(v) for k, v in coordinator.rooms.items()},
        "weather": asdict(coordinator.weather),
        "weather_updated": coordinator.weather_updated.isoformat() if coordinator.weather_updated else None,
        "occupied": coordinator.occupied,
        "mode": coordinator.mode.value,
        "enabled": coordinator.enabled,
        "day_override": coordinator.day_override.value if coordinator.day_override else None,
        "holds": {k: v.value for k, v in coordinator.holds.items()},
        "suppressed": coordinator.engine.export_state(),
        "unavailable_zones": coordinator.unavailable_zones,
    }
