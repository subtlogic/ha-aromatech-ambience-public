"""User-requested local diagnostics for the AromaTech Ambience.

Includes recent BLE frames for troubleshooting. Review a downloaded diagnostic
report before sharing it; the integration does not publish it automatically."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import AmbienceCoordinator

TO_REDACT = {CONF_ADDRESS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: AmbienceCoordinator = hass.data[DOMAIN][entry.entry_id]
    state = coordinator.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "inventory": None if coordinator.inventory_manager is None else {
            "scent_count": len(coordinator.inventory.scents),
            "bottle_count": len(coordinator.inventory.bottles),
            "initialized": coordinator.inventory.initialized,
            "status": coordinator.inventory_manager.summary()["status"],
            "confidence": coordinator.inventory_manager.summary()["confidence"],
            "storage_error": coordinator.inventory_manager.error,
        },
        "estimation": {
            "estimated_sprays": coordinator.estimates.expected_sprays,
            "estimated_active_seconds": coordinator.estimates.active_seconds,
            "observed_seconds": coordinator.estimates.observed_seconds,
            "unobserved_seconds": coordinator.estimates.unobserved_seconds,
            "coverage": coordinator.estimates.coverage,
            "calibration_status": coordinator.estimates.calibration_status,
            "accounting_at": coordinator.estimates._at.isoformat() if coordinator.estimates._at else None,
            "assumptions": coordinator.estimates.encode_inputs(coordinator.estimates._inputs),
            "adjustment_cycles": coordinator.estimates.adjustment_cycles,
            "adjustment_active_seconds": coordinator.estimates.adjustment_active_seconds,
            "adjustments": list(coordinator.estimates.adjustments),
            "command_status": coordinator.estimates.command_status,
            "bluetooth_health": coordinator.bluetooth_health,
            "last_contact": coordinator.estimates.last_contact_at.isoformat() if coordinator.estimates.last_contact_at else None,
            "confirmation_age": coordinator.bluetooth_confirmation_age,
            "consecutive_failures": coordinator.estimates.consecutive_failures,
            "clock_error": bool(coordinator.estimates._at is not None and coordinator.estimates._at > dt_util.utcnow()),
        },
        "state": None if state is None else {
            "power": state.power,
            "light_mode": state.light_mode_name,
            "rgb": state.rgb,
            "brightness": state.brightness,
            "sound": state.sound,
            "days_mask": state.days_mask,
            "start_hour": state.start_hour,
            "end_hour": state.end_hour,
            "start_minute": state.start_minute,
            "end_minute": state.end_minute,
            "schedule_status": state.schedule_status,
            "schedule_count": len(state.schedules) if state.schedules is not None else None,
            "presets": state.intensity_presets,
            "run_code": state.run_code,
            "intensity": state.intensity,
            "firmware": state.firmware,
            "raw": {f"{cmd:#06x}": payload.hex(" ") for cmd, payload in state.raw.items()},
        },
        "trace": list(coordinator.trace),
    }
