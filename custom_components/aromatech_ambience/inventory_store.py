"""Checked atomic inventory and usage-accounting persistence."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path

from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store


class InventoryStore:
    """Use HA's storage envelope, but verify disk rather than its pending cache.

    Store logs some write failures instead of raising them and can defer saves
    during shutdown. Its loader also treats corrupt files as missing. Inventory
    mutations need stronger failure reporting, without relying on private APIs.
    """

    def __init__(self, hass, entry_id: str, *, suffix: str = "inventory") -> None:
        self._hass = hass
        if suffix not in ("inventory", "schedule_mode"):
            raise ValueError("Unsupported AromaTech storage kind")
        self.key = f"aromatech_ambience.{entry_id}.{suffix}"
        self._path = Path(hass.config.path(".storage", self.key))
        self._store = Store(hass, 1, self.key, atomic_writes=True)
        self._lock = asyncio.Lock()

    def _read(self):
        """Read actual disk; preserve malformed or unsupported files unchanged."""
        try:
            with self._path.open(encoding="utf-8") as stream:
                envelope = json.load(stream)
        except FileNotFoundError:
            return None
        if (
            not isinstance(envelope, dict)
            or type(envelope.get("version")) is not int
            or envelope["version"] != 1
            or type(envelope.get("minor_version")) is not int
            or envelope["minor_version"] != 1
            or envelope.get("key") != self.key
            or not isinstance(envelope.get("data"), dict)
        ):
            raise ValueError("Invalid or unsupported AromaTech inventory storage envelope")
        return envelope["data"]

    async def async_load(self):
        """Return inventory or None only when its file does not exist."""
        async with self._lock:
            return await self._hass.async_add_executor_job(self._read)

    async def async_save(self, data) -> None:
        """Persist a detached snapshot and confirm its contents reached disk."""
        snapshot = deepcopy(data)
        if not isinstance(snapshot, dict):
            raise ValueError("Inventory storage data must be a dictionary")
        # Reject non-JSON/NaN input before touching a valid existing file.
        expected_json = json.dumps(snapshot, allow_nan=False, sort_keys=True)
        async with self._lock:
            await self._hass.async_add_executor_job(self._read)
            if self._hass.state in (
                CoreState.stopping, CoreState.final_write, CoreState.stopped
            ):
                raise RuntimeError("Cannot confirm inventory persistence during shutdown")
            # No await between the state check and entering Store.async_save:
            # HA checks its shutdown state before its first suspension point.
            await self._store.async_save(snapshot)
            persisted = await self._hass.async_add_executor_job(self._read)
            if json.dumps(persisted, allow_nan=False, sort_keys=True) != expected_json:
                raise OSError("Inventory save did not persist the requested data")

    async def async_remove(self) -> None:
        """Remove this entry's inventory without touching estimator history."""
        async with self._lock:
            await self._store.async_remove()
            if await self._hass.async_add_executor_job(self._path.exists):
                raise OSError("Inventory storage removal did not reach disk")
