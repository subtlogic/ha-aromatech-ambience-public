"""D46 clock-driven accounting and confirmation regression cases."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock, patch

from test_estimation import UsageEstimator, observation, T, PRESETS
from test_coordinator import coordinator_module as c, fake_coordinator, complete_state, FIRST, module

DAY = ((255, 8, 0, 20, 0, 2),)


class ContinuousAccountingTests(unittest.TestCase):
    def estimator(self):
        e = UsageEstimator("America/Toronto", 605)
        observation(e, T, schedules=DAY)
        return e

    def test_one_hour_timer_partition_and_duplicate_timestamp(self):
        one, many = self.estimator(), self.estimator()
        one.advance(T + timedelta(hours=1))
        for seconds in range(30, 3601, 30):
            many.advance(T + timedelta(seconds=seconds))
        many.advance(T + timedelta(hours=1))
        for e in (one, many):
            self.assertAlmostEqual(e.expected_sprays, 14.4)
            self.assertAlmostEqual(e.active_seconds, 144)
            self.assertAlmostEqual(e.observed_seconds, 605)
            self.assertAlmostEqual(e.unobserved_seconds, 2995)
        self.assertAlmostEqual(one.active_seconds, many.active_seconds)

    def test_failed_heartbeat_recovery_never_readds_elapsed(self):
        e = self.estimator()
        e.advance(T + timedelta(minutes=5))
        e.invalidate(T + timedelta(minutes=5))
        e.advance(T + timedelta(hours=1))
        before = e.active_seconds
        observation(e, T + timedelta(hours=1), schedules=DAY)
        self.assertEqual(e.active_seconds, before)
        self.assertAlmostEqual(before, 144)
        self.assertEqual(e.unobserved_seconds, 3300)
        self.assertEqual(e.consecutive_failures, 0)

    def test_confirmed_command_corrects_only_known_pending_window(self):
        e = self.estimator()
        first = ((255, 8, 0, 20, 0, 1),)
        third = ((255, 8, 0, 20, 0, 3),)
        e = UsageEstimator("America/Toronto", 10000)
        observation(e, T, schedules=first)
        command_at = T + timedelta(minutes=40)
        e.begin_command(command_at, power=True, schedules=third, presets=PRESETS)
        e.advance(command_at + timedelta(seconds=30))
        observation(e, command_at + timedelta(seconds=30), schedules=third)
        e.advance(command_at + timedelta(hours=2))
        self.assertAlmostEqual(e.active_seconds, 2400 / 305 * 5 + 7200 / 130 * 10)
        self.assertAlmostEqual(e.adjustment_active_seconds, 30 / 130 * 10 - 30 / 305 * 5)
        self.assertEqual(e.command_status, "confirmed")
        self.assertEqual(e.adjustments[-1]["from"], command_at.isoformat())
        self.assertTrue(e.adjustments[-1]["onset_known"])

    def test_failed_command_retains_old_settings_and_audits(self):
        e = self.estimator()
        e.begin_command(T + timedelta(minutes=10), power=False, schedules=DAY, presets=PRESETS)
        e.fail_command(T + timedelta(minutes=11))
        e.advance(T + timedelta(minutes=20))
        self.assertAlmostEqual(e.active_seconds, 1200 / 250 * 10)
        self.assertEqual(e.command_status, "failed")
        self.assertEqual(e.adjustment_active_seconds, 0)
        self.assertEqual(e.adjustments[-1]["reason"], "command_unconfirmed")

    def test_external_mismatch_preserves_estimate_and_invalidates_calibration_window(self):
        e = self.estimator()
        e.record_weight(T, 130, 73)
        e.advance(T + timedelta(minutes=4))
        observation(e, T + timedelta(minutes=5), power=False, schedules=DAY)
        self.assertEqual(e.unobserved_seconds, 300)
        self.assertEqual(e.observed_seconds, 0)
        self.assertAlmostEqual(e.active_seconds, 12)
        self.assertEqual(e.adjustments[-1]["cycles"], 0)
        self.assertFalse(e.adjustments[-1]["onset_known"])
        e.record_weight(T + timedelta(minutes=5), 129, 73)
        self.assertEqual(e.calibration_status, "insufficient_coverage")

    def test_failed_pending_window_is_uncertain_even_after_immediate_matching_report(self):
        e = self.estimator()
        e.record_weight(T, 130, 73)
        e.begin_command(T + timedelta(minutes=1), power=False, schedules=DAY, presets=PRESETS)
        e.fail_command(T + timedelta(minutes=2))
        observation(e, T + timedelta(minutes=2), schedules=DAY)
        e.record_weight(T + timedelta(minutes=2), 129, 73)
        self.assertEqual(e.unobserved_seconds, 60)
        self.assertEqual(e.calibration_status, "insufficient_coverage")

    def test_pending_negative_correction_roundtrip_after_partitioned_ticks(self):
        e = self.estimator()
        e.begin_command(T, power=False, schedules=DAY, presets=PRESETS)
        for seconds in range(30, 601, 30):
            e.advance(T + timedelta(seconds=seconds))
        observation(e, T + timedelta(seconds=600), power=False, schedules=DAY)
        saved = json.loads(json.dumps(e.to_dict(), allow_nan=False))
        restored = UsageEstimator("America/Toronto", 605)
        restored.restore(saved)
        self.assertAlmostEqual(restored.active_seconds, 0)
        self.assertGreaterEqual(restored.active_seconds, 0)
        self.assertAlmostEqual(restored.active_seconds - restored.adjustment_active_seconds, 24)

    def test_restart_catchup_once_and_legacy_no_backfill(self):
        e = self.estimator()
        e.advance(T + timedelta(minutes=10))
        saved = json.loads(json.dumps(e.to_dict()))
        for _ in range(2):
            restored = UsageEstimator("America/Toronto", 605)
            restored.restore(saved)
            restored.advance(T + timedelta(minutes=30))
            restored.advance(T + timedelta(minutes=30))
            self.assertAlmostEqual(restored.active_seconds, 72)
            self.assertEqual(restored.unobserved_seconds, 1200)
        legacy = {k: v for k, v in saved.items() if k in (
            "version", "expected_sprays", "active_seconds", "observed_seconds",
            "unobserved_seconds", "checkpoint", "grams_per_active_second", "calibration_status")}
        legacy["version"] = 1
        restored.restore(legacy)
        observation(restored, T + timedelta(days=2), schedules=DAY)
        self.assertEqual(restored.active_seconds, saved["active_seconds"])

    def test_restored_malformed_assumptions_are_rejected_atomically(self):
        e = self.estimator()
        saved = e.to_dict()
        for mutation in ({"at": "not a date"}, {"inputs": {"power": True}},
                         {"last_contact_at": (T + timedelta(seconds=1)).isoformat()},
                         {"adjustment_active_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                e.restore(dict(saved, **mutation))
            self.assertEqual(e.to_dict(), saved)


class IndependentTimerTests(unittest.IsolatedAsyncioTestCase):
    def make_coordinator(self):
        co = fake_coordinator()
        co.estimates = UsageEstimator("UTC", 605)
        co._store = None
        co.hass = object()
        co.scan_interval = 300
        co._timer_unsubscribers = []
        co._heartbeat_running = False
        co._closed = False
        co._disconnect_task = None
        co._client = None
        return co

    async def test_two_independent_timers_and_exact_unload_cleanup(self):
        co = self.make_coordinator()
        callbacks, unsubscribers = [], []
        def track(hass, callback, interval):
            callbacks.append((callback, interval.total_seconds()))
            unsubscribe = Mock()
            unsubscribers.append(unsubscribe)
            return unsubscribe
        event = SimpleNamespace(async_track_time_interval=track)
        with patch.dict('sys.modules', {'homeassistant.helpers.event': event}):
            co.async_start_timers()
            co.async_start_timers()
        self.assertEqual([seconds for _, seconds in callbacks], [30, 300])
        state = complete_state([FIRST])
        state.intensity_presets = PRESETS
        state.raw[c.p.CMD_INTENSITY_PRESETS] = b'fresh'
        for minute in range(1, 6):
            with patch.object(c.dt_util, 'utcnow', return_value=T + timedelta(minutes=minute)):
                co._record_observation(state)
                co.async_set_updated_data(state)  # Frequent lighting entity updates.
        await callbacks[1][0](T + timedelta(minutes=5))
        self.assertEqual(co.direct_polls, 1)
        with patch.object(c.dt_util, 'utcnow', return_value=T + timedelta(minutes=5)):
            await co.async_close()
            await co.async_close()
        self.assertTrue(all(unsub.call_count == 1 for unsub in unsubscribers))
        await callbacks[1][0](T + timedelta(minutes=10))
        self.assertEqual(co.direct_polls, 1)

    async def test_timer_counts_offline_and_health_ages(self):
        co = self.make_coordinator()
        observation(co.estimates, T, schedules=DAY)
        co.last_update_success = False
        for minutes, health in ((5, 'healthy'), (6, 'stale'), (16, 'offline')):
            now = T + timedelta(minutes=minutes)
            with patch.object(c.dt_util, 'utcnow', return_value=now):
                await co._async_counter_tick(now)
                self.assertEqual(co.bluetooth_health, health)
        self.assertAlmostEqual(co.estimates.active_seconds, 960 / 250 * 10)
        self.assertEqual(co.listener_updates, 3)

    async def test_complete_incidental_report_confirms_without_poll(self):
        co = self.make_coordinator()
        state = complete_state([FIRST])
        state.intensity_presets = PRESETS
        state.raw[c.p.CMD_INTENSITY_PRESETS] = b'fresh'
        co._connect = AsyncMock(return_value=(object(), True))
        co._write_and_wait = AsyncMock(side_effect=[state, c.p.State(light_mode=1)])
        co._schedule_disconnect = Mock()
        with patch.object(c.dt_util, 'utcnow', return_value=T):
            await c.AmbienceCoordinator._exchange(co, c.p.light(1), affects_usage=False)
        self.assertEqual(co.estimates.last_contact_at, T)
        self.assertEqual(co.direct_polls, 0)
        self.assertEqual(co.estimates.command_status, 'idle')

    async def test_usage_commands_and_inventory_share_transaction_lock(self):
        co = self.make_coordinator()
        state = complete_state([FIRST])
        state.intensity_presets = PRESETS
        state.raw[c.p.CMD_INTENSITY_PRESETS] = b'fresh'
        co.data = state
        now = T
        entered, release = asyncio.Event(), asyncio.Event()
        writes = []
        async def exchange(frames, **kwargs):
            async with co._lock:
                co._begin_usage_command(frames, co.data)
                desired = b''.join(frame[2:] for frame in frames)[8] == c.p.POWER_ON_ARG
                writes.append(desired)
                return c.p.State(power=desired)
        async def refresh():
            if len(writes) == 1:
                entered.set()
                await release.wait()
            fresh = complete_state([FIRST])
            fresh.power = writes[-1]
            fresh.intensity_presets = PRESETS
            fresh.raw[c.p.CMD_INTENSITY_PRESETS] = b'fresh'
            async with co._lock:
                co._record_observation(fresh)
                co.data = fresh
        co._exchange, co.async_refresh = exchange, refresh
        inventory_entered = []
        async def inventory(action, **fields):
            inventory_entered.append(co.estimates.command_status)
        co._inventory_action_locked = inventory
        with patch.object(c.dt_util, 'utcnow', side_effect=lambda: now):
            co._record_observation(state)
            now = T + timedelta(minutes=1)
            first = asyncio.create_task(co.async_set_power(False))
            await entered.wait()
            second = asyncio.create_task(co.async_set_power(True))
            mutation = asyncio.create_task(co.async_inventory_action('record_weight'))
            await asyncio.sleep(0)
            self.assertEqual(writes, [False])
            self.assertEqual(inventory_entered, [])
            self.assertEqual(co.estimates.command_status, 'pending')
            now = T + timedelta(minutes=2)
            release.set()
            await asyncio.gather(first, second, mutation)
        self.assertEqual(writes, [False, True])
        self.assertEqual(inventory_entered, ['confirmed'])
        self.assertEqual([event['reason'] for event in co.estimates.adjustments],
                         ['confirmed_command', 'confirmed_command'])

    async def test_queued_tick_and_heartbeat_cannot_mutate_after_unload(self):
        co = self.make_coordinator()
        observation(co.estimates, T, schedules=DAY)
        co._connect = AsyncMock(return_value=(object(), False))
        await co._lock.acquire()
        tick = asyncio.create_task(co._async_counter_tick(T + timedelta(minutes=5)))
        await asyncio.sleep(0)
        with patch.object(c.dt_util, 'utcnow', return_value=T + timedelta(minutes=1)):
            close = asyncio.create_task(co.async_close())
            await asyncio.sleep(0)
            self.assertTrue(co._closed)
            co._lock.release()
            await asyncio.gather(tick, close)
        self.assertAlmostEqual(co.estimates.active_seconds, 60 / 250 * 10)
        with self.assertRaises(c.UpdateFailed):
            await c.AmbienceCoordinator._exchange(co, c.p.sync_time(T), handshake=False)
        co._connect.assert_not_awaited()

    async def test_future_saved_clock_exposes_offline_and_does_not_rewind(self):
        co = self.make_coordinator()
        future = T + timedelta(hours=1)
        observation(co.estimates, future, schedules=DAY)
        with patch.object(c.dt_util, 'utcnow', return_value=T):
            await co._async_counter_tick(T)
            self.assertEqual(co.bluetooth_health, 'offline')
        self.assertEqual(co.estimates._at, future)
        self.assertEqual(co.estimates.active_seconds, 0)

    async def test_corrupt_inventory_with_old_anchor_does_not_loop_catchup(self):
        co = self.make_coordinator()
        observation(co.estimates, T, schedules=DAY)
        co.inventory_manager = SimpleNamespace(_load_failed=True)
        await asyncio.wait_for(co._catchup_estimates(T + timedelta(days=30)), .1)
        self.assertEqual(co.estimates._at, T)

    async def test_legacy_weight_flushes_authoritative_snapshot_under_lock(self):
        co = self.make_coordinator()
        co._last_observation_at = T
        observation(co.estimates, T, schedules=DAY)
        saved = []
        async def flush():
            self.assertTrue(co._lock.locked())
            self.assertTrue(co._schedule_edit_lock.locked())
            saved.append(co.estimates.to_dict())
        co.inventory_manager = SimpleNamespace(owns_remaining=False,
            add_usage=Mock(), add_adjustment=Mock(), accounting_changed=Mock(), flush=flush)
        with patch.object(c.dt_util, 'utcnow', return_value=T):
            await co.async_record_bottle_weight(130, 73)
        restored = UsageEstimator('UTC', 605)
        restored.restore(saved[-1])
        self.assertEqual(restored._checkpoint['net_g'], 57)
