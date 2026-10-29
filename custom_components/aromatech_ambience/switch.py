"""Power and sound switches for the AromaTech Ambience."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AmbienceCoordinator
from .entity import AmbienceEntity

# Bit position in the schedule day mask -> (ordering index, short label).
#
# Sunday at bit 0 remains an inferred mapping pending full device validation.
#
# Names carry an index because Home Assistant sorts entities alphabetically,
# which would otherwise list these as Fri, Mon, Sat, Sun, Thu, Tue, Wed.
DAY_BITS = {
    1: (1, "Mon"),
    2: (2, "Tue"),
    3: (3, "Wed"),
    4: (4, "Thu"),
    5: (5, "Fri"),
    6: (6, "Sat"),
    0: (7, "Sun"),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AmbienceCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = [
        AmbiencePower(coordinator),
        AmbienceSound(coordinator),
        AmbienceAlwaysOn(coordinator),
        AmbienceScheduleEnabled(coordinator),
    ]
    entities += [AmbienceDay(coordinator, bit) for bit in DAY_BITS]
    async_add_entities(entities)


class AmbiencePower(AmbienceEntity, SwitchEntity):
    """Master on/off: whether the diffuser scents at all.

    Named "Scenting" rather than "Diffuser" so it does not read as a second
    strength control beside Intensity. The unique id keeps its original
    "power" key, so existing entity ids are unaffected.
    """

    _attr_name = "Scenting"

    def __init__(self, coordinator: AmbienceCoordinator) -> None:
        super().__init__(coordinator, "power")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.power if self.coordinator.data else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(True)

    @property
    def icon(self) -> str:
        # mdi:scent is the diffuser glyph - vapour rising from a bottle. The
        # previous mdi:air-filter is an HVAC filter, which is a different
        # appliance entirely.
        if self.is_on is None:
            return "mdi:scent-off"
        return "mdi:scent" if self.is_on else "mdi:scent-off"

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power(False)


class AmbienceSound(AmbienceEntity, SwitchEntity):
    """The unit's audible feedback, not the diffuser."""

    _attr_name = "Sound"

    def __init__(self, coordinator: AmbienceCoordinator) -> None:
        super().__init__(coordinator, "sound")

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.sound if self.coordinator.data else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_sound(True)

    @property
    def icon(self) -> str:
        # Showing volume-high while muted was actively misleading.
        if self.is_on is None:
            return "mdi:volume-variant-off"
        return "mdi:volume-high" if self.is_on else "mdi:volume-off"

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_sound(False)


class AmbienceAlwaysOn(AmbienceEntity, SwitchEntity):
    """All-week cycling with a durable restore point for the timed schedule."""

    _attr_name = "Always on"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator):
        super().__init__(coordinator, "always_on")

    @property
    def is_on(self):
        return self.coordinator.always_on

    @property
    def extra_state_attributes(self):
        return self.coordinator.always_on_attributes

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_always_on(True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_always_on(False)


class AmbienceScheduleEnabled(AmbienceEntity, SwitchEntity):
    """Whether the stored schedule is active.

    This is bit 7 of the schedule's day mask. That reading is inference from
    three observed masks rather than proof, so this switch flips only that bit
    and leaves every other byte of the frame untouched.
    """

    _attr_name = "Schedule enabled"

    def __init__(self, coordinator: AmbienceCoordinator) -> None:
        super().__init__(coordinator, "schedule_enabled")

    @property
    def available(self):
        return super().available and not getattr(self.coordinator, "timed_schedule_controls_locked", getattr(self.coordinator, "always_on", False))

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.data
        return None if state is None else state.schedule_enabled

    @property
    def icon(self) -> str:
        if self.is_on is None:
            return "mdi:calendar-question"
        return "mdi:calendar-check" if self.is_on else "mdi:calendar-remove"

    async def _set(self, enabled: bool) -> None:
        state = self.coordinator.data
        if state is None or state.days_mask is None:
            raise HomeAssistantError("no schedule read from the device yet")
        await self.coordinator.async_set_schedule(enabled=enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class AmbienceDay(AmbienceEntity, SwitchEntity):
    """One weekday of the schedule - a single bit of the day mask.

    Flips only its own bit and hands the whole mask back, so the other days and
    the enable flag in bit 7 are preserved regardless of what they were.
    """

    def __init__(self, coordinator: AmbienceCoordinator, bit: int) -> None:
        order, label = DAY_BITS[bit]
        super().__init__(coordinator, f"day_{label.lower()}")
        self._bit = bit
        self._attr_name = f"Schedule {order} {label}"
        self._attr_entity_registry_enabled_default = True

    @property
    def available(self):
        return super().available and not getattr(self.coordinator, "timed_schedule_controls_locked", getattr(self.coordinator, "always_on", False))

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.data
        if state is None or state.days_mask is None:
            return None
        return bool(state.days_mask & (1 << self._bit))

    @property
    def icon(self) -> str:
        if self.is_on is None:
            return "mdi:calendar-question"
        return "mdi:calendar-check" if self.is_on else "mdi:calendar-blank"

    async def _set(self, on: bool) -> None:
        state = self.coordinator.data
        if state is None or state.days_mask is None:
            raise HomeAssistantError("no schedule read from the device yet")
        await self.coordinator.async_set_schedule(day_bit=self._bit, day_on=on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
