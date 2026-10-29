"""Options forms with real voluptuous and isolated HA-shaped flow/selector stubs.

These checks do not claim live Home Assistant frontend validation.
"""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

try:
    import voluptuous as vol
except ImportError:
    vol = None

ROOT = Path(__file__).parents[1] / "custom_components/aromatech_ambience"


class FormError(Exception):
    pass


def load_flow():
    def module(name, **attrs):
        result = types.ModuleType(name)
        result.__dict__.update(attrs)
        return result

    class Flow:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_show_menu(self, **kwargs):
            return {"type": "menu", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

    class SelectSelector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            return vol.In([option["value"] for option in self.config["options"]])(value)

    class NumberSelector:
        def __init__(self, config):
            self.config = config

        def __call__(self, value):
            if isinstance(value, bool):
                raise vol.Invalid("Not a number")
            return vol.All(vol.Coerce(float), vol.Range(min=self.config["min"], max=self.config["max"]))(value)

    selectors = module("homeassistant.helpers.selector", SelectSelector=SelectSelector,
                       SelectSelectorConfig=dict, SelectSelectorMode=types.SimpleNamespace(DROPDOWN="dropdown"),
                       NumberSelector=NumberSelector, NumberSelectorConfig=dict,
                       NumberSelectorMode=types.SimpleNamespace(BOX="box"))
    name = "ambience_forms_under_test"
    stubs = {
        "homeassistant": module("homeassistant"),
        "homeassistant.components": module("homeassistant.components", bluetooth=types.SimpleNamespace()),
        "homeassistant.config_entries": module("homeassistant.config_entries", ConfigEntry=object,
                                                ConfigFlow=Flow, ConfigFlowResult=dict, OptionsFlow=Flow),
        "homeassistant.const": module("homeassistant.const", CONF_ADDRESS="address"),
        "homeassistant.core": module("homeassistant.core", callback=lambda fn: fn),
        "homeassistant.exceptions": module("homeassistant.exceptions", HomeAssistantError=FormError),
        "homeassistant.helpers": module("homeassistant.helpers", selector=selectors),
        name: module(name, __path__=[str(ROOT)]),
    }
    spec = importlib.util.spec_from_file_location(name + ".config_flow", ROOT / "config_flow.py")
    loaded = importlib.util.module_from_spec(spec)
    stubs[spec.name] = loaded
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(loaded)
    return loaded


@unittest.skipUnless(vol is not None, "voluptuous unavailable; run dependency-equipped form lane")
class OptionsFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_flow()
        self.flow = self.module.AmbienceOptionsFlow()
        self.flow.config_entry = types.SimpleNamespace(entry_id="entry", options={"kept_option": 42})
        self.inventory = types.SimpleNamespace(
            scents={"scent-11111111": {"name": "Same"}, "scent-22222222": {"name": "Same"}},
            bottles={"bottle-11111111": {"name": "Same"}, "bottle-22222222": {"name": "Same"}},
            active_bottle_id="bottle-11111111")
        self.coordinator = types.SimpleNamespace(inventory=self.inventory,
                                                async_inventory_action=AsyncMock(), async_acknowledge_refill=AsyncMock())
        self.flow.hass = types.SimpleNamespace(data={"aromatech_ambience": {"entry": self.coordinator}})

    async def test_menu_exposes_every_management_route(self):
        result = await self.flow.async_step_init()
        self.assertEqual(result["type"], "menu")
        for step in result["menu_options"]:
            form = await getattr(self.flow, "async_step_" + step)()
            self.assertEqual(form["type"], "form")
            self.assertEqual(form["step_id"], step)

    async def test_duplicate_names_have_distinct_id_suffixes(self):
        form = await self.flow.async_step_edit_bottle()
        selected = next(value for key, value in form["data_schema"].schema.items() if key.schema == "bottle_id")
        self.assertEqual(selected.config["options"], [
            {"value": "bottle-11111111", "label": "Same (11111111)"},
            {"value": "bottle-22222222", "label": "Same (22222222)"}])

    async def test_mutation_returns_menu_preserves_options_and_only_sends_named_fields(self):
        result = await self.flow.async_step_edit_bottle({"bottle_id": "bottle-11111111", "name": "New"})
        self.coordinator.async_inventory_action.assert_awaited_once_with("edit_bottle", bottle_id="bottle-11111111", name="New")
        self.assertEqual(result["type"], "menu")
        self.assertEqual(self.flow.config_entry.options, {"kept_option": 42})

    async def test_every_operation_routes_to_shared_mutation_boundary(self):
        examples = {
            "add_scent": {"name": "Scent", "grams_per_spray": .1, "spray_seconds": 10},
            "edit_scent": {"scent_id": "scent-11111111", "brand": "Brand"},
            "delete_scent": {"scent_id": "scent-11111111", "confirm": True},
            "add_bottle": {"name": "Bottle", "scent_id": "scent-11111111", "empty_g": 50, "oil_g": 100},
            "install_bottle": {"bottle_id": "bottle-22222222"},
            "store_bottle": {"bottle_id": "bottle-11111111"},
            "record_weight": {"bottle_id": "bottle-11111111", "gross_g": 150},
            "correct_weight": {"bottle_id": "bottle-11111111", "gross_g": 149, "reason": "Scale correction"},
            "refill_bottle": {"bottle_id": "bottle-22222222", "scent_id": "scent-22222222", "gross_g": 150},
            "archive_bottle": {"bottle_id": "bottle-22222222", "archived": False},
            "delete_bottle": {"bottle_id": "bottle-22222222", "confirm": True},
        }
        for action, fields in examples.items():
            with self.subTest(action=action):
                self.coordinator.async_inventory_action.reset_mock()
                result = await getattr(self.flow, "async_step_" + action)(fields)
                self.assertEqual(result["type"], "menu")
                self.coordinator.async_inventory_action.assert_awaited_once_with(action, **fields)

    async def test_new_bottle_presets_default_custom_and_unknown_tare(self):
        base = {"name": "Bottle", "scent_id": "scent-11111111"}
        for size, expected in [(None, 60), ("30", 30), ("120", 120)]:
            self.coordinator.async_inventory_action.reset_mock()
            fields = {**base, **({"bottle_size": size} if size else {})}
            result = await self.flow.async_step_add_bottle(fields)
            self.assertEqual(result["type"], "menu")
            self.coordinator.async_inventory_action.assert_awaited_once_with("add_bottle", **base, oil_g=expected)
        for extra in ({"bottle_size": "custom", "oil_g": 42.5},
                      {"oil_g": 42.5}, {"bottle_size": "30", "gross_g": 82, "empty_g": 40}):
            self.coordinator.async_inventory_action.reset_mock()
            result = await self.flow.async_step_add_bottle({**base, **extra})
            self.assertEqual(result["type"], "menu")
            self.coordinator.async_inventory_action.assert_awaited_once_with(
                "add_bottle", **base, **{k: v for k, v in extra.items() if k != "bottle_size"})
        self.coordinator.async_inventory_action.reset_mock()
        for extra in ({"bottle_size": "custom"}, {"bottle_size": "invalid"}):
            result = await self.flow.async_step_add_bottle({**base, **extra})
            self.assertEqual(result["errors"], {"base": "invalid_input"})
        self.coordinator.async_inventory_action.assert_not_awaited()

    async def test_name_only_scent_does_not_inject_rate_into_edits(self):
        await self.flow.async_step_add_scent({"name": "Simple"})
        self.coordinator.async_inventory_action.assert_awaited_once_with("add_scent", name="Simple")
        self.coordinator.async_inventory_action.reset_mock()
        await self.flow.async_step_edit_scent({"scent_id": "scent-11111111", "name": "Rename"})
        self.coordinator.async_inventory_action.assert_awaited_once_with("edit_scent", scent_id="scent-11111111", name="Rename")

    async def test_delete_requires_explicit_confirmation(self):
        result = await self.flow.async_step_delete_bottle({"bottle_id": "bottle-11111111"})
        self.assertEqual(result["errors"], {"confirm": "confirmation_required"})
        self.coordinator.async_inventory_action.assert_not_awaited()

    async def test_settings_merge_existing_options_and_reject_equal_thresholds(self):
        result = await self.flow.async_step_settings({"low_oil_g": 0, "depleted_oil_g": 0})
        self.assertEqual(result["errors"], {"low_oil_g": "threshold_order"})
        result = await self.flow.async_step_settings({"refill_notifications": True})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["kept_option"], 42)
        self.assertTrue(result["data"]["refill_notifications"])
        self.assertFalse(result["data"]["experimental_schedule_writes"])
        self.assertEqual(result["data"]["refill_days"], 7)

    async def test_validation_and_persistence_errors_stay_on_form(self):
        for error in (ValueError("Referenced scent cannot be deleted"), FormError("Could not persist inventory")):
            self.coordinator.async_inventory_action.side_effect = error
            result = await self.flow.async_step_edit_scent({"scent_id": "scent-11111111", "name": "New"})
            self.assertEqual(result["type"], "form")
            self.assertEqual(result["errors"], {"base": "operation_failed"})
            self.assertEqual(result["description_placeholders"]["error"], str(error))

    async def test_stale_selection_and_bad_quantities_do_not_mutate(self):
        invalid = [{"bottle_id": "deleted", "gross_g": 100},
                   *({"bottle_id": "bottle-11111111", "gross_g": value} for value in (float("nan"), float("inf"), -1, True))]
        for fields in invalid:
            result = await self.flow.async_step_record_weight(fields)
            self.assertEqual(result["errors"], {"base": "invalid_input"})
        result = await self.flow.async_step_add_bottle({"name": "Bottle", "scent_id": "scent-11111111", "empty_g": 50, "gross_g": 100, "oil_g": 50})
        self.assertEqual(result["errors"], {"gross_g": "one_weight"})
        self.coordinator.async_inventory_action.assert_not_awaited()

    async def test_unloaded_entry_is_friendly_abort(self):
        self.flow.hass.data = {}
        result = await self.flow.async_step_record_weight()
        self.assertEqual(result, {"type": "abort", "reason": "not_loaded"})

    async def test_acknowledgement_does_not_create_options_entry(self):
        result = await self.flow.async_step_acknowledge({})
        self.assertEqual(result["type"], "menu")
        self.coordinator.async_acknowledge_refill.assert_awaited_once()

    async def test_every_form_field_is_accepted_by_inventory_contract(self):
        spec = importlib.util.spec_from_file_location("inventory_form_contract", ROOT / "inventory.py")
        inventory_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory_module)
        for action, accepted in inventory_module.Inventory._FIELDS.items():
            schema = self.flow._inventory_schema(action, self.inventory)
            offered = {key.schema if isinstance(key, vol.Marker) else key for key in schema.schema}
            self.assertLessEqual(offered - {"bottle_size"}, accepted, action)

    async def test_archived_bottle_not_offered_for_install(self):
        self.inventory.bottles["bottle-22222222"]["archived"] = True
        result = await self.flow.async_step_install_bottle({"bottle_id": "bottle-22222222"})
        self.assertEqual(result["errors"], {"base": "invalid_input"})
        self.coordinator.async_inventory_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
