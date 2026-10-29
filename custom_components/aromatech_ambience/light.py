"""Light entity for the AromaTech Ambience.

The device's light frame is `04 <R> <G> <B> <brightness> <mode>`, which maps
onto Home Assistant's light model almost exactly - so this is a light entity
with a colour wheel, not the select it started as.

Mode 4 (Custom) is the only one that honours the RGB bytes. The presets - Warm,
Cool, Flow - ignore them, so they are exposed as *effects*, which is how Home
Assistant models "a fixed look the device produces itself". Choosing a colour
implies Custom; choosing an effect leaves the stored colour alone.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AmbienceCoordinator
from .entity import AmbienceEntity

MODE_OFF = 0
MODE_WARM = 1
MODE_CUSTOM = 4
# Index into the protocol's mode byte. Off is selectable beside the fixed looks;
# Custom is selected when the user chooses an RGB color.
EFFECTS = {"Warm": 1, "Cool": 2, "Flow": 3, "Off": 0}
EFFECT_BY_MODE = {v: k for k, v in EFFECTS.items()}

# Device brightness is 0-100; Home Assistant uses 0-255.
DEVICE_MAX = 100


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: AmbienceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AmbienceLight(coordinator)])


class AmbienceLight(AmbienceEntity, LightEntity):
    """The diffuser's lamp."""

    _attr_name = "Light"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = list(EFFECTS)

    def __init__(self, coordinator: AmbienceCoordinator) -> None:
        super().__init__(coordinator, "light")

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.data
        if state is None or state.light_mode is None:
            return None
        return state.light_mode != MODE_OFF

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        state = self.coordinator.data
        return state.rgb if state else None

    @property
    def brightness(self) -> int | None:
        state = self.coordinator.data
        if state is None or state.brightness is None:
            return None
        return round(state.brightness * 255 / DEVICE_MAX)

    @property
    def effect(self) -> str | None:
        state = self.coordinator.data
        if state is None or state.light_mode is None:
            return None
        return EFFECT_BY_MODE.get(state.light_mode)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply whatever was asked for, keeping the rest as the device has it."""
        state = self.coordinator.data
        rgb = kwargs.get(ATTR_RGB_COLOR) or (state.rgb if state else None) or (255, 255, 255)

        if ATTR_BRIGHTNESS in kwargs:
            brightness = round(kwargs[ATTR_BRIGHTNESS] * DEVICE_MAX / 255)
        elif state is not None and state.brightness is not None:
            brightness = state.brightness
        else:
            brightness = DEVICE_MAX

        if ATTR_EFFECT in kwargs:
            mode = EFFECTS.get(kwargs[ATTR_EFFECT], MODE_CUSTOM)
        elif ATTR_RGB_COLOR in kwargs:
            # Picking a colour only means anything in Custom.
            mode = MODE_CUSTOM
        elif state is not None and state.light_mode not in (None, MODE_OFF):
            mode = state.light_mode
        else:
            # Off reports mode 0 and often stores RGB (0, 0, 0). A bare On
            # must choose a visible preset rather than Custom black.
            mode = MODE_WARM

        await self.coordinator.async_set_light(
            mode, rgb=tuple(rgb), brightness=max(1, min(DEVICE_MAX, brightness))
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        state = self.coordinator.data
        rgb = (state.rgb if state and state.rgb else (0, 0, 0))
        brightness = state.brightness if state and state.brightness else DEVICE_MAX
        await self.coordinator.async_set_light(MODE_OFF, rgb=rgb, brightness=brightness)
