"""Mode transitions preserve a recoverable timed list across failures/restarts."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import types
import unittest
from unittest.mock import patch

from test_coordinator import fake_coordinator, complete_state, protocol, coordinator_module, FIRST, SECOND

Mode = coordinator_module.ScheduleMode
encode = coordinator_module.encode_mode


class MemoryStore:
    def __init__(self, data=None):
        self.data = deepcopy(data)
        self.saves = []
        self.fail = False

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("disk full")
        self.saves.append(deepcopy(data))
        self.data = deepcopy(data)


def configured(outcomes=(True, True), *, slots=None, experimental=True, store=None):
    c = fake_coordinator(outcomes, slots=slots, experimental=experimental)
    c.schedule_mode = Mode(store or MemoryStore())
    return c


class ScheduleModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_enable_restore_full_list_and_keep_current_intensity(self):
        first = replace(FIRST, days_mask=130, start_minute=37, unknown=b"abcd")
        c = configured(slots=[first, SECOND])
        await c.async_set_always_on(True)
        self.assertTrue(c.always_on)
        active = list(c.data.schedules)
        self.assertEqual(active[0], replace(first, days_mask=255, start_hour=8, start_minute=0, end_hour=8, end_minute=0))
        self.assertEqual(active[1], replace(SECOND, days_mask=SECOND.days_mask & 127))
        self.assertEqual(c.schedule_mode.store.saves[0]["backup"]["previous"], encode([first, SECOND]))
        self.assertEqual(c.always_on_attributes["saved_start"], "08:37")
        self.assertEqual(c.always_on_attributes["saved_days"], "Mon")
        self.assertEqual(c.always_on_attributes["saved_enabled"], "Enabled")
        c.data = complete_state([replace(active[0], intensity=3), active[1]])
        c.experimental_schedule_writes = False  # Opt-out must not trap a user in full day.
        await c.async_set_always_on(False)
        self.assertFalse(c.always_on)
        self.assertEqual(c.data.schedules, [replace(first, intensity=3), SECOND])
        self.assertIsNone(c.schedule_mode.backup)
        self.assertEqual(c.schedule_mode.store.data, {"version": 1, "backup": None})

    async def test_backup_survives_restart_and_repeated_on(self):
        c = configured(slots=[FIRST, SECOND])
        await c.async_set_always_on(True)
        saved = deepcopy(c.schedule_mode.backup)
        await c.async_set_always_on(True)
        self.assertEqual(len(c.commands), 1)
        self.assertEqual(c.schedule_mode.backup, saved)
        restored = configured(slots=c.data.schedules, store=c.schedule_mode.store)
        await restored.schedule_mode.initialize()
        await restored.async_set_always_on(False)
        self.assertEqual(restored.data.schedules, [FIRST, SECOND])

    async def test_gate_is_actionable_and_no_backup_or_device_mutation(self):
        c = configured(experimental=False)
        with self.assertRaisesRegex(Exception, "Enable Experimental schedule writes"):
            await c.async_set_always_on(True)
        self.assertEqual(c.commands, [])
        self.assertEqual(c.schedule_mode.store.saves, [])

    async def test_disk_failure_blocks_first_write(self):
        c = configured()
        c.schedule_mode.store.fail = True
        with self.assertRaisesRegex(Exception, "disk full"):
            await c.async_set_always_on(True)
        self.assertEqual(c.commands, [])
        self.assertIsNone(c.schedule_mode.backup)

    async def test_failed_activation_keeps_backup_and_off_cancels_without_write(self):
        c = configured(outcomes=(False, False, False))
        with self.assertRaisesRegex(Exception, "did not retain"):
            await c.async_set_always_on(True)
        self.assertEqual(c.data.schedules, [FIRST])
        self.assertIsNotNone(c.schedule_mode.backup)
        writes = len(c.commands)
        await c.async_set_always_on(False)
        self.assertEqual(len(c.commands), writes)
        self.assertIsNone(c.schedule_mode.backup)

    async def test_failed_restore_retains_journal_and_succeeds_after_restart(self):
        c = configured(outcomes=(True, False, False, False))
        await c.async_set_always_on(True)
        with self.assertRaisesRegex(Exception, "did not retain"):
            await c.async_set_always_on(False)
        self.assertIsNotNone(c.schedule_mode.backup["restoring"])
        restarted = configured(slots=c.data.schedules, store=c.schedule_mode.store)
        await restarted.schedule_mode.initialize()
        await restarted.async_set_always_on(False)
        self.assertEqual(restarted.data.schedules, [FIRST])
        self.assertIsNone(restarted.schedule_mode.backup)

    async def test_clear_failure_after_successful_restore_recovers_without_second_write(self):
        c = configured()
        await c.async_set_always_on(True)
        original_save = c.schedule_mode.store.async_save
        async def save(data):
            if data["backup"] is None:
                raise OSError("clear failed")
            await original_save(data)
        c.schedule_mode.store.async_save = save
        with self.assertRaisesRegex(Exception, "clear failed"):
            await c.async_set_always_on(False)
        self.assertFalse(c.always_on)
        c.schedule_mode.store.async_save = original_save
        restarted = configured(slots=c.data.schedules, store=c.schedule_mode.store)
        await restarted.schedule_mode.initialize()
        await restarted.async_set_always_on(False)
        self.assertEqual(restarted.commands, [])
        self.assertIsNone(restarted.schedule_mode.backup)

    async def test_external_edits_are_never_overwritten_or_saved_as_original(self):
        c = configured()
        await c.async_set_always_on(True)
        backup = deepcopy(c.schedule_mode.backup)
        c.data = complete_state([replace(FIRST, start_hour=9)])
        for enabled in (False, True):
            with self.assertRaisesRegex(Exception, "changed outside"):
                await c.async_set_always_on(enabled)
        self.assertEqual(len(c.commands), 1)
        self.assertEqual(c.schedule_mode.backup, backup)

    async def test_change_in_greeting_before_write_preserves_backup_and_device(self):
        c = configured()
        async def exchange(*, build):
            return build(complete_state([replace(FIRST, start_minute=12)]))
        c._exchange = exchange
        with self.assertRaisesRegex(Exception, "changed during"):
            await c.async_set_always_on(True)
        self.assertEqual(c.schedule_mode.backup["previous"], encode([FIRST]))

    async def test_timed_service_edits_locked_but_intensity_allowed(self):
        c = configured(outcomes=(True, True, True), slots=[FIRST, SECOND])
        await c.async_set_always_on(True)
        c.experimental_schedule_writes = False
        for changes in ({"start_hour": 9}, {"enabled": False}, {"day_bit": 1, "day_on": False}):
            with self.assertRaisesRegex(Exception, "Turn Always on off"):
                await c.async_set_schedule(**changes)
        with self.assertRaisesRegex(Exception, "Turn Always on off"):
            await c.async_delete_schedule(1)
        await c.async_set_schedule(intensity=3)
        self.assertEqual(c.data.intensity, 3)
        await c.async_set_always_on(False)
        self.assertEqual(c.data.schedules[0], replace(FIRST, intensity=3))

    async def test_external_full_day_without_backup_has_honest_state_and_no_fake_restore(self):
        c = configured(slots=[replace(FIRST, end_hour=8)])
        self.assertTrue(c.always_on)
        self.assertFalse(c.always_on_attributes["restore_available"])
        with self.assertRaisesRegex(Exception, "No saved timed schedule"):
            await c.async_set_always_on(False)
        self.assertEqual(c.commands, [])

    async def test_corrupt_mode_store_blocks_writes_and_is_not_overwritten(self):
        store = MemoryStore({"version": 50, "backup": None})
        c = configured(store=store)
        await c.schedule_mode.initialize()
        with self.assertRaisesRegex(Exception, "storage cannot be read"):
            await c.async_set_always_on(True)
        self.assertEqual(store.saves, [])
        self.assertEqual(c.commands, [])

    async def test_concurrent_on_off_serializes_backup_and_restoration(self):
        c = configured()
        await asyncio.gather(c.async_set_always_on(True), c.async_set_always_on(False))
        self.assertEqual(c.data.schedules, [FIRST])
        self.assertEqual(len(c.commands), 2)
        self.assertIsNone(c.schedule_mode.backup)

    async def test_incomplete_poll_never_authorizes_mode_change(self):
        c = configured()
        c.data.schedule_status = "partial"
        with self.assertRaisesRegex(Exception, "fresh complete"):
            await c.async_set_always_on(True)
        self.assertEqual(c.commands, [])
        self.assertEqual(c.schedule_mode.store.saves, [])

    async def test_manual_time_edit_cannot_bypass_restore_journal(self):
        c = configured()
        with self.assertRaisesRegex(Exception, "Use the Always on switch"):
            await c.async_set_schedule(end_hour=8, end_minute=0)
        self.assertEqual(c.commands, [])
        self.assertIsNone(c.schedule_mode.backup)

    async def test_pending_journal_exposes_recovery_without_claiming_device_all_day(self):
        c = configured(outcomes=(False, False, False))
        with self.assertRaisesRegex(Exception, "did not retain"):
            await c.async_set_always_on(True)
        self.assertFalse(c.always_on)
        self.assertTrue(c.timed_schedule_controls_locked)
        attrs = c.always_on_attributes
        self.assertTrue(attrs["restore_pending"])
        self.assertTrue(attrs["recovery_required"])
        self.assertTrue(attrs["restore_available"])
        await c.async_set_always_on(False)
        self.assertFalse(c.timed_schedule_controls_locked)
        self.assertFalse(c.always_on_attributes["restore_pending"])

    async def test_mode_writes_preserve_both_master_power_states_without_power_commands(self):
        for powered in (False, True):
            with self.subTest(powered=powered):
                c = configured(slots=[FIRST, SECOND])
                c.data.power = powered
                async def exchange(_frames=None, *, build=None):
                    before_power = c.data.power
                    frames = build(c.data)
                    payload = b"".join(frame[2:] for frame in frames)[7:]
                    # Five-byte requests are power commands; mode changes must
                    # always carry the nonempty schedule-list write shape.
                    self.assertGreater(len(payload), 5)
                    self.assertEqual(payload[2], 1)
                    slots = protocol.parse_schedules(payload)
                    c.commands.append(frames)
                    c.data = complete_state(slots)
                    c.data.power = before_power
                    return protocol.State(power=before_power, schedule_status="partial")
                c._exchange = exchange
                await c.async_set_always_on(True)
                self.assertIs(c.data.power, powered)
                await c.async_set_always_on(False)
                self.assertIs(c.data.power, powered)
                self.assertEqual(c.data.schedules, [FIRST, SECOND])
                self.assertEqual(len(c.commands), 2)
