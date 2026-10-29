"""BLE transport and state for the AromaTech Ambience.

Connect on demand and release shared Bluetooth proxy slots after use.
State refresh uses a time-sync command and processes the returned state."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from dataclasses import replace

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import protocol as p
from .const import DISCONNECT_DELAY, REPLY_TIMEOUT, SCAN_INTERVAL_SECONDS
from .estimation import UsageEstimator
from .schedule_mode import ScheduleMode, decode as decode_mode, encode as encode_mode, is_full_day, same_timing

_LOGGER = logging.getLogger(__name__)

# Kept here rather than in const.py so that updating this file alone is a
# complete upgrade. Splitting them across two files once meant a coordinator
# that imported names the deployed const.py did not have, and the integration
# failed to load at all.
CONNECT_ATTEMPTS = 3
TRACE_FRAMES = 24        # roughly two full exchanges
NOTIFY_SETTLE = 0.5      # let the link settle before writing the CCCD
RETRY_BACKOFF = 1.0
SCHEDULE_WRITE_ATTEMPTS = 3


class AmbienceCoordinator(DataUpdateCoordinator[p.State]):
    """Owns the connection and the last known device state."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        scan_interval: int = SCAN_INTERVAL_SECONDS,
        *,
        time_zone: str = "UTC",
        experimental_schedule_writes: bool = False,
        options: dict | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Ambience {address}",
            # The heartbeat. Each tick connects, sends a time sync and
            # disconnects, and the device answers with its whole state - so
            # this is also how a change made in the vendor app, or at the unit
            # itself, gets noticed. Shorter means fresher, at the cost of
            # occupying one of the proxy's three connection slots more often.
            update_interval=None,  # Independent heartbeat must not be postponed by entity updates.
        )
        self.address = address
        self._lock = asyncio.Lock()
        self._client: BleakClient | None = None
        self._disconnect_task: asyncio.Task | None = None
        self._reply: asyncio.Future[p.State] | None = None
        self._reassembler = p.Reassembler()
        self._accumulated: p.State | None = None
        # Surfaced in the diagnostics download. A single "last write" slot was not
        # enough: a command that draws no state-carrying reply ends with
        # async_request_refresh, whose own time sync overwrites the slot before
        # anyone can read it. The command then looks like it never happened.
        # A ring keeps the whole exchange - greeting, command, reply, refresh.
        self.trace: deque[str] = deque(maxlen=TRACE_FRAMES)
        self.estimates = UsageEstimator(time_zone, 2 * scan_interval + REPLY_TIMEOUT)
        self.experimental_schedule_writes = experimental_schedule_writes
        self._schedule_edit_lock = asyncio.Lock()
        self._store = None
        self._last_observation_at = None
        self.options = dict(options or {})
        self.inventory_manager = None
        self.schedule_mode = None
        self.scan_interval = scan_interval
        self._timer_unsubscribers = []
        self._heartbeat_running = False
        self._closed = False

    async def async_initialize_schedule_mode(self, entry_id: str) -> None:
        from .inventory_store import InventoryStore

        self.schedule_mode = ScheduleMode(InventoryStore(self.hass, entry_id, suffix="schedule_mode"))
        await self.schedule_mode.initialize()

    @property
    def always_on(self):
        state = self.data
        if state is None or state.schedule_status != "complete" or state.schedules is None:
            return None
        return is_full_day(state.schedules)

    @property
    def timed_schedule_controls_locked(self):
        mode = getattr(self, "schedule_mode", None)
        return bool(self.always_on or (mode is not None and mode.backup is not None))

    @property
    def always_on_attributes(self):
        mode = getattr(self, "schedule_mode", None)
        backup = mode.backup if mode else None
        saved = decode_mode(backup["previous"]) if backup else []
        first = saved[0] if saved else None
        restore_pending = bool(backup and (backup["restoring"] is not None or self.always_on is not True))
        names = ((0, "Sun"), (1, "Mon"), (2, "Tue"), (3, "Wed"),
                 (4, "Thu"), (5, "Fri"), (6, "Sat"))
        return {
            "experimental_enabled": self.experimental_schedule_writes,
            "restore_available": bool(backup),
            "restore_pending": restore_pending,
            "recovery_required": restore_pending or bool(mode and mode.error),
            "timed_controls_locked": self.timed_schedule_controls_locked,
            "storage_error": mode.error if mode else "Schedule storage not loaded",
            "saved_schedule": [
                {"slot": i + 1, "days_mask": slot.days_mask & 127,
                 "enabled": bool(slot.days_mask & 128),
                 "start": f"{slot.start_hour:02d}:{slot.start_minute:02d}",
                 "end": f"{slot.end_hour:02d}:{slot.end_minute:02d}",
                 "intensity": slot.intensity}
                for i, slot in enumerate(saved)
            ],
            "saved_start": f"{first.start_hour:02d}:{first.start_minute:02d}" if first else None,
            "saved_end": f"{first.end_hour:02d}:{first.end_minute:02d}" if first else None,
            "saved_days": ",".join(name for bit, name in names if first.days_mask & (1 << bit)) if first else None,
            "saved_enabled": ("Enabled" if first.days_mask & 128 else "Disabled") if first else None,
        }

    async def async_initialize_storage(self, entry_id: str) -> None:
        """Restore the atomic ledger/accounting pair, or migrate legacy totals."""
        from homeassistant.helpers.storage import Store

        self._store = Store(self.hass, 1, f"aromatech_ambience.{entry_id}")
        saved = await self._store.async_load()
        if saved is not None:
            try:
                self.estimates.restore(saved)
            except ValueError:
                _LOGGER.warning("Invalid saved usage estimates; starting fresh totals")
        from .inventory_manager import InventoryManager

        self.inventory_manager = InventoryManager(self.hass, entry_id, self.options)
        await self.inventory_manager.initialize()
        manager = self.inventory_manager
        # The checked inventory envelope is authoritative for BOTH usage and its
        # watermark. A newer delayed legacy save must never double-consume a fill.
        saved_accounting = getattr(manager, "accounting_snapshot", None)
        if saved_accounting is not None:
            try:
                self.estimates.restore(saved_accounting)
            except ValueError:
                manager.error = "Invalid accounting snapshot; inventory preserved and counting paused."
                manager._load_failed = True
                return
        elif getattr(manager, "_load_failed", False):
            return
        now = dt_util.utcnow()
        # A future watermark cannot be rewound. Retain it and expose a clock error
        # until wall time catches up, rather than silently counting it twice.
        manager.accounting_provider = self.estimates.to_dict
        if saved_accounting is not None:
            manager._startup_at = None
        await self._catchup_estimates(now, uncertain=True)
        await self._flush_inventory()

    async def _catchup_estimates(self, now, *, uncertain=False):
        # Yield during extended downtime; each chunk uses bounded memory/CPU.
        if getattr(getattr(self, "inventory_manager", None), "_load_failed", False):
            return
        while self.estimates._at is not None and now - self.estimates._at > timedelta(hours=6):
            self._change_estimates(self.estimates.advance,
                self.estimates._at + timedelta(hours=6), uncertain=uncertain)
            await asyncio.sleep(0)
        self._change_estimates(self.estimates.advance, now, uncertain=uncertain)

    def async_start_timers(self):
        """One clock for accounting and one for BLE; neither is entity-driven."""
        if self._timer_unsubscribers:
            return
        from homeassistant.helpers.event import async_track_time_interval
        self._closed = False
        self._timer_unsubscribers = [
            async_track_time_interval(self.hass, self._async_counter_tick, timedelta(seconds=30)),
            async_track_time_interval(self.hass, self._async_heartbeat, timedelta(seconds=self.scan_interval)),
        ]

    async def _async_counter_tick(self, now):
        if self._closed:
            return
        async with self._lock:
            if self._closed:
                return
            await self._catchup_estimates(now)
            self._save_estimates()
            await self._flush_inventory()
        self.async_update_listeners()

    async def _async_heartbeat(self, _now):
        if self._closed or self._heartbeat_running:
            return
        self._heartbeat_running = True
        try:
            await self.async_refresh()
        finally:
            self._heartbeat_running = False

    @property
    def bluetooth_confirmation_age(self):
        contact = self.estimates.last_contact_at
        return max(0, (dt_util.utcnow() - contact).total_seconds()) if contact else None

    @property
    def bluetooth_health(self):
        age = self.bluetooth_confirmation_age
        if self.estimates._at is not None and self.estimates._at > dt_util.utcnow():
            return "offline"
        if age is None or age > 3 * self.scan_interval or self.estimates.consecutive_failures >= 3:
            return "offline"
        if age > self.scan_interval + REPLY_TIMEOUT or self.estimates.consecutive_failures:
            return "stale"
        return "healthy"

    def _advance_estimates(self, now=None):
        if hasattr(self, "estimates"):
            self._change_estimates(self.estimates.advance, now or dt_util.utcnow())
            self._save_estimates()

    @property
    def inventory(self):
        return self.inventory_manager.inventory if self.inventory_manager else None

    def _change_estimates(self, operation, *args, **kwargs):
        manager = getattr(self, "inventory_manager", None)
        if manager and getattr(manager, "_load_failed", False):
            return None
        keys = ("expected_sprays", "active_seconds", "unobserved_seconds")
        before = [getattr(self.estimates, key) for key in keys]
        adjustment = (self.estimates.adjustment_cycles, self.estimates.adjustment_active_seconds)
        uncertain_active = self.estimates.uncertain_active_seconds
        last_event = self.estimates.adjustments[-1] if self.estimates.adjustments else None
        result = operation(*args, **kwargs)
        if manager:
            dc = self.estimates.adjustment_cycles - adjustment[0]
            da = self.estimates.adjustment_active_seconds - adjustment[1]
            manager.add_usage(max(0, self.estimates.expected_sprays - before[0] - dc),
                              max(0, self.estimates.active_seconds - before[1] - da),
                              max(0, self.estimates.unobserved_seconds - before[2]),
                              max(0, self.estimates.uncertain_active_seconds - uncertain_active))
            event = self.estimates.adjustments[-1] if self.estimates.adjustments else None
            if event is not None and event is not last_event:
                manager.add_adjustment(dc, da, event)
            manager.accounting_changed()
        return result

    async def _flush_inventory(self):
        manager = getattr(self, "inventory_manager", None)
        if manager:
            try:
                await manager.flush()
            except HomeAssistantError:
                _LOGGER.error("Bottle inventory save failed; queued usage retained for retry")

    async def async_inventory_action(self, action, **fields):
        async with self._schedule_edit_lock:
            return await self._inventory_action_locked(action, **fields)

    async def _inventory_action_locked(self, action, **fields):
        manager = self.inventory_manager
        if manager is None:
            raise HomeAssistantError("Bottle inventory is not loaded")
        if action in ("install_bottle", "store_bottle", "record_weight", "correct_weight"):
            await self.async_refresh()
        # All actions, including rate edits, settle old usage before any ownership
        # or mass anchor changes. The timer and BLE commands share this lock.
        async with self._lock:
            if action in ("install_bottle", "store_bottle", "record_weight", "correct_weight"):
                if not self.last_update_success or self._last_observation_at is None:
                    raise HomeAssistantError("A fresh complete device report is required")
                if action in ("install_bottle", "store_bottle") and self.data.power is not False:
                    raise HomeAssistantError("Turn scenting off before installing or storing a bottle")
            self._advance_estimates()
            await self._flush_inventory()
            result = await manager.execute(action, **fields)
        self.async_update_listeners()
        return result

    async def async_acknowledge_refill(self):
        await self.inventory_manager.acknowledge()
        self.async_update_listeners()

    async def async_apply_inventory_options(self, options):
        self.options = dict(options)
        self.inventory_manager.options = dict(options)
        await self._flush_inventory()
        self.async_update_listeners()

    def _save_estimates(self) -> None:
        # Once the inventory is initialized it owns the atomic accounting save.
        if self._store is not None and getattr(self, "inventory_manager", None) is None:
            self._store.async_delay_save(self.estimates.to_dict, 30)

    def _invalidate_estimates(self) -> None:
        if hasattr(self, "estimates"):
            self._change_estimates(self.estimates.invalidate, dt_util.utcnow())
            self._last_observation_at = None
            self._save_estimates()

    def _record_observation(self, state: p.State) -> None:
        """Complete reports confirm assumptions; the accounting clock is separate."""
        if not hasattr(self, "estimates"):
            return
        now = dt_util.utcnow()
        complete = (
            state.schedule_status == "complete"
            and state.power is not None
            and state.intensity_presets is not None
            and p.CMD_POWER in state.raw
            and p.CMD_INTENSITY_PRESETS in state.raw
        )
        if not complete:
            return False
        self._change_estimates(self.estimates.observe,
            now, power=state.power, schedules=state.schedules,
            presets=state.intensity_presets, complete=complete,
            run_code=state.run_code,
        )
        self._last_observation_at = now if complete and self.estimates.last_observation_at == now else None
        if getattr(self, "inventory_manager", None):
            self.inventory_manager.state = state if complete else None
        self._save_estimates()
        return True

    async def async_close(self) -> None:
        """Cancel callbacks, finish serialized accounting, then release BLE."""
        self._closed = True
        for unsubscribe in getattr(self, "_timer_unsubscribers", []):
            unsubscribe()
        self._timer_unsubscribers = []
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
        async with self._lock:
            try:
                client, self._client = self._client, None
                if client is not None and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception:
                        _LOGGER.debug("disconnect failed during unload, ignoring", exc_info=True)
            finally:
                self._advance_estimates()
                if self._store is not None and getattr(self, "inventory_manager", None) is None:
                    await self._store.async_save(self.estimates.to_dict())
                await self._flush_inventory()

    async def async_record_bottle_weight(self, gross_g: float, empty_g: float) -> None:
        async with self._schedule_edit_lock:
            await self._record_legacy_weight_locked(gross_g, empty_g)

    async def _record_legacy_weight_locked(self, gross_g: float, empty_g: float) -> None:
        """Checkpoint current totals after a fresh complete observation."""
        if getattr(self, "inventory_manager", None) and self.inventory_manager.owns_remaining:
            raise HomeAssistantError("Use bottle management Record weight for the installed bottle")
        await self.async_refresh()
        if not self.last_update_success or self._last_observation_at is None:
            raise HomeAssistantError("A complete fresh device report is required before weighing")
        async with self._lock:
            try:
                self._change_estimates(self.estimates.record_weight, dt_util.utcnow(), gross_g, empty_g)
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            if getattr(self, "inventory_manager", None) is not None:
                await self.inventory_manager.flush()
            elif self._store is not None:
                await self._store.async_save(self.estimates.to_dict())
        self.async_update_listeners()

    # ------------------------------------------------------------------ BLE

    @callback
    def _on_notify(self, _handle, data: bytearray) -> None:
        """Collect replies, and only finish on one that carries state.

        A command draws TWO replies, not one. The first echoes the command back
        - a time sync returns a single 0x000a message holding the time just
        sent - and the real state arrives immediately after as a separate
        eight-message batch. Resolving on the first reply therefore returns a
        State with every field None, which is precisely what left the entities
        at unknown: the receipt was taken for the answer.

        So replies are merged as they arrive and the wait ends only once
        something usable is present. If nothing usable turns up, the timeout in
        `_exchange` returns whatever accumulated rather than nothing.
        """
        self._trace("RX", bytes(data))
        state = self._reassembler.feed(bytes(data))
        if state is None:
            return
        self._accumulated = _merge(self._accumulated, state)
        if state.schedule_status == "partial":
            # The observed short ACK also matches the first fragment of a
            # full single-command report. Keep collecting its continuation;
            # a truly short ACK is returned by the exchange timeout.
            return
        self._reassembler = p.Reassembler()  # ready for the next reply

        if not _carries_state(self._accumulated):
            return
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(self._accumulated)

    @callback
    def _trace(self, direction: str, data: bytes) -> None:
        """Record a local diagnostic frame with timestamp and command identifier."""
        # Both directions put the command at bytes 4-5 of the FIRST fragment
        # only: a write is `25 <frag> ff 01 <cmd:2> ...` and a reply is
        # `<seq> <frag> <count:2> <cmd:2> ...`. Continuation fragments carry no
        # command, so they are left untagged rather than mislabelled.
        cmd = ""
        if len(data) >= 6 and data[1] == 0x01:
            cmd = f" cmd={(data[4] << 8) | data[5]:#06x}"
        stamp = dt_util.utcnow().strftime("%H:%M:%S.%f")[:-3]
        self.trace.append(f"{stamp} {direction}{cmd} {data.hex(' ')}")

    async def _connect(self) -> tuple[BleakClient, bool]:
        """Connect and subscribe, retrying transient GATT failures.

        Return the client and whether a fresh link needs synchronization.
        A failed subscription closes the connection before service rediscovery."""
        if self._client is not None and self._client.is_connected:
            return self._client, False

        last_error: Exception | None = None
        for attempt in range(CONNECT_ATTEMPTS):
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                raise UpdateFailed(
                    f"{self.address} was not heard by any Bluetooth proxy "
                    "recently. This device advertises intermittently, so a "
                    "miss is not necessarily a fault."
                )

            client = await establish_connection(
                BleakClient,
                device,
                self.address,
                max_attempts=2,
                # Rediscover on a retry: a wrong cached handle is a prime
                # suspect for 133, and reusing the cache would repeat it.
                use_services_cache=attempt == 0,
            )
            try:
                await asyncio.sleep(NOTIFY_SETTLE)
                await client.start_notify(p.NOTIFY_UUID, self._on_notify)
            except Exception as err:  # noqa: BLE001 - retried below
                last_error = err
                _LOGGER.debug(
                    "start_notify failed on attempt %s/%s: %s",
                    attempt + 1, CONNECT_ATTEMPTS, err,
                )
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    pass
                await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
                continue

            self._client = client
            return client, True

        raise UpdateFailed(
            f"could not enable notifications on {self.address} after "
            f"{CONNECT_ATTEMPTS} attempts: {last_error}"
        )

    async def _disconnect_later(self) -> None:
        try:
            await asyncio.sleep(DISCONNECT_DELAY)
        except asyncio.CancelledError:
            return
        async with self._lock:
            client, self._client = self._client, None
            if client is not None and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    _LOGGER.debug("disconnect failed, ignoring", exc_info=True)

    def _schedule_disconnect(self) -> None:
        if self._disconnect_task is not None and not self._disconnect_task.done():
            self._disconnect_task.cancel()
        self._disconnect_task = self.hass.async_create_background_task(
            self._disconnect_later(), "ambience-disconnect"
        )

    # -------------------------------------------------------------- commands

    async def _write_and_wait(
        self, client: BleakClient, frames: list[bytes]
    ) -> p.State | None:
        """Write one command and return whatever state the device reports."""
        self._reassembler = p.Reassembler()
        self._accumulated = None
        self._reply = self.hass.loop.create_future()
        try:
            for frame in frames:
                self._trace("TX", frame)
                await client.write_gatt_char(p.WRITE_UUID, frame, response=True)
            try:
                return await asyncio.wait_for(
                    asyncio.shield(self._reply), REPLY_TIMEOUT
                )
            except asyncio.TimeoutError:
                # Not necessarily a failure: only 0x03e9 and the time sync were
                # ever seen to answer, so light and sound appear to be
                # fire-and-forget. Return anything that did arrive.
                _LOGGER.debug("no usable reply within %ss", REPLY_TIMEOUT)
                return self._accumulated
        finally:
            self._reply = None

    async def _exchange(
        self,
        frames: list[bytes] | None = None,
        *,
        build: Callable[[p.State | None], list[bytes]] | None = None,
        handshake: bool = True,
        affects_usage: bool = True,
    ) -> p.State | None:
        """Synchronize a fresh link before sending a command.

        Schedule edits use the state returned by synchronization as their
        read-modify-write base, preserving other slots and unknown bytes."""
        async with self._lock:
            if getattr(self, "_closed", False):
                raise UpdateFailed("Integration is unloading")
            client, fresh = await self._connect()
            try:
                current = self.data
                if fresh and handshake:
                    greeting = await self._write_and_wait(
                        client, p.sync_time(dt_util.now())
                    )
                    if _carries_state(greeting):
                        self._record_observation(greeting)
                        current = _merge(current, greeting)
                        self.async_set_updated_data(current)
                payload = frames if frames is not None else build(current)
                if handshake and affects_usage:
                    self._begin_usage_command(payload, current)
                reported = await self._write_and_wait(client, payload)
                if reported is not None:
                    self._record_observation(reported)
                return reported
            except Exception:
                if handshake and affects_usage and hasattr(self, "estimates"):
                    self._change_estimates(self.estimates.fail_command, dt_util.utcnow())
                raise
            finally:
                self._schedule_disconnect()
                await self._flush_inventory()

    def _begin_usage_command(self, frames, current):
        if not hasattr(self, "estimates") or current is None:
            return
        body = b"".join(frame[2:] for frame in frames)
        if len(body) < 7 or int.from_bytes(body[2:4], "big") != p.CMD_POWER:
            return
        payload = body[7:]
        power, schedules = current.power, current.schedules
        if len(payload) == 5:
            power = payload[1] == p.POWER_ON_ARG
        else:
            schedules = p.parse_schedules(payload)
        self._change_estimates(self.estimates.begin_command, dt_util.utcnow(),
            power=power, schedules=schedules, presets=current.intensity_presets)

    async def _fail_pending_command(self):
        async with self._lock:
            if hasattr(self, "estimates") and self.estimates.command_status == "pending":
                self._change_estimates(self.estimates.fail_command, dt_util.utcnow())
                await self._flush_inventory()

    async def _command(
        self,
        frames: list[bytes] | None = None,
        *,
        build: Callable[[p.State | None], list[bytes]] | None = None,
        affects_usage: bool = True,
    ) -> None:
        """Send a command, then refresh so entities reflect the device."""
        reported = await self._exchange(frames, build=build, affects_usage=affects_usage)
        if _carries_state(reported):
            self.async_set_updated_data(_merge(self.data, reported))
        else:
            await self.async_request_refresh()
        if affects_usage and hasattr(self, "estimates") and self.estimates.command_status == "pending":
            await self.async_refresh()
            if self.estimates.command_status == "pending":
                await self._fail_pending_command()
                raise HomeAssistantError("Device did not confirm the scenting change; previous counting settings retained")

    async def async_set_power(self, on: bool) -> None:
        # Keep begin/write/readback/outcome indivisible against other usage changes.
        async with self._schedule_edit_lock:
            await self._command(p.power(on))

    async def async_set_sound(self, on: bool) -> None:
        await self._command(p.sound(on), affects_usage=False)

    async def async_set_light(
        self,
        mode: int,
        rgb: tuple[int, int, int] | None = None,
        brightness: int | None = None,
    ) -> None:
        current = self.data
        if rgb is None:
            rgb = current.rgb if current and current.rgb else (0, 0, 0)
        if brightness is None:
            brightness = current.brightness if current and current.brightness else 0x64
        await self._command(p.light(mode, rgb=rgb, brightness=brightness), affects_usage=False)

    async def async_set_schedule(
        self,
        *,
        slot: int = 1,
        allow_append: bool = False,
        enabled: bool | None = None,
        day_bit: int | None = None,
        day_on: bool | None = None,
        day_bits: int | None = None,
        days_mask: int | None = None,
        start_hour: int | None = None,
        start_minute: int | None = None,
        end_hour: int | None = None,
        end_minute: int | None = None,
        intensity: int | None = None,
    ) -> None:
        """Edit a one-based slot and confirm every retained slot on fresh readback."""
        changes = {key: value for key, value in {
            "days_mask": days_mask, "start_hour": start_hour,
            "start_minute": start_minute, "end_hour": end_hour,
            "end_minute": end_minute, "intensity": intensity,
            "enabled": enabled, "day_bits": day_bits,
            "day_bit": day_bit, "day_on": day_on,
        }.items() if value is not None}
        if not changes:
            raise HomeAssistantError("Supply at least one schedule field")
        if intensity is not None and (type(intensity) is not int or not 1 <= intensity <= 4):
            raise HomeAssistantError("New intensity writes must be 1–4; level 5 is not hardware validated")
        await self._edit_schedules(slot, changes, allow_append=allow_append)

    async def async_set_always_on(self, enabled=True) -> None:
        """Journal the timed list before changing mode; restore only our own write."""
        if type(enabled) is not bool:
            raise HomeAssistantError("Always on must be a boolean")
        if enabled and not self.experimental_schedule_writes:
            raise HomeAssistantError(
                "Enable Experimental schedule writes in the AromaTech integration options to use Always on")
        mode = getattr(self, "schedule_mode", None)
        if mode is None or mode.error:
            raise HomeAssistantError(mode.error if mode else "Previous schedule storage is not loaded")
        async with self._schedule_edit_lock:
            await self.async_refresh()
            current = self._fresh_schedule_list()
            if not current:
                raise HomeAssistantError("Create a timed schedule before using Always on")
            backup = mode.backup
            try:
                if enabled:
                    if backup:
                        previous, active = decode_mode(backup["previous"]), decode_mode(backup["active"])
                        if backup["restoring"] is not None:
                            raise HomeAssistantError("Finish turning Always on off before enabling it again")
                        if same_timing(current, active):
                            return
                        if not same_timing(current, previous):
                            raise HomeAssistantError("Device schedule changed outside Always on; saved schedule was not overwritten")
                    else:
                        if is_full_day(current):
                            raise HomeAssistantError("Already all day, but no saved timed schedule is available to restore")
                        previous = current
                    target = [replace(current[0], days_mask=255, start_hour=8,
                                      start_minute=0, end_hour=8, end_minute=0)]
                    target += [replace(slot, days_mask=slot.days_mask & 127) for slot in previous[1:]]
                    backup = {"previous": encode_mode(previous), "active": encode_mode(target), "restoring": None}
                    await mode.save(backup)  # Checked disk persistence must precede BLE.
                    await self._replace_mode_schedules(current, target)
                else:
                    if not backup:
                        if is_full_day(current):
                            raise HomeAssistantError("No saved timed schedule to restore; set a timed schedule in the vendor app first")
                        return
                    previous, active = decode_mode(backup["previous"]), decode_mode(backup["active"])
                    restoring = decode_mode(backup["restoring"]) if backup["restoring"] else None
                    if same_timing(current, previous) or (restoring is not None and current == restoring):
                        await mode.save(None)  # Completed restore or a failed activation.
                    else:
                        if not same_timing(current, active):
                            raise HomeAssistantError("Device schedule changed outside Always on; refusing to overwrite it")
                        target = [replace(previous[0], intensity=current[0].intensity), *previous[1:]]
                        await mode.save({**backup, "restoring": encode_mode(target)})
                        await self._replace_mode_schedules(current, target)
                        await mode.save(None)
            except (OSError, ValueError, RuntimeError) as err:
                raise HomeAssistantError(f"Always on operation could not safely complete: {err}") from err
            finally:
                self.async_update_listeners()

    def _fresh_schedule_list(self):
        state = self.data
        if (not self.last_update_success or state is None or state.schedule_status != "complete"
                or state.schedule_raw is None or state.schedules is None):
            raise HomeAssistantError("A fresh complete schedule report is required")
        return list(state.schedules)

    async def _replace_mode_schedules(self, original, expected):
        """Called under schedule lock, preserves every other command's power state."""
        def build(current):
            if (current is None or current.schedule_status != "complete"
                    or current.schedule_raw is None or current.schedules is None):
                raise HomeAssistantError("No complete fresh schedule snapshot; refusing mode change")
            if list(current.schedules) not in (original, expected):
                raise HomeAssistantError("Schedule changed during mode update; refusing to overwrite it")
            return p.encode_schedules(expected)

        for attempt in range(SCHEDULE_WRITE_ATTEMPTS):
            reported = await self._exchange(build=build)
            if _carries_state(reported):
                self.async_set_updated_data(_merge(self.data, reported))
            await self.async_refresh()
            if self.last_update_success and self.data is not None and self.data.schedule_status == "complete" and self.data.schedules == expected:
                return
            if attempt < SCHEDULE_WRITE_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BACKOFF)
        await self._fail_pending_command()
        raise HomeAssistantError("Device did not retain Always on schedule after fresh readback; saved schedule retained")

    async def async_delete_schedule(self, slot: int) -> None:
        await self._edit_schedules(slot, {}, delete=True)

    async def _edit_schedules(self, slot, changes, *, allow_append=False, delete=False):
        if type(slot) is not int or not 1 <= slot <= 5:
            raise HomeAssistantError("Schedule slot must be 1–5")
        async with self._schedule_edit_lock:
            await self.async_refresh()
            if not self.last_update_success:
                raise HomeAssistantError("A fresh schedule report is required")
            mode = getattr(self, "schedule_mode", None)
            if (self.always_on or (mode is not None and mode.backup is not None)) and not (
                slot == 1 and set(changes) == {"intensity"} and not delete and not allow_append
            ):
                raise HomeAssistantError("Turn Always on off to restore and edit the timed schedule")
            original = None
            expected = None

            def build(current):
                nonlocal original, expected
                if (current is None or current.schedule_status != "complete"
                        or current.schedule_raw is None or current.schedules is None):
                    raise HomeAssistantError("No complete parsed schedule snapshot; refusing to rewrite")
                actual = list(current.schedules)
                if is_full_day(actual) and not (
                    slot == 1 and set(changes) == {"intensity"} and not delete and not allow_append
                ):
                    raise HomeAssistantError("Turn Always on off before editing the timed schedule")
                if original is not None:
                    if actual != original and actual != expected:
                        raise HomeAssistantError("Schedule changed during retry; refresh before editing again")
                    return p.encode_schedules(expected)
                original = actual
                append = slot == len(actual) + 1 and allow_append and not delete
                if not append and slot > len(actual):
                    raise HomeAssistantError("Slot does not exist; append only at the next slot")
                managed_intensity = (mode is not None and mode.backup is not None
                                     and slot == 1 and set(changes) == {"intensity"}
                                     and same_timing(actual, decode_mode(mode.backup["active"])))
                if (delete or append or len(actual) > 1) and not self.experimental_schedule_writes and not managed_intensity:
                    raise HomeAssistantError("Multi-slot/append/delete writes require the experimental schedule option")
                expected = list(actual)
                try:
                    fields = dict(changes)
                    enabled = fields.pop("enabled", None)
                    day_bits = fields.pop("day_bits", None)
                    day_bit = fields.pop("day_bit", None)
                    day_on = fields.pop("day_on", None)
                    mask = 0 if append else actual[slot - 1].days_mask
                    if day_bit is not None or day_on is not None:
                        if type(day_bit) is not int or not 0 <= day_bit <= 6 or type(day_on) is not bool:
                            raise ValueError("A day toggle requires a bit 0–6 and boolean")
                        mask = mask | (1 << day_bit) if day_on else mask & ~(1 << day_bit)
                    if day_bits is not None:
                        if type(day_bits) is not int or not 0 <= day_bits <= 127:
                            raise ValueError("Day mask must be 0–127")
                        mask = (mask & 0x80) | day_bits
                    if enabled is not None:
                        if type(enabled) is not bool:
                            raise ValueError("Enabled must be a boolean")
                        mask = (mask | 0x80) if enabled else (mask & 0x7F)
                    if enabled is not None or day_bits is not None or day_bit is not None:
                        fields["days_mask"] = mask
                    if delete:
                        del expected[slot - 1]
                    elif append:
                        required = {"days_mask", "start_hour", "start_minute", "end_hour", "end_minute", "intensity"}
                        if not required.issubset(fields) or enabled is None or day_bits is None:
                            raise ValueError("A new slot requires days, start, end and intensity")
                        expected.append(p.Schedule(**fields))
                    else:
                        expected[slot - 1] = replace(actual[slot - 1], **fields)
                    if not delete and (append or any(key in changes for key in (
                            "start_hour", "start_minute", "end_hour", "end_minute",
                            "days_mask", "day_bits", "day_bit", "enabled"))):
                        _validate_edited_window(expected, slot - 1, allow_full_day=self.experimental_schedule_writes, check_window=append or any(
                            key in changes for key in ("start_hour", "start_minute", "end_hour", "end_minute")))
                    if is_full_day(expected) and not is_full_day(actual):
                        raise ValueError("Use the Always on switch so the timed schedule is saved before enabling all day")
                    return p.encode_schedules(expected)
                except (ValueError, TypeError) as err:
                    raise HomeAssistantError(str(err)) from err

            for attempt in range(SCHEDULE_WRITE_ATTEMPTS):
                reported = await self._exchange(build=build)
                if _carries_state(reported):
                    self.async_set_updated_data(_merge(self.data, reported))
                # A transport ACK never confirms a save. Always compare full list.
                await self.async_refresh()
                if (self.last_update_success and self.data is not None
                        and self.data.schedule_status == "complete"
                        and self.data.schedules == expected):
                    return
                if attempt < SCHEDULE_WRITE_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_BACKOFF)
            await self._fail_pending_command()
            raise HomeAssistantError("Ambience did not retain schedule change after fresh full-list readback")

    # --------------------------------------------------------------- polling

    async def _async_update_data(self) -> p.State:
        """Ask for state by doing what the app does on connect.

        A time sync is a write, but it is the write the vendor app itself
        sends every time it connects, and the device answers with its whole
        state. Nothing else in the protocol reads without writing.
        """
        try:
            state = await self._exchange(p.sync_time(dt_util.now()), handshake=False)
            if state is None or state.power is None or state.schedule_status != "complete":
                raise UpdateFailed("Device did not return a complete parsed schedule snapshot")
            if (state.intensity_presets is None or p.CMD_POWER not in state.raw
                    or p.CMD_INTENSITY_PRESETS not in state.raw):
                raise UpdateFailed("Device did not return complete operating assumptions")
            # _exchange confirmed this report under the shared accounting lock.
            # Replaying it here could cross a bottle mutation awaiting disk I/O.
        except Exception:
            async with self._lock:
                if getattr(self, "_closed", False):
                    raise
                self._invalidate_estimates()
                if getattr(self, "inventory_manager", None):
                    self.inventory_manager.state = None
                await self._flush_inventory()
            raise
        return _merge(self.data, state)


