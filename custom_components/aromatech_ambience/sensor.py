"""Status, usage estimates and optional diagnostic sensors for AromaTech Ambience."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AmbienceCoordinator
from .entity import AmbienceEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AmbienceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AmbienceSchedule(coordinator),
        AmbienceTiming(coordinator, "spray", 0),
        AmbienceTiming(coordinator, "pause", 1),
        *[AmbienceUsage(coordinator, key) for key in USAGE_SENSORS],
        *[AmbienceBottle(coordinator, key) for key in BOTTLE_SENSORS],
        *[AmbienceBluetooth(coordinator, key) for key in HEALTH_SENSORS],
    ])


class AmbienceSchedule(AmbienceEntity, SensorEntity):
    """The active schedule, as the device reports it."""

    _attr_name = "Schedule"

    def __init__(self, coordinator: AmbienceCoordinator) -> None:
        super().__init__(coordinator, "schedule")

    @property
    def icon(self) -> str:
        state = self.coordinator.data
        if state is None or state.schedule_enabled is None:
            return "mdi:calendar-question"
        return "mdi:calendar-clock" if state.schedule_enabled else "mdi:calendar-remove"

    @property
    def native_value(self) -> str | None:
        s = self.coordinator.data
        if s is None or s.schedules is None:
            return None
        if not s.schedules:
            return "no schedules"
        if len(s.schedules) > 1:
            return f"{len(s.schedules)} schedules"
        if not s.schedule_enabled:
            return "disabled"
        days = ",".join(s.days) if s.days else "no days"
        if (s.start_hour, s.start_minute) == (s.end_hour, s.end_minute) and (s.start_hour or s.start_minute):
            return f"{days} 24 hours"
        return f"{days} {s.start_hour:02d}:{s.start_minute:02d}-{s.end_hour:02d}:{s.end_minute:02d}"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        s = self.coordinator.data
        if s is None:
            return {}
        return {
            "enabled": s.schedule_enabled,
            "days": s.days,
            "days_mask": s.days_mask,
            "start_hour": s.start_hour,
            "end_hour": s.end_hour,
            "start_minute": s.start_minute,
            "end_minute": s.end_minute,
            "schedule_status": s.schedule_status,
            "schedule_count": len(s.schedules) if s.schedules is not None else None,
            "schedules": None if s.schedules is None else [
                {"slot": i + 1, "enabled": bool(slot.days_mask & 0x80),
                 "days_mask": slot.days_mask & 0x7F,
                 "start": f"{slot.start_hour:02d}:{slot.start_minute:02d}",
                 "end": f"{slot.end_hour:02d}:{slot.end_minute:02d}",
                 "intensity": slot.intensity}
                for i, slot in enumerate(s.schedules)
            ],
            "intensity": s.intensity,
        }


class AmbienceTiming(AmbienceEntity, SensorEntity):
    """Device-reported preset duration for the first schedule's intensity."""

    _attr_native_unit_of_measurement = "s"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, name, index):
        super().__init__(coordinator, f"schedule_{name}_duration")
        self._attr_name = f"Schedule 1 {name} duration"
        self._index = index

    @property
    def native_value(self):
        state = self.coordinator.data
        if (state is None or state.intensity is None or not state.intensity_presets
                or not 1 <= state.intensity <= len(state.intensity_presets)):
            return None
        return state.intensity_presets[state.intensity - 1][self._index]


USAGE_SENSORS = {
    "expected_sprays": ("Estimated sprays", "sprays", None, getattr(SensorStateClass, "TOTAL", SensorStateClass.MEASUREMENT)),
    "active_seconds": ("Estimated spray time", "s", SensorDeviceClass.DURATION, getattr(SensorStateClass, "TOTAL", SensorStateClass.MEASUREMENT)),
    "observed_seconds": ("Observed estimation time", "s", SensorDeviceClass.DURATION, getattr(SensorStateClass, "TOTAL", SensorStateClass.MEASUREMENT)),
    "unobserved_seconds": ("Unobserved estimation time", "s", SensorDeviceClass.DURATION, getattr(SensorStateClass, "TOTAL", SensorStateClass.MEASUREMENT)),
    "coverage": ("Estimation coverage", "%", None, SensorStateClass.MEASUREMENT),
    "estimated_remaining_g": ("Estimated oil remaining", "g", SensorDeviceClass.WEIGHT, SensorStateClass.MEASUREMENT),
}


