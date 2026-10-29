"""Checked storage against actual temporary files, with HA failure semantics."""

import asyncio
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class CoreState(Enum):
    running = 1
    stopping = 2
    final_write = 3
    stopped = 4


class FakeStore:
    def __init__(self, hass, version, key, **kwargs):
        self.path = Path(hass.config.path('.storage', key))
        self.key, self.version, self.kwargs = key, version, kwargs
        self.swallow = False
        self.pending = None
        self.saves = 0
        self.entered = asyncio.Event()
        self.release = None

    async def async_save(self, data):
        self.saves += 1
        self.pending = data
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.swallow:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(dict(version=self.version, minor_version=1,
                                           key=self.key, data=data)))

    async def async_remove(self):
        self.path.unlink(missing_ok=True)


def module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


class TestInventoryStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        async def executor(func, *args):
            return await asyncio.to_thread(func, *args)
        self.hass = types.SimpleNamespace(
            state=CoreState.running, async_add_executor_job=executor,
            config=types.SimpleNamespace(path=lambda *parts: str(Path(self.tmp.name, *parts))))
        stubs = {
            'homeassistant': module('homeassistant'),
            'homeassistant.core': module('homeassistant.core', CoreState=CoreState),
            'homeassistant.helpers': module('homeassistant.helpers'),
            'homeassistant.helpers.storage': module('homeassistant.helpers.storage', Store=FakeStore),
        }
        source = Path(__file__).parents[1] / 'custom_components/aromatech_ambience/inventory_store.py'
        spec = importlib.util.spec_from_file_location('checked_inventory_test', source)
        mod = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(mod)
        self.store = mod.InventoryStore(self.hass, 'entry1')
        self.raw = self.store._store

    async def test_atomic_key_missing_roundtrip_and_remove(self):
        self.assertEqual(self.raw.kwargs, {'atomic_writes': True})
        self.assertEqual(self.raw.key, 'aromatech_ambience.entry1.inventory')
        self.assertIsNone(await self.store.async_load())
        await self.store.async_save({'bottles': {'one': {'grams': 12.5}}})
        self.assertEqual((await self.store.async_load())['bottles']['one']['grams'], 12.5)
        await self.store.async_remove()
        self.assertIsNone(await self.store.async_load())

    async def test_schedule_mode_uses_isolated_checked_key(self):
        mode = type(self.store)(self.hass, "entry1", suffix="schedule_mode")
        self.assertEqual(mode.key, "aromatech_ambience.entry1.schedule_mode")
        await self.store.async_save({"bottles": {"a": {"grams": 10}}})
        await mode.async_save({"version": 1, "backup": None})
        self.assertEqual(await self.store.async_load(), {"bottles": {"a": {"grams": 10}}})
        mode._store.swallow = True
        with self.assertRaises(OSError):
            await mode.async_save({"version": 1, "backup": {"previous": []}})
        await mode.async_remove()
        self.assertIsNotNone(await self.store.async_load())

    async def test_swallowed_failure_ignores_pending_cache(self):
        await self.store.async_save({'grams': 20})
        self.raw.swallow = True
        with self.assertRaises(OSError):
            await self.store.async_save({'grams': 10})
        self.assertEqual(self.raw.pending, {'grams': 10})
        self.assertEqual(await self.store.async_load(), {'grams': 20})

    async def test_equal_python_values_cannot_hide_failed_type_change(self):
        await self.store.async_save({'value': True})
        self.raw.swallow = True
        with self.assertRaises(OSError):
            await self.store.async_save({'value': 1})

    async def test_first_write_swallowed_failure(self):
        self.raw.swallow = True
        with self.assertRaises(OSError):
            await self.store.async_save({'grams': 10})

    async def test_corrupt_file_retained_and_never_overwritten(self):
        self.raw.path.parent.mkdir(parents=True)
        self.raw.path.write_text('{corrupt')
        with self.assertRaises(ValueError):
            await self.store.async_load()
        with self.assertRaises(ValueError):
            await self.store.async_save({'grams': 10})
        self.assertEqual(self.raw.path.read_text(), '{corrupt')
        self.assertEqual(self.raw.saves, 0)

    async def test_wrong_envelopes_rejected(self):
        await self.store.async_save({})
        valid = json.loads(self.raw.path.read_text())
        for field, value in [('version', 2), ('version', True), ('minor_version', 2),
                             ('key', 'another'), ('data', None)]:
            with self.subTest(field=field, value=value):
                self.raw.path.write_text(json.dumps({**valid, field: value}))
                with self.assertRaises(ValueError):
                    await self.store.async_load()

    async def test_shutdown_rejected_even_if_existing_payload_matches(self):
        await self.store.async_save({'grams': 10})
        for state in (CoreState.stopping, CoreState.final_write, CoreState.stopped):
            self.hass.state = state
            with self.assertRaises(RuntimeError):
                await self.store.async_save({'grams': 10})
        self.assertEqual(self.raw.saves, 1)

    async def test_input_detached_while_save_waits(self):
        self.raw.release = asyncio.Event()
        payload = {'bottles': ['original']}
        task = asyncio.create_task(self.store.async_save(payload))
        await self.raw.entered.wait()
        payload['bottles'].append('later')
        self.raw.release.set()
        await task
        self.assertEqual(await self.store.async_load(), {'bottles': ['original']})

    async def test_non_json_input_does_not_touch_store(self):
        for data in ([], {'grams': float('nan')}, {'grams': float('inf')}):
            with self.assertRaises(ValueError):
                await self.store.async_save(data)
        self.assertEqual(self.raw.saves, 0)


if __name__ == '__main__':
    unittest.main()
