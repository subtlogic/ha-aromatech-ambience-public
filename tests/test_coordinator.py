"""Schedule edits require complete list readback and preserve untouched slots."""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch, AsyncMock, Mock


ROOT = Path(__file__).parents[1] / "custom_components/aromatech_ambience"


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    sys.modules[name] = result
    return result


module("bleak", BleakClient=type("BleakClient", (), {}))
module("bleak_retry_connector", establish_connection=None)
module("homeassistant")
module("homeassistant.components", bluetooth=None)
module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
module("homeassistant.exceptions", HomeAssistantError=type("HomeAssistantError", (Exception,), {}))
module("homeassistant.helpers")


class DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls


module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=DataUpdateCoordinator,
       UpdateFailed=type("UpdateFailed", (Exception,), {}))
module("homeassistant.util", dt=types.SimpleNamespace(now=datetime.now, utcnow=lambda: datetime.now(timezone.utc)))
module("aromatech_ambience", __path__=[str(ROOT)])
module("aromatech_ambience.const", DISCONNECT_DELAY=20, REPLY_TIMEOUT=5,
       SCAN_INTERVAL_SECONDS=300)

spec = importlib.util.spec_from_file_location("aromatech_ambience.protocol", ROOT / "protocol.py")
protocol = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = protocol
spec.loader.exec_module(protocol)
spec = importlib.util.spec_from_file_location("aromatech_ambience.coordinator", ROOT / "coordinator.py")
coordinator_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = coordinator_module
spec.loader.exec_module(coordinator_module)
coordinator_module.RETRY_BACKOFF = 0


def complete_state(slots):
    raw = bytes((1, 1, 0, len(slots), 1)) + b"".join(s.to_bytes() for s in slots)
    body = b"\0\1\x03\xe9\xaf\0" + bytes((len(raw),)) + raw
    return protocol.parse_batch(body)


FIRST = protocol.Schedule(255, 8, 0, 18, 0, 2)
SECOND = protocol.Schedule(255, 19, 37, 21, 45, 3, b"\x25\x01\xff\x42")


class FakeInventoryManager:
    """Storage tests isolate the lazily imported inventory persistence layer."""

    def __init__(self, hass, entry_id, options):
        self.args = hass, entry_id, options
        self.initialized = False
        self.add_usage = Mock()
        self.add_adjustment = Mock()
        self.accounting_changed = Mock()
        self.flush = AsyncMock()

    async def initialize(self):
        self.initialized = True


INVENTORY_MANAGER_STUB = types.ModuleType("aromatech_ambience.inventory_manager")
INVENTORY_MANAGER_STUB.InventoryManager = FakeInventoryManager


def fake_coordinator(outcomes=(), *, slots=None, experimental=False, refresh_success=()):
    coordinator = coordinator_module.AmbienceCoordinator.__new__(coordinator_module.AmbienceCoordinator)
    coordinator.data = complete_state([FIRST] if slots is None else slots)
    coordinator.experimental_schedule_writes = experimental
    coordinator._schedule_edit_lock = asyncio.Lock()
    coordinator._lock = asyncio.Lock()
    coordinator.last_update_success = True
    coordinator.options = {}
    coordinator.inventory_manager = None
    coordinator.listener_updates = 0
    def notify():
        coordinator.listener_updates += 1
    coordinator.async_update_listeners = notify
    coordinator.commands = []
    coordinator.direct_polls = 0
    coordinator.debounced_requests = 0
    replies = iter(outcomes)
    refreshes = iter(refresh_success)
    coordinator.async_set_updated_data = lambda value: setattr(coordinator, "data", value)

    async def exchange(_frames=None, *, build=None):
        frames = build(coordinator.data)
        coordinator.commands.append(frames)
        if next(replies, False):
            raw = b"".join(frame[2:] for frame in frames)[7:]
            coordinator.data = complete_state(protocol.parse_schedules(raw))
        return protocol.State(power=True, schedule_status="partial")

    async def refresh():
        coordinator.direct_polls += 1
        coordinator.last_update_success = next(refreshes, True)

    async def debounced_refresh():
        coordinator.debounced_requests += 1

    coordinator._exchange = exchange
    coordinator.async_refresh = refresh
    coordinator.async_request_refresh = debounced_refresh
    return coordinator


