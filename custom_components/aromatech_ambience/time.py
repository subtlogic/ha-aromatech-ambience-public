"""Schedule start and end times for the AromaTech Ambience.

The app treats each time as separate hour and minute bytes. Preserve both
when displaying and editing the first slot; retain every other schedule.
"""
from __future__ import annotations

from datetime import time as dt_time

from homeassistant.components.time import TimeEntity
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
    async_add_entities([
        AmbienceScheduleTime(coordinator, "start"),
        AmbienceScheduleTime(coordinator, "end"),
    ])


class AmbienceScheduleTime(AmbienceEntity, TimeEntity):
    """One end of the schedule window."""

    def __init__(self, coordinator: AmbienceCoordinator, which: str) -> None:
        super().__init__(coordinator, f"schedule_{which}")
        self._which = which
        # "from"/"to" rather than "start"/"end" so they sort in reading order.
        # Home Assistant lists entities alphabetically, and "Schedule end"
        # sorts above "Schedule start", which put the window backwards on the
        # device page.
        self._attr_name = "Schedule from" if which == "start" else "Schedule to"
        self._attr_icon = (
            "mdi:clock-start" if which == "start" else "mdi:clock-end"
        )

    @property
    def available(self):
        return super().available and not getattr(self.coordinator, "timed_schedule_controls_locked", getattr(self.coordinator, "always_on", False))

    @property
    def native_value(self) -> dt_time | None:
        state = self.coordinator.data
        if state is None:
            return None
        hour = state.start_hour if self._which == "start" else state.end_hour
        minute = state.start_minute if self._which == "start" else state.end_minute
        if hour is None or not 0 <= hour <= 23:
            return None
        if minute is None or not 0 <= minute <= 59:
            return None
        return dt_time(hour=hour, minute=minute)

    async def async_set_value(self, value: dt_time) -> None:
        if self._which == "start":
            await self.coordinator.async_set_schedule(start_hour=value.hour, start_minute=value.minute)
        else:
            await self.coordinator.async_set_schedule(end_hour=value.hour, end_minute=value.minute)
