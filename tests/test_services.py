"""Service validation and routing with real voluptuous and isolated HA stubs.

These tests exercise our schemas/handlers, not a running Home Assistant instance.
The project stdlib-only lane skips them if voluptuous is not installed.
"""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import voluptuous as vol
except ImportError:
    vol = None


ROOT = Path(__file__).parents[1] / "custom_components/aromatech_ambience"
DOMAIN = "aromatech_ambience"


class ServiceError(Exception):
    pass


def load_services():
    """Restore sys.modules after import so other test stubs cannot be polluted."""
    def module(name, **attrs):
        result = types.ModuleType(name)
        result.__dict__.update(attrs)
        return result

    def string(value):
        if not isinstance(value, str):
            raise vol.Invalid("Expected string")
        return value

    def boolean(value):
        if type(value) is not bool:
            raise vol.Invalid("Expected boolean")
        return value

    name = "ambience_service_schema_test"
    stubs = {
        "homeassistant": module("homeassistant"),
        "homeassistant.core": module("homeassistant.core", HomeAssistant=object,
                                     ServiceCall=object, SupportsResponse=types.SimpleNamespace(ONLY="only"), callback=lambda fn: fn),
        "homeassistant.exceptions": module("homeassistant.exceptions", HomeAssistantError=ServiceError),
        "homeassistant.helpers": module("homeassistant.helpers"),
        "homeassistant.helpers.config_validation": module(
            "homeassistant.helpers.config_validation", string=string, boolean=boolean),
        name: module(name, __path__=[str(ROOT)]),
        name + ".const": module(name + ".const", DOMAIN=DOMAIN),
    }
    spec = importlib.util.spec_from_file_location(name + ".services", ROOT / "services.py")
    loaded = importlib.util.module_from_spec(spec)
    stubs[spec.name] = loaded
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(loaded)
    return loaded, stubs


class Registry:
    def __init__(self):
        self.entries = {}
        self.registrations = 0
        self.responses = {}

    def has_service(self, domain, name):
        return (domain, name) in self.entries

    def async_register(self, domain, name, handler, *, schema, supports_response=None):
        self.entries[domain, name] = handler, schema
        self.registrations += 1
        self.responses[name] = supports_response

    async def call(self, name, data):
        handler, schema = self.entries[DOMAIN, name]
        return await handler(types.SimpleNamespace(data=schema(data)))


