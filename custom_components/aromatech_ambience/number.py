"""Schedule intensity for the AromaTech Ambience.

Intensity is the last byte of the 15-byte schedule frame - the app calls it
"diffuser spray speed and delay time" and only exposes it inside the schedule
editor. There is no live intensity command, so setting this rewrites the
schedule.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AmbienceCoordinator
from .entity import AmbienceEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AmbienceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AmbienceIntensity(coordinator)])


class AmbienceIntensity(AmbienceEntity, NumberEntity):
    """Spray intensity, 1 to 4."""

    _attr_name = "Intensity"
    _attr_icon = "mdi:scent"
    _attr_native_min_value = 1
    _attr_native_max_value = 4
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: AmbienceCoordinator) -> None:
        super().__init__(coordinator, "intensity")

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data
        return None if state is None else state.intensity

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_schedule(intensity=int(value))