class AmbienceUsage(AmbienceEntity, SensorEntity):
    """Persisted model totals; never presented as hardware spray telemetry."""

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self._key = key
        (self._attr_name, self._attr_native_unit_of_measurement,
         self._attr_device_class, self._attr_state_class) = USAGE_SENSORS[key]
        if key != "estimated_remaining_g":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self):
        # Restored totals remain meaningful even while BLE is unavailable.
        return True

    @property
    def native_value(self):
        manager = getattr(self.coordinator, "inventory_manager", None)
        if self._key == "estimated_remaining_g" and manager and manager.owns_remaining:
            value = manager.summary()["remaining_g"]
        else:
            value = getattr(self.coordinator.estimates, self._key)
        if value is None:
            return None
        return round(value * 100 if self._key == "coverage" else value, 4)

    @property
    def extra_state_attributes(self):
        manager = getattr(self.coordinator, "inventory_manager", None)
        if self._key == "estimated_remaining_g" and manager and manager.owns_remaining:
            summary = manager.summary()
            return {"estimated": True, "bottle_id": manager.inventory.active_bottle_id,
                    "fill_id": summary["fill_id"], "confidence": summary["confidence"],
                    "rate_source": summary.get("rate_source"), "mass_basis": summary.get("mass_basis"),
                    "remaining_lower_g": summary.get("remaining_lower_g"),
                    "remaining_upper_g": summary.get("remaining_upper_g"),
                    "uncertainty_g": summary.get("uncertainty_g")}
        estimates = self.coordinator.estimates
        if self._key == "estimated_remaining_g":
            return {"estimated": True, "calibration_status": estimates.calibration_status}
        return {
            "estimated": True,
            "calibration_status": estimates.calibration_status,
            "unknown_seconds_since_checkpoint": estimates.unknown_seconds_since_checkpoint,
            "model": "Elapsed time under last known schedule and intensity; Bluetooth confirms assumptions, not physical spray events",
            "last_confirmation": estimates.last_contact_at.isoformat() if estimates.last_contact_at else None,
            "command_status": estimates.command_status,
            "adjustment_cycles": estimates.adjustment_cycles,
            "adjustment_active_seconds": estimates.adjustment_active_seconds,
            "accumulated_cycles": estimates.expected_sprays - estimates.adjustment_cycles,
            "accumulated_active_seconds": estimates.active_seconds - estimates.adjustment_active_seconds,
            "last_adjustment": estimates.adjustments[-1] if estimates.adjustments else None,
            "weekday_mapping": "Sunday bit 0 remains inferred",
        }


BOTTLE_SENSORS = {
    "active_bottle": ("Installed bottle", None, None),
    "active_scent": ("Installed scent", None, None),
    "fill_sprays": ("Bottle estimated sprays", "sprays", None),
    "fill_active_seconds": ("Bottle estimated spray time", "s", SensorDeviceClass.DURATION),
    "fill_consumed_g": ("Bottle estimated oil consumed", "g", SensorDeviceClass.WEIGHT),
    "rate_mg_per_second": ("Estimated oil per spraying second", "mg/s", None),
    "remaining_percent": ("Bottle oil remaining", "%", None),
    "daily_g": ("Expected daily oil use", "g/d", None),
    "days_remaining": ("Estimated days remaining", "d", SensorDeviceClass.DURATION),
    "status": ("Bottle refill status", None, None),
}


BOTTLE_DIAGNOSTICS = {"fill_sprays", "fill_active_seconds", "fill_consumed_g", "rate_mg_per_second", "daily_g"}