@unittest.skipUnless(vol is not None, "voluptuous unavailable; run optional dependency-backed service lane")
class TestServices(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.services, stubs = load_services()
        module_patch = patch.dict(sys.modules, stubs)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.coordinator = types.SimpleNamespace(
            async_set_schedule=AsyncMock(), async_delete_schedule=AsyncMock(),
            async_record_bottle_weight=AsyncMock(), async_inventory_action=AsyncMock(),
            async_acknowledge_refill=AsyncMock(), inventory_manager=types.SimpleNamespace(export=Mock(return_value={"inventory": {}})))
        self.entry = types.SimpleNamespace(domain=DOMAIN)
        self.registry = Registry()
        self.hass = types.SimpleNamespace(
            services=self.registry, data={DOMAIN: {"device": self.coordinator}},
            config_entries=types.SimpleNamespace(async_get_entry=lambda entry_id:
                self.entry if entry_id == "device" else None))
        self.services.async_setup_services(self.hass)

    async def test_registration_is_idempotent(self):
        first = dict(self.registry.entries)
        self.services.async_setup_services(self.hass)
        self.assertEqual(self.registry.registrations, 6)
        self.assertEqual(self.registry.entries, first)
        self.assertEqual({name for domain, name in first},
                         {"set_schedule", "delete_schedule", "record_bottle_weight", "manage_inventory", "list_inventory", "acknowledge_refill"})

    async def test_inventory_routes_same_mutation_entrypoint(self):
        await self.registry.call("manage_inventory", {"config_entry_id": "device", "action": "add_scent", "name": "Fir"})
        self.coordinator.async_inventory_action.assert_awaited_once_with("add_scent", name="Fir")
        self.coordinator.async_inventory_action.side_effect = ServiceError("Unknown bottle")
        with self.assertRaises(ServiceError):
            await self.registry.call("manage_inventory", {"config_entry_id": "device", "action": "install_bottle", "bottle_id": "missing"})

    async def test_simple_setup_allows_omitted_tare_and_rate_without_injecting_edit_defaults(self):
        for action, fields in [("add_scent", {"name": "Simple"}),
                               ("add_bottle", {"name": "Bottle", "scent_id": "scent"}),
                               ("refill_bottle", {"bottle_id": "bottle", "scent_id": "scent"}),
                               ("edit_scent", {"scent_id": "scent", "name": "Rename"}),
                               ("edit_bottle", {"bottle_id": "bottle", "name": "Rename"})]:
            self.coordinator.async_inventory_action.reset_mock()
            await self.registry.call("manage_inventory", {"config_entry_id": "device", "action": action, **fields})
            self.coordinator.async_inventory_action.assert_awaited_once_with(action, **fields)

    async def test_inventory_response_only_and_acknowledgement(self):
        self.assertEqual(self.registry.responses["list_inventory"], "only")
        self.assertEqual(await self.registry.call("list_inventory", {"config_entry_id": "device"}), {"inventory": {}})
        await self.registry.call("acknowledge_refill", {"config_entry_id": "device"})
        self.coordinator.async_acknowledge_refill.assert_awaited_once_with()
        self.coordinator.inventory_manager = None
        with self.assertRaises(ServiceError):
            await self.registry.call("list_inventory", {"config_entry_id": "device"})

    async def test_inventory_rejects_invalid_action_and_mass(self):
        for fields in ({"action": "unknown"}, {"action": "record_weight", "gross_g": True}, {"action": "record_weight", "gross_g": float("nan")}, {"action": "add_scent", "raw_packet": "01"}):
            with self.assertRaises(vol.Invalid):
                await self.registry.call("manage_inventory", {"config_entry_id": "device", **fields})
        self.coordinator.async_inventory_action.assert_not_awaited()

    async def test_routes_full_schedule_without_user_unknown_bytes(self):
        await self.registry.call("set_schedule", {
            "config_entry_id": "device", "slot": 2, "start": "06:30", "end": "20:45",
            "days_mask": 62, "enabled": True, "intensity": 3})
        self.coordinator.async_set_schedule.assert_awaited_once_with(
            slot=2, allow_append=True, enabled=True, intensity=3, day_bits=62,
            start_hour=6, start_minute=30, end_hour=20, end_minute=45)
        with self.assertRaises(vol.Invalid):
            await self.registry.call("set_schedule", {
                "config_entry_id": "device", "slot": 1, "unknown": "00000000"})

    async def test_partial_edit_only_passes_named_fields(self):
        await self.registry.call("set_schedule", {
            "config_entry_id": "device", "slot": 1, "enabled": False})
        self.coordinator.async_set_schedule.assert_awaited_once_with(
            slot=1, allow_append=True, enabled=False)

    async def test_deletion_routes_one_based_slot(self):
        await self.registry.call("delete_schedule", {"config_entry_id": "device", "slot": 5})
        self.coordinator.async_delete_schedule.assert_awaited_once_with(5)

    async def test_checkpoint_routes_normalized_grams(self):
        await self.registry.call("record_bottle_weight", {
            "config_entry_id": "device", "gross_g": "125.25", "empty_g": 70})
        self.coordinator.async_record_bottle_weight.assert_awaited_once_with(125.25, 70.0)

    async def test_rejects_missing_wrong_domain_or_unloaded_entry(self):
        for case in ("missing", "wrong-domain", "unloaded"):
            with self.subTest(case=case):
                self.entry.domain = "other" if case == "wrong-domain" else DOMAIN
                self.hass.data = {} if case == "unloaded" else {DOMAIN: {"device": self.coordinator}}
                with self.assertRaises(ServiceError):
                    await self.registry.call("delete_schedule", {
                        "config_entry_id": "absent" if case == "missing" else "device", "slot": 1})
        self.coordinator.async_delete_schedule.assert_not_awaited()

    async def test_slot_mask_and_intensity_are_strict_integers(self):
        for field, values in {
            "slot": [True, 1.2, "1", 0, 6],
            "days_mask": [False, 1.2, "1", -1, 128],
            "intensity": [True, 2.2, "2", 0, 5],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(vol.Invalid):
                        await self.registry.call("set_schedule", {
                            "config_entry_id": "device", "slot": 1, field: value})
        self.coordinator.async_set_schedule.assert_not_awaited()

    async def test_clocks_require_exact_minute_format(self):
        for value in ("6:30", "24:00", "12:60", "06:30:00", "06:30 ", 630, None):
            with self.subTest(value=value):
                with self.assertRaises(vol.Invalid):
                    await self.registry.call("set_schedule", {
                        "config_entry_id": "device", "slot": 1, "start": value})
        self.coordinator.async_set_schedule.assert_not_awaited()

    async def test_rejects_nonfinite_negative_and_oversized_weights(self):
        for value in (True, None, float("nan"), float("inf"), "-inf", -1, 10001, "bad"):
            with self.subTest(value=value):
                with self.assertRaises(vol.Invalid):
                    await self.registry.call("record_bottle_weight", {
                        "config_entry_id": "device", "gross_g": value, "empty_g": 70})
        self.coordinator.async_record_bottle_weight.assert_not_awaited()

    async def test_rejects_impossible_gross_tare_pair(self):
        for gross, empty in ((0, 0), (60, 70)):
            with self.subTest(gross=gross, empty=empty):
                with self.assertRaises(ServiceError):
                    await self.registry.call("record_bottle_weight", {
                        "config_entry_id": "device", "gross_g": gross, "empty_g": empty})
        self.coordinator.async_record_bottle_weight.assert_not_awaited()

    async def test_enormous_integer_is_schema_error_not_overflow(self):
        with self.assertRaises(vol.Invalid):
            await self.registry.call("record_bottle_weight", {
                "config_entry_id": "device", "gross_g": 10 ** 10000, "empty_g": 70})
        self.coordinator.async_record_bottle_weight.assert_not_awaited()

    async def test_rejects_backdated_checkpoint_field(self):
        with self.assertRaises(vol.Invalid):
            await self.registry.call("record_bottle_weight", {
                "config_entry_id": "device", "gross_g": 125, "empty_g": 70,
                "timestamp": "2026-10-25T07:40:00-04:00"})
        self.coordinator.async_record_bottle_weight.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
