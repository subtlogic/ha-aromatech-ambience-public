"""Behavior checks for AromaTech light effects without a Home Assistant install."""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    sys.modules[name] = result
    return result


module("homeassistant")
module("homeassistant.components")
module(
    "homeassistant.components.light",
    ATTR_BRIGHTNESS="brightness",
    ATTR_EFFECT="effect",
    ATTR_RGB_COLOR="rgb_color",
    ColorMode=SimpleNamespace(RGB="rgb"),
    LightEntity=type("LightEntity", (), {}),
    LightEntityFeature=SimpleNamespace(EFFECT=4),
)
module("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
module("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}))
module("homeassistant.helpers")
module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
module("aromatech_ambience", __path__=[])
module("aromatech_ambience.const", DOMAIN="aromatech_ambience")
module("aromatech_ambience.coordinator", AmbienceCoordinator=type("AmbienceCoordinator", (), {}))


class Entity:
    def __init__(self, coordinator, key):
        self.coordinator = coordinator


module("aromatech_ambience.entity", AmbienceEntity=Entity)
path = Path(__file__).resolve().parents[1] / "custom_components/aromatech_ambience/light.py"
spec = importlib.util.spec_from_file_location("aromatech_ambience.light", path)
light = importlib.util.module_from_spec(spec)
spec.loader.exec_module(light)


class Coordinator:
    def __init__(self, state):
        self.data = state
        self.commands = []

    async def async_set_light(self, mode, *, rgb, brightness):
        self.commands.append((mode, rgb, brightness))


class LightEffectTest(unittest.TestCase):
    def test_off_is_selectable_and_sends_mode_zero(self):
        coordinator = Coordinator(SimpleNamespace(light_mode=1, rgb=(255, 160, 80), brightness=65))
        entity = light.AmbienceLight(coordinator)

        self.assertEqual(entity._attr_effect_list, ["Warm", "Cool", "Flow", "Off"])
        asyncio.run(entity.async_turn_on(effect="Off"))

        self.assertEqual(coordinator.commands, [(0, (255, 160, 80), 65)])
        coordinator.data.light_mode = 0
        self.assertFalse(entity.is_on)
        self.assertEqual(entity.effect, "Off")

    def test_bare_on_after_off_still_selects_warm(self):
        coordinator = Coordinator(SimpleNamespace(light_mode=0, rgb=(0, 0, 0), brightness=65))
        entity = light.AmbienceLight(coordinator)

        asyncio.run(entity.async_turn_on())

        self.assertEqual(coordinator.commands, [(1, (0, 0, 0), 65)])


if __name__ == "__main__":
    unittest.main()
