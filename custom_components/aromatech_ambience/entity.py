"""Shared entity base for the AromaTech Ambience."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AmbienceCoordinator


class AmbienceEntity(CoordinatorEntity[AmbienceCoordinator]):
    """Base class carrying the device registry entry."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AmbienceCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        state = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            connections={("bluetooth", self.coordinator.address)},
            manufacturer="AromaTech",
            model="Ambience",
            name=(state.name if state and state.name else "Ambience"),
            sw_version=(state.firmware if state else None),
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data is not None
