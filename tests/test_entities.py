"""HA-facing values and minute/bit edits, with isolated framework stubs."""
import asyncio
from datetime import datetime, time, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1] / "custom_components/aromatech_ambience"


def mod(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


class Entity:
    def __init__(self, coordinator, key):
        self.coordinator = coordinator
        self.key = key

    @property
    def available(self):
        return True


class ManagerView(types.SimpleNamespace):
    @property
    def owns_remaining(self):
        return self.inventory.initialized or getattr(self, "load_failed", False)


class EntityTests(unittest.TestCase):
    def setUp(self):
        package = "ambience_entity_test"
        modules = {
            package: mod(package, __path__=[str(ROOT)]),
            f"{package}.const": mod("const", DOMAIN="aromatech_ambience"),
            f"{package}.coordinator": mod("coordinator", AmbienceCoordinator=object),
            f"{package}.entity": mod("entity", AmbienceEntity=Entity),
            "homeassistant.components.sensor": mod("sensor", SensorEntity=type("Sensor", (), {}),
                SensorDeviceClass=types.SimpleNamespace(DURATION="duration", WEIGHT="weight"),
                SensorStateClass=types.SimpleNamespace(TOTAL_INCREASING="total_increasing", MEASUREMENT="measurement")),
            "homeassistant.components.time": mod("time", TimeEntity=type("Time", (), {})),
            "homeassistant.components.button": mod("button", ButtonEntity=type("Button", (), {})),
            "homeassistant.components.switch": mod("switch", SwitchEntity=type("Switch", (), {})),
            "homeassistant.config_entries": mod("config_entries", ConfigEntry=object),
            "homeassistant.core": mod("core", HomeAssistant=object),
            "homeassistant.util": mod("util", dt=types.SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc))),
            "homeassistant.helpers.entity": mod("entity", EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic")),
            "homeassistant.helpers.entity_platform": mod("entity_platform", AddEntitiesCallback=object),
            "homeassistant.exceptions": mod("exceptions", HomeAssistantError=ValueError),
        }
        self.registry = patch.dict(sys.modules, modules)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        for name in ("protocol", "inventory", "sensor", "time", "switch", "button"):
            spec = importlib.util.spec_from_file_location(f"{package}.{name}", ROOT / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = loaded
            spec.loader.exec_module(loaded)
            setattr(self, name, loaded)
        self.calls = []

        async def save(**kwargs):
            self.calls.append(kwargs)

        self.coordinator = types.SimpleNamespace(data=self.protocol.State(), async_set_schedule=save)

    def test_technical_sensors_are_optional_diagnostics_without_new_ids(self):
        optional = [self.sensor.AmbienceTiming(self.coordinator, "spray", 0),
                    self.sensor.AmbienceTiming(self.coordinator, "pause", 1),
                    *[self.sensor.AmbienceUsage(self.coordinator, key) for key in self.sensor.USAGE_SENSORS
                      if key != "estimated_remaining_g"],
                    *[self.sensor.AmbienceBottle(self.coordinator, key) for key in self.sensor.BOTTLE_DIAGNOSTICS],
                    self.sensor.AmbienceBluetooth(self.coordinator, "last_bluetooth_contact"),
                    self.sensor.AmbienceBluetooth(self.coordinator, "bluetooth_confirmation_age")]
        for entity in optional:
            with self.subTest(key=entity.key):
                self.assertEqual(entity._attr_entity_category, "diagnostic")
                self.assertFalse(entity._attr_entity_registry_enabled_default)
        primary = [self.sensor.AmbienceSchedule(self.coordinator),
                   self.sensor.AmbienceUsage(self.coordinator, "estimated_remaining_g"),
                   *[self.sensor.AmbienceBottle(self.coordinator, key)
                     for key in ("active_bottle", "active_scent", "remaining_percent", "days_remaining", "status")],
                   self.sensor.AmbienceBluetooth(self.coordinator, "bluetooth_health")]
        for entity in primary:
            self.assertTrue(getattr(entity, "_attr_entity_registry_enabled_default", True))
        self.assertEqual(optional[0].key, "schedule_spray_duration")
        self.assertEqual(primary[1].key, "estimated_remaining_g")
        self.assertEqual(primary[-1].key, "bluetooth_health")

    def test_schedule_attributes_keep_public_timings_without_protocol_bytes(self):
        self.coordinator.data.schedules = [self.protocol.Schedule(255, 8, 0, 20, 0, 2)]
        self.coordinator.data.firmware = "Test"
        attrs = self.sensor.AmbienceSchedule(self.coordinator).extra_state_attributes
        self.assertEqual(attrs["schedules"][0]["start"], "08:00")
        self.assertEqual(attrs["schedules"][0]["intensity"], 2)
        self.assertNotIn("unknown", attrs["schedules"][0])
        for key in ("raw_schedule", "light_rgb", "light_brightness", "firmware"):
            self.assertNotIn(key, attrs)

    def test_always_on_switch_available_without_opt_in_and_routes_both_states(self):
        calls = []
        async def preset(enabled):
            calls.append(enabled)
        coordinator = types.SimpleNamespace(
            experimental_schedule_writes=False, always_on=False,
            always_on_attributes={"experimental_enabled": False, "restore_available": False},
            async_set_always_on=preset)
        switch = self.switch.AmbienceAlwaysOn(coordinator)
        self.assertTrue(switch.available)
        self.assertFalse(switch.is_on)
        self.assertFalse(switch.extra_state_attributes["experimental_enabled"])
        asyncio.run(switch.async_turn_on())
        asyncio.run(switch.async_turn_off())
        self.assertEqual(calls, [True, False])

    def test_timed_controls_unavailable_only_while_always_on(self):
        self.coordinator.always_on = True
        entities = [self.switch.AmbienceDay(self.coordinator, 1),
                    self.switch.AmbienceScheduleEnabled(self.coordinator),
                    self.time.AmbienceScheduleTime(self.coordinator, "start"),
                    self.time.AmbienceScheduleTime(self.coordinator, "end")]
        self.assertTrue(all(not entity.available for entity in entities))
        self.assertTrue(self.switch.AmbiencePower(self.coordinator).available)
        self.coordinator.always_on = False
        self.assertTrue(all(entity.available for entity in entities))
        self.coordinator.timed_schedule_controls_locked = True
        self.assertTrue(all(not entity.available for entity in entities))
        self.assertTrue(self.switch.AmbiencePower(self.coordinator).available)

    def test_old_button_registry_cleanup_is_owned_entry_only(self):
        removed, added = [], []
        registry = types.SimpleNamespace(
            async_get_entity_id=lambda *args: "button.legacy_full_day",
            async_get=lambda entity_id: types.SimpleNamespace(config_entry_id="entry1"),
            async_remove=removed.append)
        helpers = mod("homeassistant.helpers", entity_registry=types.SimpleNamespace(async_get=lambda hass: registry))
        hass = types.SimpleNamespace(data={"aromatech_ambience": {"entry1": types.SimpleNamespace(address="test-address")}})
        entry = types.SimpleNamespace(entry_id="entry1")
        with patch.dict(sys.modules, {"homeassistant.helpers": helpers}):
            asyncio.run(self.button.async_setup_entry(hass, entry, added.extend))
            self.assertEqual(removed, ["button.legacy_full_day"])
            self.assertEqual(added, [])
            registry.async_get = lambda entity_id: types.SimpleNamespace(config_entry_id="different-entry")
            asyncio.run(self.button.async_setup_entry(hass, entry, added.extend))
            self.assertEqual(len(removed), 1)

    def test_minutes_roundtrip_and_unique_id_preserved(self):
        self.coordinator.data.start_hour = 8
        self.coordinator.data.start_minute = 37
        entity = self.time.AmbienceScheduleTime(self.coordinator, "start")
        self.assertEqual(entity.key, "schedule_start")
        self.assertEqual(entity.native_value, time(8, 37))
        asyncio.run(entity.async_set_value(time(9, 42)))
        self.assertEqual(self.calls, [{"start_hour": 9, "start_minute": 42}])

    def test_day_and_enable_send_intent_without_cached_mask(self):
        self.coordinator.data.days_mask = 0xff
        asyncio.run(self.switch.AmbienceDay(self.coordinator, 1).async_turn_off())
        asyncio.run(self.switch.AmbienceScheduleEnabled(self.coordinator).async_turn_off())
        self.assertEqual(self.calls, [{"day_bit": 1, "day_on": False}, {"enabled": False}])

    def test_schedule_distinguishes_unknown_empty_and_multiple(self):
        entity = self.sensor.AmbienceSchedule(self.coordinator)
        self.assertIsNone(entity.native_value)
        self.coordinator.data.schedules = []
        self.assertEqual(entity.native_value, "no schedules")
        self.coordinator.data.schedules = [self.protocol.Schedule(255, 8, 37, 9, 42, 2)] * 2
        self.assertEqual(entity.native_value, "2 schedules")
        attrs = entity.extra_state_attributes
        self.assertEqual(attrs["schedule_count"], 2)
        self.assertEqual(attrs["schedules"][1]["start"], "08:37")

    def test_usage_unknown_mass_and_restored_totals(self):
        self.coordinator.estimates = types.SimpleNamespace(coverage=.75, expected_sprays=12.5,
            estimated_remaining_g=None, calibration_status="restart_requires_weight",
            unknown_seconds_since_checkpoint=None, last_contact_at=None, command_status="idle",
            adjustment_cycles=0, adjustment_active_seconds=0, active_seconds=0, adjustments=[])
        coverage = self.sensor.AmbienceUsage(self.coordinator, "coverage")
        self.assertEqual(coverage.native_value, 75)
        self.assertTrue(coverage.available)
        self.assertTrue(coverage.extra_state_attributes["estimated"])
        self.assertIsNone(self.sensor.AmbienceUsage(self.coordinator, "estimated_remaining_g").native_value)
        self.assertEqual(self.sensor.AmbienceUsage(self.coordinator, "expected_sprays").native_value, 12.5)

    def test_existing_oil_key_uses_legacy_until_first_install_then_inventory(self):
        inventory = self.inventory.Inventory()
        now = datetime(2026, 10, 28, tzinfo=timezone.utc)
        self.coordinator.inventory_manager = ManagerView(inventory=inventory, summary=inventory.summary)
        self.coordinator.estimates = types.SimpleNamespace(estimated_remaining_g=42, expected_sprays=1000)
        oil = self.sensor.AmbienceUsage(self.coordinator, "estimated_remaining_g")
        self.assertEqual(oil.key, "estimated_remaining_g")
        self.assertEqual(oil.native_value, 42)
        scent = inventory.execute("add_scent", now, name="Test", grams_per_spray=1, spray_seconds=10)
        a = inventory.execute("add_bottle", now, name="A", empty_g=20, scent_id=scent, oil_g=100)
        b = inventory.execute("add_bottle", now, name="B", empty_g=20, scent_id=scent, oil_g=50)
        self.assertEqual(oil.native_value, 42)
        inventory.execute("install_bottle", now, bottle_id=a)
        self.assertEqual(oil.native_value, 100)
        self.assertEqual(oil.extra_state_attributes["fill_id"], inventory.bottles[a]["fills"][-1]["id"])
        self.assertNotIn("calibration_status", oil.extra_state_attributes)
        inventory.apply_usage(2, 20, 0)
        fill_count = self.sensor.AmbienceBottle(self.coordinator, "fill_sprays")
        lifetime = self.sensor.AmbienceUsage(self.coordinator, "expected_sprays")
        self.assertEqual(fill_count.native_value, 2)
        self.assertEqual(lifetime.native_value, 1000)
        inventory.execute("install_bottle", now, bottle_id=b)
        self.assertEqual(fill_count.native_value, 0)
        self.assertEqual(lifetime.native_value, 1000)
        self.assertEqual(oil.native_value, 50)
        inventory.execute("store_bottle", now)
        self.assertIsNone(oil.native_value)  # Never falls back to stale alpha mass.

    def test_bottle_sensor_exposes_uncertainty_without_history_recorder_payload(self):
        inventory = self.inventory.Inventory()
        now = datetime(2026, 10, 28, tzinfo=timezone.utc)
        scent = inventory.execute("add_scent", now, name="Test", grams_per_spray=1, spray_seconds=10)
        bottle = inventory.execute("add_bottle", now, name="A", empty_g=20, scent_id=scent, oil_g=100)
        inventory.execute("install_bottle", now, bottle_id=bottle)
        inventory.apply_usage(0, 0, 10)
        self.coordinator.inventory_manager = ManagerView(inventory=inventory, summary=inventory.summary)
        sensor = self.sensor.AmbienceBottle(self.coordinator, "status")
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["confidence"], "estimated_with_uncertainty")
        self.assertEqual(attrs["remaining_lower_g"], 99)
        self.assertEqual(attrs["remaining_upper_g"], 100)
        self.assertEqual(attrs["bottle_id"], bottle)
        self.assertNotIn("measurements", attrs)
        self.assertNotIn("fills", attrs)
        self.assertEqual(attrs["mass_basis"], "initial_estimate")
        for key in ("last_adjustment_at", "last_mass_correction_g", "last_adjustment_reason"):
            self.assertNotIn(key, attrs)
        technical = self.sensor.AmbienceBottle(self.coordinator, "fill_consumed_g")
        self.assertIn("last_adjustment_at", technical.extra_state_attributes)

    def test_corrupt_inventory_never_reveals_legacy_mass(self):
        self.coordinator.estimates = types.SimpleNamespace(estimated_remaining_g=42)
        self.coordinator.inventory_manager = ManagerView(
            inventory=self.inventory.Inventory(), load_failed=True,
            summary=lambda: {"remaining_g": None, "fill_id": None, "confidence": "storage_error"})
        oil = self.sensor.AmbienceUsage(self.coordinator, "estimated_remaining_g")
        self.assertIsNone(oil.native_value)
        self.assertEqual(oil.extra_state_attributes["confidence"], "storage_error")