class ScheduleReadbackTest(unittest.TestCase):
    def test_light_off_reply_updates_mode_before_bare_on(self):
        coordinator = coordinator_module.AmbienceCoordinator.__new__(coordinator_module.AmbienceCoordinator)
        coordinator.data = protocol.State(light_mode=4, rgb=(255, 137, 14), brightness=30)

        async def exchange(_frames, *, build=None, affects_usage=True):
            return protocol.State(light_mode=0, rgb=(255, 137, 14), brightness=30)

        async def unexpected_refresh():
            self.fail("a state-carrying light reply should update immediately")

        coordinator._exchange = exchange
        coordinator.async_request_refresh = unexpected_refresh
        coordinator.async_set_updated_data = lambda value: setattr(coordinator, "data", value)

        asyncio.run(coordinator._command([b"light off"]))

        self.assertEqual(coordinator.data.light_mode, 0)

    def test_retries_ignored_write_then_confirms_value(self):
        coordinator = fake_coordinator([False, True])

        asyncio.run(coordinator.async_set_schedule(intensity=3))

        self.assertEqual(coordinator.data.intensity, 3)
        self.assertEqual(len(coordinator.commands), 2)
        self.assertEqual(coordinator.commands[0][1], bytes.fromhex("25 02 00 00 00 00"))

    def test_third_attempt_can_confirm_value(self):
        coordinator = fake_coordinator([False, False, True])

        asyncio.run(coordinator.async_set_schedule(intensity=3))

        self.assertEqual(coordinator.data.intensity, 3)
        self.assertEqual(len(coordinator.commands), 3)

    def test_raises_when_all_device_reports_keep_old_value(self):
        coordinator = fake_coordinator([False, False, False])

        with self.assertRaisesRegex(Exception, "did not retain schedule change"):
            asyncio.run(coordinator.async_set_schedule(intensity=3))

        self.assertEqual(coordinator.data.intensity, 2)
        self.assertEqual(len(coordinator.commands), 3)

    def test_experimental_gate_blocks_multi_slot_append_and_delete_before_write(self):
        for operation in ("multi", "append", "delete"):
            coordinator = fake_coordinator(slots=[FIRST, SECOND] if operation == "multi" else [FIRST])
            async def edit():
                if operation == "delete":
                    await coordinator.async_delete_schedule(1)
                elif operation == "append":
                    await coordinator.async_set_schedule(slot=2, allow_append=True, enabled=True, day_bits=127,
                                                        start_hour=19, start_minute=0, end_hour=20, end_minute=0, intensity=2)
                else:
                    await coordinator.async_set_schedule(slot=2, intensity=4)
            with self.subTest(operation=operation), self.assertRaisesRegex(Exception, "experimental"):
                asyncio.run(edit())
            self.assertEqual(coordinator.commands, [])

    def test_partial_ack_preserves_full_list_and_write_base(self):
        old = complete_state([FIRST, SECOND])
        ack = protocol.State(power=False, run_code=3, schedule_status="partial", raw={protocol.CMD_POWER: b"ack"})
        merged = coordinator_module._merge(old, ack)
        self.assertEqual(merged.schedules, [FIRST, SECOND])
        self.assertEqual(merged.schedule_raw, old.schedule_raw)
        self.assertEqual(merged.schedule_status, "complete")
        self.assertFalse(merged.power)
        self.assertEqual(merged.raw[protocol.CMD_POWER], b"ack")

    def test_empty_complete_list_clears_previous_scalar_aliases(self):
        merged = coordinator_module._merge(complete_state([FIRST]), complete_state([]))
        self.assertEqual(merged.schedules, [])
        self.assertEqual(merged.schedule_status, "complete")
        self.assertIsNone(merged.intensity)
        self.assertIsNone(merged.days_mask)
        self.assertTrue(coordinator_module._carries_state(merged))

    def test_indexed_edit_preserves_untouched_slot_and_unknown_bytes(self):
        coordinator = fake_coordinator([True], slots=[FIRST, SECOND], experimental=True)
        asyncio.run(coordinator.async_set_schedule(slot=2, intensity=4))
        self.assertEqual(coordinator.data.schedules[0], FIRST)
        updated = coordinator.data.schedules[1]
        self.assertEqual((updated.start_minute, updated.end_minute, updated.unknown), (37, 45, SECOND.unknown))
        self.assertEqual(updated.intensity, 4)

    def test_delete_retry_does_not_delete_next_slot(self):
        coordinator = fake_coordinator([True, True], slots=[FIRST, SECOND], experimental=True,
                                       refresh_success=[True, False, True])
        asyncio.run(coordinator.async_delete_schedule(1))
        self.assertEqual(coordinator.data.schedules, [SECOND])
        self.assertEqual(len(coordinator.commands), 2)
        self.assertEqual(coordinator.commands[0], coordinator.commands[1])

    def test_delete_last_slot_confirms_empty_list(self):
        coordinator = fake_coordinator([True], experimental=True)
        asyncio.run(coordinator.async_delete_schedule(1))
        self.assertEqual(coordinator.data.schedules, [])
        self.assertIsNone(coordinator.data.intensity)

    def test_append_requires_all_fields_and_next_index(self):
        coordinator = fake_coordinator([True], slots=[], experimental=True)
        with self.assertRaisesRegex(Exception, "requires days"):
            asyncio.run(coordinator.async_set_schedule(slot=1, allow_append=True, intensity=2))
        self.assertEqual(coordinator.commands, [])
        asyncio.run(coordinator.async_set_schedule(slot=1, allow_append=True, enabled=True, day_bits=127,
                                                  start_hour=6, start_minute=30, end_hour=8, end_minute=45, intensity=2))
        self.assertEqual(coordinator.data.schedules, [protocol.Schedule(255, 6, 30, 8, 45, 2)])
        with self.assertRaisesRegex(Exception, "next slot"):
            asyncio.run(coordinator.async_set_schedule(slot=3, allow_append=True, intensity=2))
        self.assertEqual(len(coordinator.commands), 1)

    def test_close_saves_estimates_even_when_disconnect_fails(self):
        coordinator = fake_coordinator()
        coordinator._lock = asyncio.Lock()
        coordinator._disconnect_task = None
        events = []

        async def disconnect():
            events.append("disconnect")
            raise RuntimeError("disconnect failed")

        async def save(value):
            events.append(("save", value))

        coordinator._client = types.SimpleNamespace(is_connected=True, disconnect=disconnect)
        coordinator._store = types.SimpleNamespace(async_save=save)
        coordinator.estimates = types.SimpleNamespace(to_dict=lambda: {"total": 42})
        coordinator._advance_estimates = lambda: events.append("advance")
        asyncio.run(coordinator.async_close())
        self.assertEqual(events, ["disconnect", "advance", ("save", {"total": 42})])
        self.assertIsNone(coordinator._client)

    def test_missing_or_partial_snapshot_cannot_supply_write_base(self):
        for state in (protocol.State(schedule_status="partial"), protocol.State(schedule_status="unparsed")):
            coordinator = fake_coordinator([True])
            coordinator.data = state
            with self.assertRaisesRegex(Exception, "complete parsed"):
                asyncio.run(coordinator.async_set_schedule(intensity=3))
            self.assertEqual(coordinator.commands, [])

    def test_failed_initial_refresh_blocks_write(self):
        coordinator = fake_coordinator([True], refresh_success=[False])
        with self.assertRaisesRegex(Exception, "fresh schedule report"):
            asyncio.run(coordinator.async_set_schedule(intensity=3))
        self.assertEqual(coordinator.commands, [])

    def test_schedule_action_uses_direct_polls_despite_noop_debounced_request(self):
        coordinator = fake_coordinator([True])
        asyncio.run(coordinator.async_set_schedule(intensity=3))
        self.assertEqual(coordinator.direct_polls, 2)
        self.assertEqual(coordinator.debounced_requests, 0)

    def test_weight_action_uses_direct_poll_timestamp(self):
        coordinator = fake_coordinator()
        coordinator._store = None
        coordinator._last_observation_at = None
        recorded = []
        stamp = datetime(2026, 10, 27, 20, tzinfo=timezone.utc)
        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
        coordinator.estimates.record_weight = lambda *args: recorded.append(args)
        original_refresh = coordinator.async_refresh

        async def complete_poll():
            await original_refresh()
            coordinator._last_observation_at = stamp

        coordinator.async_refresh = complete_poll
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=stamp):
            asyncio.run(coordinator.async_record_bottle_weight(200, 50))
        self.assertEqual(recorded, [(stamp, 200, 50)])
        self.assertEqual(coordinator.direct_polls, 1)
        self.assertEqual(coordinator.debounced_requests, 0)
        self.assertEqual(coordinator.listener_updates, 1)

    def test_day_bit_edit_preserves_other_bits_from_fresh_poll(self):
        coordinator = fake_coordinator([True, True], slots=[protocol.Schedule(0x81, 8, 0, 18, 0, 2)])
        original_refresh = coordinator.async_refresh

        async def fresh_report():
            if coordinator.direct_polls == 0:
                # Another app enabled Tuesday after our old Sunday-only cache.
                coordinator.data = complete_state([protocol.Schedule(0x85, 8, 0, 18, 0, 2)])
            await original_refresh()

        coordinator.async_refresh = fresh_report
        asyncio.run(coordinator.async_set_schedule(day_bit=1, day_on=True))
        self.assertEqual(coordinator.data.schedules[0].days_mask, 0x87)
        asyncio.run(coordinator.async_set_schedule(day_bit=0, day_on=False))
        self.assertEqual(coordinator.data.schedules[0].days_mask, 0x86)

    def test_unparsed_report_clears_scalar_schedule_aliases_and_write_base(self):
        old = complete_state([FIRST, SECOND])
        merged = coordinator_module._merge(old, protocol.State(schedule_status="unparsed"))
        self.assertEqual(merged.schedule_status, "unparsed")
        for field in ("schedules", "schedule_raw", "days_mask", "start_hour", "start_minute",
                      "end_hour", "end_minute", "intensity"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(merged, field))

    def test_new_level_five_is_rejected_but_existing_five_is_preserved(self):
        coordinator = fake_coordinator([True])
        with self.assertRaisesRegex(Exception, "1–4"):
            asyncio.run(coordinator.async_set_schedule(intensity=5))
        self.assertEqual(coordinator.commands, [])
        existing = protocol.Schedule(255, 8, 0, 18, 0, 5)
        coordinator = fake_coordinator([True], slots=[existing])
        asyncio.run(coordinator.async_set_schedule(start_minute=37))
        self.assertEqual(coordinator.data.schedules[0].intensity, 5)

    def test_overlap_and_overnight_rejected_but_touching_or_disjoint_days_allowed(self):
        for slot, accepted in (
            (protocol.Schedule(255, 17, 30, 20, 0, 2), False),
            (protocol.Schedule(255, 18, 0, 20, 0, 2), True),
            (protocol.Schedule(255, 22, 0, 6, 0, 2), False),
            (protocol.Schedule(0x82, 8, 30, 9, 0, 2), True),
            (protocol.Schedule(0x01, 8, 30, 9, 0, 2), True),
        ):
            first = protocol.Schedule(0x81, 8, 0, 18, 0, 2)
            coordinator = fake_coordinator([True], slots=[first, SECOND], experimental=True)
            async def edit():
                await coordinator.async_set_schedule(slot=2, days_mask=slot.days_mask, start_hour=slot.start_hour,
                                                    start_minute=slot.start_minute, end_hour=slot.end_hour, end_minute=slot.end_minute)
            with self.subTest(slot=slot):
                if accepted:
                    asyncio.run(edit())
                    self.assertEqual(len(coordinator.commands), 1)
                else:
                    with self.assertRaises(Exception):
                        asyncio.run(edit())
                    self.assertEqual(coordinator.commands, [])

    def test_notify_retains_continuation_after_partial_first_fragment(self):
        coordinator = fake_coordinator()
        coordinator._reassembler = protocol.Reassembler()
        coordinator._accumulated = None
        coordinator._reply = None
        coordinator.trace = deque(maxlen=24)
        raw = complete_state([FIRST]).schedule_raw
        body = b"\0\1\x03\xe9\xaf\0\x0f" + raw
        coordinator._on_notify(None, bytearray(b"\x07\x01" + body[:18]))
        coordinator._on_notify(None, bytearray(b"\x07\x02" + body[18:]))
        self.assertEqual(coordinator._accumulated.schedule_status, "complete")
        self.assertEqual(coordinator._accumulated.schedules, [FIRST])

    def test_intensity_only_edit_preserves_existing_ambiguous_windows(self):
        overnight = protocol.Schedule(255, 22, 0, 6, 0, 2)
        overlapping = protocol.Schedule(255, 10, 0, 20, 0, 2)
        for slots in ([overnight], [FIRST, overlapping]):
            with self.subTest(slots=slots):
                coordinator = fake_coordinator([True], slots=slots, experimental=True)
                asyncio.run(coordinator.async_set_schedule(slot=len(slots), intensity=4))
                updated = coordinator.data.schedules[-1]
                self.assertEqual((updated.start_hour, updated.end_hour), (slots[-1].start_hour, slots[-1].end_hour))
                self.assertEqual(updated.intensity, 4)

    def test_existing_overnight_can_be_disabled_but_not_reenabled(self):
        overnight = protocol.Schedule(255, 22, 0, 6, 0, 2)
        coordinator = fake_coordinator([True], slots=[overnight])
        asyncio.run(coordinator.async_set_schedule(enabled=False))
        self.assertEqual(coordinator.data.schedules[0].days_mask, 127)
        with self.assertRaisesRegex(Exception, "end after start"):
            asyncio.run(coordinator.async_set_schedule(enabled=True))
        self.assertEqual(len(coordinator.commands), 1)

    def test_changed_or_new_overnight_window_is_rejected_even_disabled(self):
        coordinator = fake_coordinator([True], slots=[protocol.Schedule(127, 8, 0, 18, 0, 2)])
        with self.assertRaisesRegex(Exception, "end after start"):
            asyncio.run(coordinator.async_set_schedule(start_hour=22, end_hour=6))
        self.assertEqual(coordinator.commands, [])
        coordinator = fake_coordinator([True], slots=[], experimental=True)
        with self.assertRaisesRegex(Exception, "end after start"):
            asyncio.run(coordinator.async_set_schedule(slot=1, allow_append=True, enabled=False, day_bits=127,
                                                      start_hour=22, start_minute=0, end_hour=6, end_minute=0, intensity=2))
        self.assertEqual(coordinator.commands, [])


