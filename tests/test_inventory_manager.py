"""Ledger coordination tests with real inventory and isolated HA boundaries."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).parents[1] / 'custom_components/aromatech_ambience'


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


class ServiceError(Exception):
    pass


class TestInventoryManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        self.saved = None
        self.store = types.SimpleNamespace(async_load=AsyncMock(return_value=None),
                                          async_save=AsyncMock(side_effect=self.save))
        self.notifications = types.SimpleNamespace(async_create=Mock(), async_dismiss=Mock())
        name = 'inventory_manager_isolated_test'
        stubs = {
            name: module(name, __path__=[str(ROOT)]),
            name + '.inventory_store': module(name + '.inventory_store', InventoryStore=lambda *args: self.store),
            'homeassistant': module('homeassistant'),
            'homeassistant.exceptions': module('homeassistant.exceptions', HomeAssistantError=ServiceError),
            'homeassistant.util': module('homeassistant.util', dt=types.SimpleNamespace(utcnow=lambda: self.now)),
            'homeassistant.components': module('homeassistant.components', persistent_notification=self.notifications),
        }
        spec = importlib.util.spec_from_file_location(name + '.inventory_manager', ROOT / 'inventory_manager.py')
        loaded = importlib.util.module_from_spec(spec)
        stubs[spec.name] = loaded
        ctx = patch.dict(sys.modules, stubs)
        ctx.start()
        self.addCleanup(ctx.stop)
        spec.loader.exec_module(loaded)
        self.cls = loaded.InventoryManager
        self.manager = self.cls(object(), 'entry1')
        await self.manager.initialize()

    async def save(self, payload):
        self.saved = deepcopy(payload)

    async def populate(self, oil=50):
        self.scent = await self.manager.execute('add_scent', name='Fir', grams_per_spray=0.1, spray_seconds=1)
        self.bottle = await self.manager.execute('add_bottle', name='Office', empty_g=70, oil_g=oil, scent_id=self.scent)
        await self.manager.execute('install_bottle', bottle_id=self.bottle)

    def fill(self, manager=None, bottle=None):
        return (manager or self.manager).inventory.bottles[bottle or self.bottle]['fills'][-1]

    async def test_mutation_is_published_only_after_save_completes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            entered.set()
            await release.wait()
            await self.save(payload)
        self.store.async_save.side_effect = slow
        task = asyncio.create_task(self.manager.execute('add_scent', name='Fir'))
        await entered.wait()
        self.assertEqual(self.manager.inventory.scents, {})
        release.set()
        ident = await task
        self.assertIn(ident, self.manager.inventory.scents)
        self.assertIn(ident, self.saved['inventory']['scents'])

    async def test_failed_mutation_rolls_back_without_notification(self):
        await self.populate()
        before = self.manager.inventory.to_dict()
        self.store.async_save.side_effect = OSError('disk full')
        with self.assertRaises(ServiceError):
            await self.manager.execute('edit_bottle', bottle_id=self.bottle, name='New name')
        self.assertEqual(self.manager.inventory.to_dict(), before)
        self.notifications.async_create.assert_not_called()
        self.assertEqual(self.manager.summary()['confidence'], 'storage_error')

    async def test_usage_arriving_during_save_is_flushed_exactly_once(self):
        await self.populate()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            entered.set()
            await release.wait()
            await self.save(payload)
        self.store.async_save.side_effect = slow
        self.manager.add_usage(1, 2, 0)
        task = asyncio.create_task(self.manager.flush())
        await entered.wait()
        self.manager.add_usage(3, 4, 0)
        release.set()
        await task
        self.assertEqual(self.fill()['cycles'], 4)
        self.assertEqual(self.fill()['active_seconds'], 6)
        self.assertAlmostEqual(self.fill()['remaining_g'], 49.4)
        self.assertFalse(self.manager._pending)
        await self.manager.flush()
        self.assertEqual(self.fill()['cycles'], 4)

    async def test_failed_usage_save_retains_queue_for_retry(self):
        await self.populate()
        self.manager.add_usage(2, 3, 0)
        self.store.async_save.side_effect = OSError('disk full')
        with self.assertRaises(ServiceError):
            await self.manager.flush()
        self.assertEqual(self.fill()['cycles'], 0)
        self.assertEqual(len(self.manager._pending), 1)
        self.manager.add_usage(4, 5, 0)
        self.store.async_save.side_effect = self.save
        await self.manager.flush()
        self.assertEqual(self.fill()['cycles'], 6)
        self.assertEqual(self.fill()['active_seconds'], 8)
        self.assertIsNone(self.manager.error)

    async def test_usage_during_store_keeps_original_bottle_identity(self):
        await self.populate()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            entered.set()
            await release.wait()
            await self.save(payload)
        self.store.async_save.side_effect = slow
        task = asyncio.create_task(self.manager.execute('store_bottle', bottle_id=self.bottle))
        await entered.wait()
        self.manager.add_usage(2, 3, 0)
        release.set()
        await task
        self.assertIsNone(self.manager.inventory.active_bottle_id)
        await self.manager.flush()
        self.assertEqual(self.fill()['cycles'], 2)
        self.assertIsNone(self.manager.inventory.active_bottle_id)

    async def test_corrupt_load_disables_mutation_and_flush(self):
        self.store.async_load.side_effect = ValueError('corrupt')
        await self.manager.initialize()
        with self.assertRaises(ServiceError):
            await self.manager.execute('add_scent', name='Fir')
        with self.assertRaises(ServiceError):
            await self.manager.acknowledge()
        await self.manager.flush()
        self.store.async_save.assert_not_awaited()

    async def test_restart_bounds_known_elapsed_and_first_observation_once(self):
        await self.populate()
        self.manager.add_usage(0, 0, 0)
        await self.manager.execute('edit_bottle', bottle_id=self.bottle, name='Office')
        self.store.async_load.return_value = deepcopy(self.saved)
        self.now += timedelta(seconds=20)
        restored = self.cls(object(), 'entry1')
        await restored.initialize()
        self.now += timedelta(seconds=5)
        restored.add_usage(0, 0, 0)
        await restored.flush()
        self.assertEqual(self.fill(restored)['unknown_seconds'], 25)
        restored.add_usage(0, 0, 0)
        await restored.flush()
        self.assertEqual(self.fill(restored)['unknown_seconds'], 25)
        self.assertAlmostEqual(self.fill(restored)['uncertainty_g'], 2.5)

    async def test_alert_dedup_ack_recovery_and_new_fill(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        self.notifications.async_create.assert_called_once()
        self.assertEqual(self.notifications.async_create.call_args.kwargs['notification_id'], 'aromatech_ambience_refill_entry1')
        await self.manager.flush()
        self.assertEqual(self.notifications.async_create.call_count, 1)
        await self.manager.acknowledge()
        self.assertTrue(self.saved['alerts']['acknowledged'])
        await self.manager.flush()
        self.assertEqual(self.notifications.async_create.call_count, 1)
        await self.manager.execute('store_bottle', bottle_id=self.bottle)
        await self.manager.execute('refill_bottle', bottle_id=self.bottle, scent_id=self.scent, oil_g=4)
        await self.manager.execute('install_bottle', bottle_id=self.bottle)
        self.assertEqual(self.notifications.async_create.call_count, 2)
        self.assertFalse(self.saved['alerts']['acknowledged'])

    async def test_severity_increase_rearms_acknowledged_alert(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        await self.manager.acknowledge()
        self.manager.add_usage(40, 40, 0)
        await self.manager.flush()
        self.assertEqual(self.manager.summary()['status'], 'estimated_depleted')
        self.assertEqual(self.notifications.async_create.call_count, 2)
        self.assertFalse(self.saved['alerts']['acknowledged'])

    async def test_live_options_disable_then_enable_rearms_notification(self):
        await self.populate(oil=4)
        self.notifications.async_create.assert_not_called()
        self.manager.options = {'refill_notifications': True}
        await self.manager.flush()
        self.assertEqual(self.notifications.async_create.call_count, 1)
        self.manager.options = {'refill_notifications': False}
        await self.manager.flush()
        self.assertIsNone(self.saved['alerts']['key'])
        self.manager.options = {'refill_notifications': True}
        await self.manager.flush()
        self.assertEqual(self.notifications.async_create.call_count, 2)

    async def test_ack_save_failure_preserves_old_ack_and_notification(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        self.notifications.async_dismiss.reset_mock()
        self.store.async_save.side_effect = OSError('disk full')
        with self.assertRaises(ServiceError):
            await self.manager.acknowledge()
        self.assertFalse(self.manager._alerts['acknowledged'])
        self.notifications.async_dismiss.assert_not_called()

    async def test_severity_downgrade_dismisses_then_reincrease_notifies(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        self.manager.add_usage(40, 40, 0)
        await self.manager.flush()
        await self.manager.acknowledge()
        count = self.notifications.async_create.call_count
        await self.manager.execute('correct_weight', bottle_id=self.bottle, gross_g=74, reason='scale correction')
        self.assertEqual(self.manager.summary()['status'], 'low')
        self.assertEqual(self.notifications.async_create.call_count, count)
        self.assertTrue(self.manager._alerts['acknowledged'])
        self.manager.add_usage(40, 40, 0)
        await self.manager.flush()
        self.assertEqual(self.notifications.async_create.call_count, count + 1)

    async def test_saved_acknowledgement_survives_restore_without_duplicate(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        self.manager.add_usage(0, 0, 0)
        await self.manager.acknowledge()
        self.store.async_load.return_value = deepcopy(self.saved)
        restored = self.cls(object(), 'entry1', options=self.manager.options)
        await restored.initialize()
        await restored.flush()
        self.assertTrue(restored._alerts['acknowledged'])
        self.assertEqual(self.notifications.async_create.call_count, 1)

    def forecast_state(self):
        return types.SimpleNamespace(schedule_status='complete',
                                     schedules=[(255, 8, 0, 8, 10, 1)],
                                     intensity_presets=[(1, 1)])

    async def test_forecast_unavailable_preserves_ack_across_restart_and_failed_poll(self):
        self.manager.options = {'refill_notifications': True}
        self.manager.state = self.forecast_state()
        await self.populate(oil=50)
        self.assertEqual(self.manager.summary()['status'], 'refill_soon')
        self.manager.add_usage(0, 0, 0)
        await self.manager.acknowledge()
        alert = deepcopy(self.manager._alerts)
        self.store.async_load.return_value = deepcopy(self.saved)
        restored = self.cls(object(), 'entry1', options=self.manager.options)
        await restored.initialize()
        for state in (None, types.SimpleNamespace(schedule_status='invalid', schedules=[], intensity_presets=[])):
            restored.state = state
            await restored.flush()
            self.assertEqual(restored.summary()['status'], 'forecast_unavailable')
            self.assertEqual(restored._alerts, alert)
        restored.state = self.forecast_state()
        await restored.flush()
        self.assertEqual(restored._alerts, alert)
        self.assertEqual(self.notifications.async_create.call_count, 1)

    async def test_unacknowledged_restored_forecast_alert_recreated_once(self):
        self.manager.options = {'refill_notifications': True}
        self.manager.state = self.forecast_state()
        await self.populate()
        self.manager.add_usage(0, 0, 0)
        await self.manager.flush()
        self.store.async_load.return_value = deepcopy(self.saved)
        restored = self.cls(object(), 'entry1', options=self.manager.options)
        await restored.initialize()
        await restored.flush()
        await restored.flush()
        self.assertEqual(self.notifications.async_create.call_count, 2)
        self.assertIn('refill soon', self.notifications.async_create.call_args.args[1])
        restored.state = self.forecast_state()
        await restored.flush()
        self.assertEqual(self.notifications.async_create.call_count, 2)

    async def test_zero_usage_observations_persist_recent_time(self):
        await self.populate()
        for _ in range(3):
            self.now += timedelta(hours=1)
            self.manager.add_usage(0, 0, 0)
            await self.manager.flush()
            self.assertEqual(self.saved['observed_at'], self.now.isoformat())
        self.store.async_load.return_value = deepcopy(self.saved)
        self.now += timedelta(seconds=10)
        restored = self.cls(object(), 'entry1')
        await restored.initialize()
        self.assertEqual(self.fill(restored)['unknown_seconds'], 10)

    async def test_future_observation_is_unbounded_without_blocking_load(self):
        await self.populate()
        self.manager.add_usage(0, 0, 0)
        await self.manager.flush()
        saved = deepcopy(self.saved)
        saved['observed_at'] = (self.now + timedelta(hours=1)).isoformat()
        self.store.async_load.return_value = saved
        restored = self.cls(object(), 'entry1')
        await restored.initialize()
        self.assertFalse(restored._load_failed)
        self.assertIsNone(restored.error)
        self.assertTrue(self.fill(restored)['unbounded_unknown'])
        self.assertEqual(restored.summary()['status'], 'needs_weighing')
        await restored.execute('edit_bottle', bottle_id=self.bottle, name='Still editable')

    async def test_metadata_save_failure_retries_clean_ledger_without_pending_usage(self):
        await self.populate()
        self.store.async_save.side_effect = OSError('disk full')
        with self.assertRaises(ServiceError):
            await self.manager.execute('edit_bottle', bottle_id=self.bottle, name='Rejected')
        self.assertFalse(self.manager._pending)
        self.assertTrue(self.manager._dirty)
        self.store.async_save.side_effect = self.save
        await self.manager.flush()
        self.assertIsNone(self.manager.error)
        self.assertFalse(self.manager._dirty)
        self.assertEqual(self.manager.inventory.bottles[self.bottle]['name'], 'Office')

    async def test_corrupt_load_owns_remaining_to_prevent_legacy_fallback(self):
        self.store.async_load.side_effect = ValueError('corrupt')
        await self.manager.initialize()
        self.assertTrue(self.manager.owns_remaining)
        self.assertIsNone(self.manager.summary()['remaining_g'])
        self.assertEqual(self.manager.summary()['confidence'], 'storage_error')

    async def test_bounded_uncertainty_crossing_low_creates_weighing_alert(self):
        self.manager.options = {'refill_notifications': True}
        self.manager.state = self.forecast_state()
        await self.populate(oil=10)
        await self.manager.acknowledge()
        before = self.notifications.async_create.call_count
        self.manager.add_usage(0, 0, 60)
        await self.manager.flush()
        summary = self.manager.summary()
        self.assertEqual(summary['status'], 'needs_weighing')
        self.assertEqual(summary['remaining_lower_g'], 4)
        self.assertIsNone(summary['remaining_g'])
        self.assertEqual(self.notifications.async_create.call_count, before + 1)
        self.assertIn('needs weighing', self.notifications.async_create.call_args.args[1])
        self.assertFalse(self.manager._alerts['acknowledged'])

    async def test_zero_usage_arriving_during_save_persists_latest_observation(self):
        await self.populate()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(payload):
            entered.set()
            await release.wait()
            await self.save(payload)
        self.store.async_save.side_effect = slow
        self.manager.add_usage(1, 1, 0)
        task = asyncio.create_task(self.manager.flush())
        await entered.wait()
        self.now += timedelta(minutes=5)
        self.manager.add_usage(0, 0, 0)
        release.set()
        await task
        self.assertEqual(self.saved['observed_at'], self.now.isoformat())

    async def test_bounded_uncertainty_keeps_already_low_warning(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        self.manager.add_usage(0, 0, 1)
        await self.manager.flush()
        self.assertEqual(self.manager.summary()['status'], 'low')
        self.assertEqual(self.notifications.async_create.call_count, 1)
        self.assertIn('low', self.notifications.async_create.call_args.args[1])

    async def test_suppressed_downgrade_stays_suppressed_after_restart(self):
        self.manager.options = {'refill_notifications': True}
        await self.populate(oil=4)
        self.manager.add_usage(40, 40, 0)
        await self.manager.flush()
        await self.manager.execute('correct_weight', bottle_id=self.bottle, gross_g=74, reason='scale correction')
        self.assertEqual(self.manager.summary()['status'], 'low')
        self.assertTrue(self.saved['alerts']['acknowledged'])
        before = self.notifications.async_create.call_count
        self.store.async_load.return_value = deepcopy(self.saved)
        restored = self.cls(object(), 'entry1', options=self.manager.options)
        await restored.initialize()
        await restored.flush()
        self.assertEqual(restored.summary()['status'], 'low')
        self.assertTrue(restored._alerts['acknowledged'])
        self.assertEqual(self.notifications.async_create.call_count, before)
        restored.add_usage(40, 40, 0)
        await restored.flush()
        self.assertEqual(self.notifications.async_create.call_count, before + 1)

    async def test_weighing_resolution_rearms_each_established_alert_status(self):
        self.manager.options = {'refill_notifications': True}
        self.manager.state = self.forecast_state()
        await self.populate(oil=10)
        for gross, status in ((74, 'low'), (70, 'estimated_depleted'), (80, 'refill_soon')):
            with self.subTest(status=status):
                await self.manager.execute('correct_weight', bottle_id=self.bottle, gross_g=80, reason='baseline')
                self.manager.add_usage(0, 0, 60)
                await self.manager.flush()
                self.assertEqual(self.manager.summary()['status'], 'needs_weighing')
                await self.manager.acknowledge()
                before = self.notifications.async_create.call_count
                await self.manager.execute('correct_weight', bottle_id=self.bottle, gross_g=gross, reason='weighed')
                self.assertEqual(self.manager.summary()['status'], status)
                self.assertEqual(self.notifications.async_create.call_count, before + 1)
                self.assertFalse(self.saved['alerts']['acknowledged'])

    async def test_atomic_accounting_snapshot_survives_failed_save_and_restart_once(self):
        from test_estimation import UsageEstimator, observation, PRESETS
        await self.populate(oil=100)
        start = self.now
        slots = ((255, 8, 0, 20, 0, 2),)
        estimator = UsageEstimator('UTC', 605)
        observation(estimator, start, schedules=slots)
        self.manager.accounting_provider = estimator.to_dict

        async def settle(manager, model, at):
            before = (model.expected_sprays, model.active_seconds, model.unobserved_seconds,
                      model.uncertain_active_seconds)
            model.advance(at)
            manager.add_usage(*(value - old for value, old in zip(
                (model.expected_sprays, model.active_seconds, model.unobserved_seconds,
                 model.uncertain_active_seconds), before)))
            manager.accounting_changed()
            await manager.flush()

        self.now += timedelta(minutes=10)
        await settle(self.manager, estimator, self.now)
        saved = deepcopy(self.saved)
        self.assertEqual(saved['accounting']['at'], self.now.isoformat())
        self.assertAlmostEqual(self.fill()['active_seconds'], 24)
        self.store.async_save.side_effect = OSError('disk full')
        self.now += timedelta(minutes=10)
        with self.assertRaises(ServiceError):
            await settle(self.manager, estimator, self.now)
        self.assertEqual(self.saved, saved)
        self.assertTrue(self.manager._pending)

        # A process restart ignores the failed in-memory watermark, restoring the
        # exact durable pair (ledger + model), then catching up once.
        self.store.async_load.return_value = deepcopy(saved)
        self.store.async_save.side_effect = self.save
        self.now += timedelta(minutes=10)
        restored = self.cls(object(), 'entry1')
        await restored.initialize()
        model = UsageEstimator('UTC', 605)
        model.restore(restored.accounting_snapshot)
        restored.accounting_provider = model.to_dict
        await settle(restored, model, self.now)
        await settle(restored, model, self.now)
        self.assertAlmostEqual(self.fill(restored)['active_seconds'], 72)
        self.assertAlmostEqual(self.fill(restored)['consumed_g'], 7.2)
        self.assertEqual(self.saved['accounting']['at'], self.now.isoformat())
        self.assertEqual(self.fill(restored)['unknown_seconds'], 1200)
        self.store.async_load.return_value = deepcopy(self.saved)
        again = self.cls(object(), 'entry1')
        await again.initialize()
        again_model = UsageEstimator('UTC', 605)
        again_model.restore(again.accounting_snapshot)
        again.accounting_provider = again_model.to_dict
        await settle(again, again_model, self.now)
        self.assertAlmostEqual(self.fill(again)['active_seconds'], 72)

    async def test_pending_usage_keeps_exact_fill_and_rate_across_refill(self):
        await self.populate()
        old_fill = self.fill()['id']
        self.manager.add_usage(2, 3, 0)
        await self.manager.execute('store_bottle', bottle_id=self.bottle)
        await self.manager.execute('refill_bottle', bottle_id=self.bottle, scent_id=self.scent, oil_g=50)
        await self.manager.execute('install_bottle', bottle_id=self.bottle)
        self.manager.add_usage(4, 5, 0)
        await self.manager.flush()
        fills = self.manager.inventory.bottles[self.bottle]['fills']
        self.assertEqual(fills[0]['id'], old_fill)
        self.assertEqual(fills[0]['active_seconds'], 3)
        self.assertEqual(fills[1]['active_seconds'], 5)
        self.assertAlmostEqual(fills[0]['consumed_g'], .3)
        self.assertAlmostEqual(fills[1]['consumed_g'], .5)

    async def test_weighing_signed_adjustment_is_audited_without_repricing_usage(self):
        await self.populate()
        self.manager.add_usage(2, 10, 0)
        await self.manager.flush()
        await self.manager.execute('record_weight', bottle_id=self.bottle, gross_g=118)
        self.assertEqual(self.fill()['consumed_g'], 1)
        self.assertEqual(self.fill()['remaining_g'], 48)
        event = self.saved['audit'][-1]
        self.assertEqual(event['reason'], 'record_weight')
        self.assertEqual(event['remaining_adjustment_g'], -1)
        self.assertEqual(event['fill_id'], self.fill()['id'])
        self.assertEqual(self.manager.export()['accounting_audit'][-1], event)

    async def test_signed_usage_reversal_retains_depletion_debt(self):
        await self.populate(oil=1)
        self.manager.add_usage(2, 20, 0)
        await self.manager.flush()
        self.assertEqual(self.fill()['remaining_g'], 0)
        event = {'at': self.now.isoformat(), 'reason': 'confirmed_command',
                 'cycles': -1, 'active_seconds': -10}
        self.manager.add_adjustment(-1, -10, event)
        await self.manager.flush()
        self.assertAlmostEqual(self.fill()['consumed_g'], 1)
        self.assertEqual(self.fill()['remaining_g'], 0)
        self.assertEqual(self.saved['audit'][-1]['consumed_g'], -1)
        self.manager.add_adjustment(-2, -20, event)
        with self.assertRaises(ValueError):
            await self.manager.flush()

    async def test_uncertainty_bounds_do_not_double_subtract_estimated_consumption(self):
        await self.populate(oil=100)
        # 10s estimated pump time in 100s uncertain elapsed; rate .1g/s.
        self.manager.add_usage(1, 10, 100, 10)
        await self.manager.flush()
        summary = self.manager.summary()
        self.assertEqual(self.fill()['remaining_g'], 99)
        self.assertEqual(summary['remaining_lower_g'], 90)
        self.assertEqual(summary['remaining_upper_g'], 100)
        self.assertEqual(self.fill()['uncertainty_g'], 9)
        self.assertEqual(self.fill()['uncertainty_upper_g'], 1)

    async def test_corrupt_audit_is_preserved_and_blocks_mutations(self):
        await self.populate()
        saved = deepcopy(self.saved)
        saved['audit'] = [{'at': self.now.isoformat(), 'reason': 'record_weight',
                           'bottle_id': self.bottle, 'fill_id': self.fill()['id'],
                           'remaining_adjustment_g': float('nan')}]
        self.store.async_load.return_value = saved
        restored = self.cls(object(), 'entry1')
        await restored.initialize()
        self.assertTrue(restored._load_failed)
        with self.assertRaises(ServiceError):
            await restored.execute('edit_bottle', bottle_id=self.bottle, name='Unsafe')

    async def test_delete_bottle_removes_its_audit_transactionally(self):
        await self.populate()
        await self.manager.execute('record_weight', bottle_id=self.bottle, gross_g=119)
        self.assertTrue(self.manager.audit)
        await self.manager.execute('store_bottle', bottle_id=self.bottle)
        await self.manager.execute('delete_bottle', bottle_id=self.bottle, confirm=True)
        await self.manager.flush()
        self.assertEqual(self.manager.audit, [])
        self.assertEqual(self.saved['audit'], [])

    async def test_atomic_envelope_without_snapshot_fails_closed(self):
        await self.populate()
        saved = deepcopy(self.saved)
        saved['version'] = 2
        saved['accounting'] = None
        self.store.async_load.return_value = saved
        restored = self.cls(object(), 'entry1')
        await restored.initialize()
        self.assertTrue(restored._load_failed)