def _carries_state(state: p.State | None) -> bool:
    """Does this reply actually tell us anything about the device?

    Deliberately not `state is not None`. The echo reply parses perfectly well
    into a State object with every field unset, and treating that as an answer
    is the bug this guards against.
    """
    if state is None:
        return False
    return any(v is not None for v in (
        state.power, state.light_mode, state.sound, state.days_mask, state.schedules,
    ))


def _merge(old: p.State | None, new: p.State) -> p.State:
    """Overlay a partial reply onto what is already known.

    A reply to a power command carries only the 0x03e9 message, so light,
    sound and firmware would otherwise be blanked on every toggle.
    """
    if old is None:
        return new
    merged = p.State(**{**old.__dict__, **{
        k: v for k, v in new.__dict__.items() if v not in (None, {}, [])
    }})
    merged.raw = {**old.raw, **new.raw}
    schedule_fields = ("schedules", "schedule_raw", "days_mask", "start_hour",
                       "start_minute", "end_hour", "end_minute", "intensity")
    if new.schedule_status == "complete":
        for key in schedule_fields:
            setattr(merged, key, getattr(new, key))
        merged.schedule_status = "complete"
    elif new.schedule_status == "unparsed":
        for key in schedule_fields:
            setattr(merged, key, None)
        merged.schedule_status = "unparsed"
    else:
        # ACK values belong in diagnostics, never in the authoritative write base.
        for key in schedule_fields:
            setattr(merged, key, getattr(old, key))
        merged.schedule_status = old.schedule_status
    return merged


def _validate_edited_window(schedules, index, *, check_window=True, allow_full_day=False):
    edited = schedules[index]
    start = edited.start_hour * 60 + edited.start_minute
    end = edited.end_hour * 60 + edited.end_minute
    if start == end and start != 0 and allow_full_day:
        start, end = 0, 1440
    if (check_window or edited.days_mask & 0x80) and end <= start:
        raise ValueError("New schedule windows must end after start on the same day")
    if not edited.days_mask & 0x80:
        return
    for position, other in enumerate(schedules):
        if position == index or not other.days_mask & 0x80:
            continue
        if not (edited.days_mask & other.days_mask & 0x7F):
            continue
        other_start = other.start_hour * 60 + other.start_minute
        other_end = other.end_hour * 60 + other.end_minute
        if other_start == other_end and other_start != 0:
            other_start, other_end = 0, 1440
        if other_end <= other_start or max(start, other_start) < min(end, other_end):
            raise ValueError("Edited enabled schedule overlaps another slot or an ambiguous overnight window")
