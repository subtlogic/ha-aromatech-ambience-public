"""Config-entry lifecycle wiring with isolated Home Assistant module stubs."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch, call


ROOT = Path(__file__).parents[1] / "custom_components/aromatech_ambience"
DOMAIN = "aromatech_ambience"


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.coordinator = types.SimpleNamespace(
            async_initialize_storage=AsyncMock(side_effect=lambda entry_id: self.events.append("storage")),
            async_initialize_schedule_mode=AsyncMock(side_effect=lambda entry_id: self.events.append("schedule_mode")),
            async_refresh=AsyncMock(side_effect=lambda: self.events.append("refresh")),
            async_close=AsyncMock(), async_start_timers=Mock(), options={}, async_apply_inventory_options=AsyncMock(),
        )
        self.coordinator_factory = Mock(return_value=self.coordinator)
        self.setup_services = Mock()
        self.schema = object()
        self.config_entry_schema = Mock(return_value=self.schema)
        self.store = types.SimpleNamespace(async_remove=AsyncMock())
        self.store_factory = Mock(return_value=self.store)
        self.inventory_store = types.SimpleNamespace(async_remove=AsyncMock())
        self.inventory_factory = Mock(return_value=self.inventory_store)
        self.dismiss = Mock()
        package = "ambience_lifecycle_test"
        stubs = {
            "homeassistant": module("homeassistant"),
            "homeassistant.components": module("homeassistant.components", persistent_notification=types.SimpleNamespace(async_dismiss=self.dismiss)),
            package + ".inventory_store": module(package + ".inventory_store", InventoryStore=self.inventory_factory),
            "homeassistant.config_entries": module("homeassistant.config_entries", ConfigEntry=object),
            "homeassistant.const": module("homeassistant.const", CONF_ADDRESS="address",
                Platform=types.SimpleNamespace(BUTTON="button", LIGHT="light", NUMBER="number", SENSOR="sensor",
                                               SWITCH="switch", TIME="time")),
            "homeassistant.core": module("homeassistant.core", HomeAssistant=object),
            "homeassistant.helpers": module("homeassistant.helpers"),
            "homeassistant.helpers.config_validation": module(
                "homeassistant.helpers.config_validation",
                config_entry_only_config_schema=self.config_entry_schema),
            "homeassistant.helpers.storage": module("homeassistant.helpers.storage", Store=self.store_factory),
            package + ".coordinator": module(package + ".coordinator",
                                               AmbienceCoordinator=self.coordinator_factory),
            package + ".services": module(package + ".services", async_setup_services=self.setup_services),
        }
        spec = importlib.util.spec_from_file_location(
            package, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        self.integration = importlib.util.module_from_spec(spec)
        stubs[package] = self.integration
        # Keep lazy imports isolated through the test, then restore the original
        # global modules even if an assertion or async handler raises.
        self.module_patch = patch.dict(sys.modules, stubs)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        spec.loader.exec_module(self.integration)

        self.hass = types.SimpleNamespace(
            data={}, config=types.SimpleNamespace(time_zone="America/Toronto"),
            config_entries=types.SimpleNamespace(
                async_forward_entry_setups=AsyncMock(side_effect=self.forward),
                async_unload_platforms=AsyncMock(return_value=True), async_reload=AsyncMock()))
        self.unsubscribe = object()
        self.entry = types.SimpleNamespace(
            entry_id="entry-1", data={"address": "test-address"}, options={},
            add_update_listener=Mock(return_value=self.unsubscribe), async_on_unload=Mock())

    async def forward(self, entry, platforms):
        self.assertIs(self.hass.data[DOMAIN][entry.entry_id], self.coordinator)
        self.events.append("forward")

    async def test_declares_config_entry_only_schema(self):
        self.config_entry_schema.assert_called_once_with(DOMAIN)
        self.assertIs(self.integration.CONFIG_SCHEMA, self.schema)

    async def test_domain_setup_registers_services_without_devices(self):
        self.assertTrue(await self.integration.async_setup(self.hass, {}))
        self.setup_services.assert_called_once_with(self.hass)
        self.coordinator_factory.assert_not_called()

    async def test_setup_restores_before_refresh_and_forwards_after_publish(self):
        self.entry.options = {"scan_interval": 45, "experimental_schedule_writes": True}
        self.hass.data = {DOMAIN: {"another-entry": object()}}
        other = self.hass.data[DOMAIN]["another-entry"]
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.coordinator_factory.assert_called_once_with(
            self.hass, "test-address", 45, time_zone="America/Toronto",
            experimental_schedule_writes=True, options=dict(self.entry.options))
        self.assertEqual(self.events, ["storage", "schedule_mode", "refresh", "forward"])
        self.coordinator.async_initialize_storage.assert_awaited_once_with("entry-1")
        self.assertIs(self.hass.data[DOMAIN]["another-entry"], other)
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            self.entry, self.integration.PLATFORMS)
        self.entry.add_update_listener.assert_called_once_with(self.integration._async_reload)
        self.entry.async_on_unload.assert_called_once_with(self.unsubscribe)

    async def test_setup_defaults_keep_experimental_writes_disabled(self):
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.coordinator_factory.assert_called_once_with(
            self.hass, "test-address", 300, time_zone="America/Toronto",
            experimental_schedule_writes=False, options={})

    async def test_initial_ble_unavailability_does_not_skip_entity_setup(self):
        self.coordinator.last_update_success = False
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        self.assertIs(self.hass.data[DOMAIN]["entry-1"], self.coordinator)

    async def test_options_listener_reloads_matching_entry(self):
        self.hass.data = {DOMAIN: {"entry-1": self.coordinator}}
        self.entry.options = {"scan_interval": 60}
        await self.integration._async_reload(self.hass, self.entry)
        self.hass.config_entries.async_reload.assert_awaited_once_with("entry-1")

    async def test_threshold_options_apply_live_without_reload(self):
        self.hass.data = {DOMAIN: {"entry-1": self.coordinator}}
        self.entry.options = {"low_oil_g": 9, "refill_notifications": True}
        await self.integration._async_reload(self.hass, self.entry)
        self.coordinator.async_apply_inventory_options.assert_awaited_once_with(self.entry.options)
        self.hass.config_entries.async_reload.assert_not_awaited()

    async def test_unloaded_options_listener_is_noop(self):
        await self.integration._async_reload(self.hass, self.entry)
        self.hass.config_entries.async_reload.assert_not_awaited()
        self.coordinator.async_apply_inventory_options.assert_not_awaited()

    async def test_failed_platform_unload_keeps_coordinator_open(self):
        self.hass.data = {DOMAIN: {"entry-1": self.coordinator}}
        self.hass.config_entries.async_unload_platforms.return_value = False
        self.assertFalse(await self.integration.async_unload_entry(self.hass, self.entry))
        self.assertIs(self.hass.data[DOMAIN]["entry-1"], self.coordinator)
        self.coordinator.async_close.assert_not_awaited()

    async def test_successful_unload_closes_only_target_entry(self):
        other = object()
        self.hass.data = {DOMAIN: {"entry-1": self.coordinator, "another-entry": other}}
        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            self.entry, self.integration.PLATFORMS)
        self.coordinator.async_close.assert_awaited_once_with()
        self.assertNotIn("entry-1", self.hass.data[DOMAIN])
        self.assertIs(self.hass.data[DOMAIN]["another-entry"], other)

    async def test_removal_deletes_versioned_per_entry_store(self):
        await self.integration.async_remove_entry(self.hass, self.entry)
        self.store_factory.assert_called_once_with(self.hass, 1, "aromatech_ambience.entry-1")
        self.store.async_remove.assert_awaited_once_with()
        self.inventory_factory.assert_has_calls([call(self.hass, "entry-1"), call(self.hass, "entry-1", suffix="schedule_mode")])
        self.assertEqual(self.inventory_store.async_remove.await_count, 2)
        self.dismiss.assert_called_once_with(self.hass, "aromatech_ambience_refill_entry-1")


if __name__ == "__main__":
    unittest.main()