class CoordinatorStorageTest(unittest.TestCase):
    def test_light_and_sound_do_not_discard_usage_but_power_does(self):
        async def run():
            coordinator = fake_coordinator()
            coordinator._exchange = types.MethodType(coordinator_module.AmbienceCoordinator._exchange, coordinator)
            coordinator._store = None
            coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
            start = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
            inputs = dict(power=True, schedules=[FIRST], presets=((5, 300), (10, 240)))
            coordinator.estimates.observe(start, **inputs)
            coordinator._last_observation_at = start
            async def connect():
                return object(), False
            async def write(client, frames):
                return protocol.State(light_mode=1, sound=False)
            coordinator._connect = connect
            coordinator._write_and_wait = write
            coordinator._schedule_disconnect = lambda: None
            for minutes in (5, 10):
                now = start + timedelta(minutes=minutes)
                with patch.object(coordinator_module.dt_util, "utcnow", return_value=now):
                    await coordinator.async_set_light(1)
                    await coordinator.async_set_sound(False)
                coordinator.estimates.observe(now, **inputs)
            self.assertAlmostEqual(coordinator.estimates.active_seconds, 600 / 250 * 10)
            self.assertEqual(coordinator.estimates.unobserved_seconds, 0)
            coordinator.data.intensity_presets = inputs["presets"]
            with patch.object(coordinator_module.dt_util, "utcnow", return_value=start + timedelta(minutes=11)):
                with self.assertRaisesRegex(Exception, "did not confirm"):
                    await coordinator.async_set_power(False)
            self.assertEqual(coordinator.estimates.command_status, "failed")
            self.assertAlmostEqual(coordinator.estimates.active_seconds, 660 / 250 * 10)
        asyncio.run(run())

    def test_direct_full_day_edits_require_opt_in_and_midnight_is_rejected(self):
        for experimental, hour in ((False, 8), (True, 0)):
            with self.subTest(experimental=experimental, hour=hour):
                coordinator = fake_coordinator([True], experimental=experimental)
                with self.assertRaisesRegex(Exception, "end after start"):
                    asyncio.run(coordinator.async_set_schedule(
                        start_hour=hour, start_minute=0, end_hour=hour, end_minute=0))
                self.assertEqual(coordinator.commands, [])

    def test_invalid_saved_totals_log_and_leave_fresh_estimator_usable(self):
        class Store:
            def __init__(self, *args):
                pass

            async def async_load(self):
                return {"bad": "saved totals"}

        storage = types.ModuleType("homeassistant.helpers.storage")
        storage.Store = Store
        coordinator = fake_coordinator()
        coordinator.hass = object()
        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
        fresh = coordinator.estimates.to_dict()
        with patch.dict(sys.modules, {"homeassistant.helpers.storage": storage,
                                     "aromatech_ambience.inventory_manager": INVENTORY_MANAGER_STUB}), self.assertLogs(coordinator_module._LOGGER, level="WARNING"):
            asyncio.run(coordinator.async_initialize_storage("invalid-entry"))
        self.assertEqual(coordinator.estimates.expected_sprays, fresh["expected_sprays"])
        self.assertEqual(coordinator.estimates.active_seconds, 0)

    def test_estimator_rejected_observation_cannot_authorize_weight_checkpoint(self):
        coordinator = fake_coordinator()
        coordinator._store = None
        coordinator._last_observation_at = datetime.now(timezone.utc)
        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
        coordinator.estimates.observe = lambda *args, **kwargs: None
        state = complete_state([FIRST])
        state.intensity_presets = ((60, 120),)
        state.raw[protocol.CMD_INTENSITY_PRESETS] = bytes.fromhex("01003c0078")
        coordinator._record_observation(state)
        self.assertIsNone(coordinator._last_observation_at)

    def test_restore_uses_versioned_per_entry_storage(self):
        saved = {"total_active_seconds": 42}
        constructions = []
        restored = []

        class Store:
            def __init__(self, hass, version, key):
                constructions.append((hass, version, key))

            async def async_load(self):
                return saved

        storage = types.ModuleType("homeassistant.helpers.storage")
        storage.Store = Store
        with patch.dict(sys.modules, {"homeassistant.helpers.storage": storage,
                                     "aromatech_ambience.inventory_manager": INVENTORY_MANAGER_STUB}):
            for entry_id in ("first-entry", "second-entry"):
                coordinator = fake_coordinator()
                coordinator.hass = "hass-instance"
                coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
                coordinator.estimates.restore = restored.append
                asyncio.run(coordinator.async_initialize_storage(entry_id))
                self.assertIsInstance(coordinator._store, Store)
                self.assertTrue(coordinator.inventory_manager.initialized)
                self.assertEqual(coordinator.inventory_manager.args, ("hass-instance", entry_id, {}))
        self.assertEqual(constructions, [("hass-instance", 1, "aromatech_ambience.first-entry"),
                                        ("hass-instance", 1, "aromatech_ambience.second-entry")])
        self.assertEqual(restored, [saved, saved])

    def test_delayed_save_uses_latest_totals_after_thirty_seconds(self):
        coordinator = fake_coordinator()
        totals = {"total": 1}
        pending = []
        coordinator.estimates = types.SimpleNamespace(to_dict=lambda: dict(totals))
        coordinator._store = types.SimpleNamespace(async_delay_save=lambda callback, delay: pending.append((callback, delay)))
        coordinator._save_estimates()
        totals["total"] = 2
        self.assertEqual(pending[0][1], 30)
        self.assertEqual(pending[0][0](), {"total": 2})

    def test_poll_observation_uses_received_fields_before_merging_cached_presets(self):
        coordinator = fake_coordinator()
        presets = ((60, 120), (120, 300))
        coordinator.data.intensity_presets = presets
        coordinator.data.raw[protocol.CMD_INTENSITY_PRESETS] = b"old presets"
        observations = []
        def observe(stamp, **values):
            observations.append((stamp, values))
            coordinator.estimates.last_observation_at = stamp if values["complete"] else None

        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
        coordinator.estimates.observe = observe
        coordinator._store = None
        coordinator._last_observation_at = datetime.now(timezone.utc)
        received = complete_state([FIRST])

        async def exchange(*args, **kwargs):
            return received

        coordinator._exchange = exchange
        with self.assertRaisesRegex(coordinator_module.UpdateFailed, "complete operating assumptions"):
            asyncio.run(coordinator._async_update_data())
        self.assertEqual(coordinator.data.intensity_presets, presets)
        self.assertEqual(observations, [])  # Partial reports do not confirm cached values.
        received.intensity_presets = presets
        # A populated field alone is insufficient without its received raw message.
        coordinator._record_observation(received)
        self.assertEqual(observations, [])
        received.raw[protocol.CMD_INTENSITY_PRESETS] = bytes.fromhex("02003c00780078012c")
        coordinator._record_observation(received)
        self.assertTrue(observations[-1][1]["complete"])
        self.assertEqual(observations[-1][1]["presets"], presets)
        self.assertEqual(coordinator._last_observation_at, observations[-1][0])

    def test_non_usage_command_preserves_anchor_before_write(self):
        coordinator = fake_coordinator()
        # Exercise the real exchange; the fake fixture ordinarily replaces it.
        coordinator._exchange = types.MethodType(coordinator_module.AmbienceCoordinator._exchange, coordinator)
        coordinator._lock = asyncio.Lock()
        coordinator._store = None
        coordinator._last_observation_at = datetime.now(timezone.utc)
        events = []
        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)

        async def connect():
            return object(), False

        async def write(client, frames):
            self.assertIsNotNone(coordinator._last_observation_at)
            events.append("write")
            return protocol.State(sound=True)

        coordinator._connect = connect
        coordinator._write_and_wait = write
        coordinator._schedule_disconnect = lambda: events.append("disconnect scheduled")
        asyncio.run(coordinator._exchange(protocol.sound(True)))
        self.assertEqual(events, ["write", "disconnect scheduled"])