class AmbienceBottle(AmbienceEntity, SensorEntity):
    """Current fill values; lifetime counters remain separate on swaps."""

    def __init__(self, coordinator, key):
        super().__init__(coordinator, f"bottle_{key}")
        self._key = key
        self._attr_name, self._attr_native_unit_of_measurement, self._attr_device_class = BOTTLE_SENSORS[key]
        if key in BOTTLE_DIAGNOSTICS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False
        if key in ("fill_consumed_g", "rate_mg_per_second"):
            self._attr_suggested_display_precision = 3

    @property
    def available(self):
        return getattr(self.coordinator, "inventory_manager", None) is not None

    @property
    def native_value(self):
        if not self.available:
            return None
        value = self.coordinator.inventory_manager.summary()[self._key]
        return round(value, 3) if isinstance(value, (float, int)) else value

    @property
    def extra_state_attributes(self):
        if not self.available:
            return {}
        manager = self.coordinator.inventory_manager
        summary = manager.summary()
        if self._key not in BOTTLE_DIAGNOSTICS:
            return {"estimated": True, "bottle_id": manager.inventory.active_bottle_id,
                    "fill_id": summary["fill_id"], "confidence": summary["confidence"],
                    "rate_source": summary.get("rate_source"), "mass_basis": summary.get("mass_basis"),
                    "remaining_lower_g": summary.get("remaining_lower_g"),
                    "remaining_upper_g": summary.get("remaining_upper_g"),
                    "uncertainty_g": summary.get("uncertainty_g")}
        audit = next((event for event in reversed(getattr(manager, "audit", []))
                      if event.get("bottle_id") == manager.inventory.active_bottle_id
                      and event.get("fill_id") == summary["fill_id"]), {})
        return {"bottle_id": manager.inventory.active_bottle_id, "fill_id": summary["fill_id"],
                "confidence": summary["confidence"], "estimated": True,
                "remaining_lower_g": summary.get("remaining_lower_g"),
                "remaining_upper_g": summary.get("remaining_upper_g"),
                "uncertainty_g": summary.get("uncertainty_g"),
                "rate_mg_per_second": summary.get("rate_mg_per_second"),
                "five_second_dose_mg": summary.get("five_second_dose_mg"),
                "rate_source": summary.get("rate_source"),
                "last_adjustment_reason": audit.get("reason"),
                "last_adjustment_at": audit.get("at"),
                "last_adjustment_consumed_g": audit.get("consumed_g"),
                "last_mass_correction_g": audit.get("remaining_adjustment_g"),
                "usage_basis": "Estimated active pump seconds; five-second dose is an equivalent unit, not a physical burst",
                "forecast_basis": "Seven-day mean of the configured schedule; assumes continued use"}


HEALTH_SENSORS = {
    "bluetooth_health": ("Bluetooth health", None, None),
    "last_bluetooth_contact": ("Last Bluetooth contact", None, getattr(SensorDeviceClass, "TIMESTAMP", None)),
    "bluetooth_confirmation_age": ("Bluetooth confirmation age", "s", SensorDeviceClass.DURATION),
}


class AmbienceBluetooth(AmbienceEntity, SensorEntity):
    """Health remains readable while the device itself is unavailable."""

    def __init__(self, coordinator, key):
        super().__init__(coordinator, key)
        self._key = key
        self._attr_name, self._attr_native_unit_of_measurement, self._attr_device_class = HEALTH_SENSORS[key]
        if key != "bluetooth_health":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        if self._key == "last_bluetooth_contact":
            return self.coordinator.estimates.last_contact_at
        value = getattr(self.coordinator, self._key)
        return round(value, 1) if isinstance(value, (float, int)) else value

    @property
    def extra_state_attributes(self):
        estimates = self.coordinator.estimates
        return {"heartbeat_interval": self.coordinator.scan_interval,
                "consecutive_failures": estimates.consecutive_failures,
                "last_success": estimates.last_contact_at.isoformat() if estimates.last_contact_at else None}