class InventoryBoundaryTest(unittest.TestCase):
    def test_power_on_outside_window_widens_bound_without_counting_sprays(self):
        spec = importlib.util.spec_from_file_location("outside_window_inventory", ROOT / "inventory.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        inventory = loaded.Inventory()
        start = datetime(2026, 10, 28, 22, tzinfo=timezone.utc)
        scent = inventory.execute("add_scent", start, name="Test", grams_per_spray=1, spray_seconds=10)
        bottle = inventory.execute("add_bottle", start, name="A", scent_id=scent, empty_g=20, oil_g=100)
        inventory.execute("install_bottle", start, bottle_id=bottle)
        coordinator = fake_coordinator()
        coordinator._store = None
        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
        coordinator.inventory_manager = types.SimpleNamespace(add_usage=inventory.apply_usage, state=None, accounting_changed=Mock())
        state = complete_state([FIRST])
        state.intensity_presets = ((5, 300), (10, 240))
        state.raw[protocol.CMD_INTENSITY_PRESETS] = b"fresh"
        for stamp in (start, start + timedelta(seconds=60)):
            with patch.object(coordinator_module.dt_util, "utcnow", return_value=stamp):
                coordinator._record_observation(state)
        summary = inventory.summary(state.schedules, state.intensity_presets)
        self.assertEqual(summary["fill_sprays"], 0)
        self.assertEqual(summary["remaining_upper_g"], 100)
        self.assertEqual(summary["remaining_lower_g"], 100)
        self.assertEqual(summary["confidence"], "scent_default_provisional")

    def test_swap_transaction_holds_ble_lock_until_saved(self):
        async def scenario():
            coordinator = fake_coordinator()
            coordinator.data.power = False
            coordinator._last_observation_at = datetime.now(timezone.utc)
            entered, release = asyncio.Event(), asyncio.Event()
            async def execute(action, **fields):
                self.assertTrue(coordinator._lock.locked())
                entered.set()
                await release.wait()
            coordinator.inventory_manager = types.SimpleNamespace(execute=execute, flush=AsyncMock())
            task = asyncio.create_task(coordinator.async_inventory_action("install_bottle", bottle_id="a"))
            await entered.wait()
            acquired = False
            async def competing_command():
                nonlocal acquired
                async with coordinator._lock:
                    acquired = True
            contender = asyncio.create_task(competing_command())
            await asyncio.sleep(0)
            self.assertFalse(acquired)
            release.set()
            await task
            await contender
            self.assertTrue(acquired)
        asyncio.run(scenario())

    def test_observe_and_invalidate_capture_each_delta_once(self):
        coordinator = fake_coordinator()
        coordinator._store = None
        coordinator.estimates = coordinator_module.UsageEstimator("UTC", 605)
        deltas = []
        coordinator.inventory_manager = types.SimpleNamespace(add_usage=lambda *delta: deltas.append(delta), state=None, accounting_changed=Mock())
        state = complete_state([FIRST])
        state.intensity_presets = ((5, 300), (10, 50))
        state.raw[protocol.CMD_INTENSITY_PRESETS] = b"fresh presets"
        start = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=start):
            coordinator._record_observation(state)
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=start + timedelta(seconds=60)):
            coordinator._record_observation(state)
            coordinator._record_observation(state)  # Duplicate timestamp cannot add usage.
        with patch.object(coordinator_module.dt_util, "utcnow", return_value=start + timedelta(seconds=70)):
            coordinator._invalidate_estimates()
            coordinator._invalidate_estimates()
        totals = tuple(map(sum, zip(*deltas)))
        self.assertAlmostEqual(totals[0], 70 / 60)
        self.assertAlmostEqual(totals[1], 70 / 6)
        self.assertEqual(totals[2], 0)
        self.assertEqual(deltas[-1], (0, 0, 0, 0))
        self.assertIsNone(coordinator._last_observation_at)

    def test_swap_gate_uses_fresh_report_and_requires_power_off(self):
        for action in ("install_bottle", "store_bottle"):
            for fresh_success, fresh_power, complete in ((False, False, True), (True, True, True),
                                                          (True, False, False), (True, False, True)):
                with self.subTest(action=action, success=fresh_success, power=fresh_power, complete=complete):
                    coordinator = fake_coordinator()
                    coordinator.data.power = False  # Cached off cannot authorize the action.
                    coordinator._last_observation_at = datetime.now(timezone.utc)
                    calls = []
                    async def execute(name, **fields):
                        calls.append((name, fields))
                        return "bottle"
                    async def refresh():
                        coordinator.direct_polls += 1
                        coordinator.last_update_success = fresh_success
                        coordinator.data.power = fresh_power
                        coordinator._last_observation_at = datetime.now(timezone.utc) if complete else None
                    coordinator.async_refresh = refresh
                    coordinator.inventory_manager = types.SimpleNamespace(execute=execute, flush=AsyncMock())
                    if fresh_success and not fresh_power and complete:
                        result = asyncio.run(coordinator.async_inventory_action(action, bottle_id="bottle"))
                        self.assertEqual(result, "bottle")
                        self.assertEqual(calls, [(action, {"bottle_id": "bottle"})])
                        self.assertEqual(coordinator.listener_updates, 1)
                    else:
                        with self.assertRaises(coordinator_module.HomeAssistantError):
                            asyncio.run(coordinator.async_inventory_action(action, bottle_id="bottle"))
                        self.assertEqual(calls, [])
                        self.assertEqual(coordinator.listener_updates, 0)
                    self.assertEqual(coordinator.direct_polls, 1)

    def test_metadata_operations_do_not_poll_or_claim_bluetooth_success(self):
        coordinator = fake_coordinator()
        coordinator.last_update_success = False
        calls = []
        async def execute(action, **fields):
            calls.append((action, fields))
            return "record"
        async def forbidden():
            self.fail("Metadata operations must not poll Bluetooth")
        coordinator.async_refresh = forbidden
        coordinator.async_request_refresh = forbidden
        coordinator.async_set_updated_data = lambda _: self.fail("Metadata must not overwrite BLE success state")
        coordinator.inventory_manager = types.SimpleNamespace(execute=execute, flush=AsyncMock())
        for action in ("add_scent", "edit_scent", "delete_scent", "add_bottle", "edit_bottle",
                       "refill_bottle", "archive_bottle", "delete_bottle"):
            self.assertEqual(asyncio.run(coordinator.async_inventory_action(action, name="Test")), "record")
        self.assertEqual(len(calls), 8)
        self.assertEqual(coordinator.listener_updates, 8)
        self.assertFalse(coordinator.last_update_success)

    def test_inventory_takeover_rejects_legacy_weight_before_poll(self):
        coordinator = fake_coordinator()
        coordinator.inventory_manager = types.SimpleNamespace(owns_remaining=True, inventory=types.SimpleNamespace(initialized=True))
        with self.assertRaisesRegex(coordinator_module.HomeAssistantError, "bottle management"):
            asyncio.run(coordinator.async_record_bottle_weight(100, 20))
        self.assertEqual(coordinator.direct_polls, 0)
        self.assertEqual(coordinator.listener_updates, 0)

    def test_weight_actions_require_fresh_complete_observation_but_allow_power_on(self):
        for action in ("record_weight", "correct_weight"):
            coordinator = fake_coordinator()
            calls = []
            async def execute(name, **fields):
                calls.append(name)
            coordinator.inventory_manager = types.SimpleNamespace(execute=execute, flush=AsyncMock())
            coordinator._last_observation_at = None
            with self.assertRaisesRegex(coordinator_module.HomeAssistantError, "fresh complete"):
                asyncio.run(coordinator.async_inventory_action(action, gross_g=100))
            self.assertEqual(calls, [])
            coordinator._last_observation_at = datetime.now(timezone.utc)
            self.assertTrue(coordinator.data.power)
            asyncio.run(coordinator.async_inventory_action(action, gross_g=100))
            self.assertEqual(calls, [action])


if __name__ == "__main__":
    unittest.main()
