"""Serialized, checked persistence and notifications for the bottle ledger."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import logging
import json
import math

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .inventory import Inventory
from .inventory_store import InventoryStore

_LOGGER = logging.getLogger(__name__)
_SEVERITY = {"refill_soon": 1, "low": 2, "needs_weighing": 2, "estimated_depleted": 3}


class InventoryManager:
    def __init__(self, hass, entry_id, options=None):
        self.hass = hass
        self.entry_id = entry_id
        self.inventory = Inventory()
        self.store = InventoryStore(hass, entry_id)
        self.options = dict(options or {})
        self.state = None
        self.error = None
        self._load_failed = False
        self._lock = asyncio.Lock()
        self._pending = []
        self._alerts = {"key": None, "acknowledged": False, "notified": False}
        self._dirty = False
        self._observed_at = None
        self._startup_at = None
        self._restore_notification = False
        self._revision = 0
        self.accounting_snapshot = None
        self.accounting_provider = None
        self.audit = []

    @property
    def owns_remaining(self):
        """A failed inventory load must never reveal unrelated legacy mass."""
        return self.inventory.initialized or self._load_failed

    async def initialize(self):
        try:
            data = await self.store.async_load()
            if data is not None:
                if not isinstance(data, dict) or data.get("version") not in (1, 2):
                    raise ValueError("Unsupported inventory envelope")
                if data["version"] == 2 and not isinstance(data.get("accounting"), dict):
                    raise ValueError("Atomic accounting snapshot is missing")
                self.inventory.restore(data["inventory"])
                alerts = data.get("alerts")
                if (not isinstance(alerts, dict) or set(alerts) != {"key", "acknowledged", "notified"}
                        or (alerts["key"] is not None and not isinstance(alerts["key"], str))
                        or type(alerts["acknowledged"]) is not bool or type(alerts["notified"]) is not bool):
                    raise ValueError("Invalid saved notification state")
                self._alerts = alerts
                stamp = data.get("observed_at")
                now = dt_util.utcnow()
                elapsed = None
                if stamp is not None:
                    previous = datetime.fromisoformat(stamp)
                    if previous.tzinfo is None or previous.utcoffset() is None:
                        raise ValueError("Invalid saved observation timestamp")
                    if previous <= now:
                        elapsed = (now - previous).total_seconds()
                self.accounting_snapshot = data.get("accounting")
                self.audit = data.get("audit", [])
                if not isinstance(self.audit, list) or len(self.audit) > 500:
                    raise ValueError("Invalid accounting audit")
                # Audit is persisted evidence, not unchecked UI text.
                json.dumps(self.audit, allow_nan=False)
                for event in self.audit:
                    if (not isinstance(event, dict) or not isinstance(event.get("reason"), str)
                            or not all(isinstance(event.get(key), str) and event[key]
                                       for key in ("bottle_id", "fill_id"))):
                        raise ValueError("Invalid accounting audit identity")
                    timestamp = datetime.fromisoformat(event["at"])
                    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                        raise ValueError("Invalid accounting audit timestamp")
                    for key in ("cycles", "active_seconds", "remaining_adjustment_g", "consumed_g"):
                        value = event.get(key)
                        if value is not None and (isinstance(value, bool)
                                or not isinstance(value, (float, int)) or not math.isfinite(value)):
                            raise ValueError("Invalid accounting audit amount")
                if self.accounting_snapshot is None:
                    self.inventory.mark_restart(unknown_seconds=elapsed)
                    self._startup_at = now
                self._observed_at = now.isoformat()
                self._dirty = True
                self._restore_notification = bool(alerts["key"] and not alerts["acknowledged"])
        except (ValueError, KeyError, TypeError, OSError) as err:
            self.error = "Inventory could not be loaded; preserved on disk. Repair or restore its backup before editing."
            self._load_failed = True
            _LOGGER.error("Inventory load failed; writes disabled: %s", type(err).__name__)

    def summary(self, inventory=None, *, include_error=True):
        inventory = inventory or self.inventory
        state = self.state
        result = inventory.summary(
            schedules=state.schedules if state and state.schedule_status == "complete" else None,
            presets=state.intensity_presets if state else None,
            refill_days=self.options.get("refill_days", 7),
            low_g=self.options.get("low_oil_g", 5),
            depleted_g=self.options.get("depleted_oil_g", 0),
        )
        if self.error and include_error:
            result.update(remaining_g=None, remaining_percent=None, daily_g=None,
                          days_remaining=None, status="needs_weighing", confidence="storage_error")
        return result

    def add_usage(self, cycles, active_seconds, unknown_seconds, uncertain_active_seconds=0):
        """Queue against the committed bottle identity, including during a save."""
        ident = self.inventory.active_bottle_id
        now = dt_util.utcnow()
        if self._startup_at is not None:
            unknown_seconds += max(0, (now - self._startup_at).total_seconds())
            self._startup_at = None
        stamp = now.isoformat()
        if ident is not None and stamp != self._observed_at:
            self._dirty = True
            self._revision += 1
        self._observed_at = stamp
        if ident is not None and any((cycles, active_seconds, unknown_seconds)):
            fill = self.inventory.bottles[ident]["fills"][-1]
            self._pending.append((ident, fill["id"], fill["rate_g_per_second"],
                                  cycles, active_seconds, unknown_seconds, None, uncertain_active_seconds))

    def add_adjustment(self, cycles, active_seconds, event):
        ident = self.inventory.active_bottle_id
        if ident is not None:
            fill = self.inventory.bottles[ident]["fills"][-1]
            self._pending.append((ident, fill["id"], fill["rate_g_per_second"],
                                  cycles, active_seconds, 0, deepcopy(event), 0))
            self._dirty = True
            self._revision += 1

    def accounting_changed(self):
        self._dirty = True
        self._revision += 1

    def _candidate(self):
        candidate = Inventory()
        candidate.restore(self.inventory.to_dict())
        return candidate

    def _apply_pending(self, candidate, count):
        selected = candidate.active_bottle_id
        for ident, fill_id, rate, cycles, active, unknown, event, uncertain_active in self._pending[:count]:
            if ident not in candidate.bottles:
                raise ValueError("Usage references a missing bottle")
            bottle = candidate.bottles[ident]
            position = next((i for i, fill in enumerate(bottle["fills"]) if fill["id"] == fill_id), None)
            if position is None:
                raise ValueError("Usage references a missing fill")
            # apply_usage operates on the selected final fill; preserve exact captured
            # ownership/rate even if a refill/rate edit was committed while saving.
            fills = bottle["fills"]
            fill = fills[position]
            previous_rate = fill["rate_g_per_second"]
            bottle["fills"] = [fill]
            fill["rate_g_per_second"] = rate
            candidate.active_bottle_id = ident
            if event is None:
                candidate.apply_usage(cycles, active, unknown, uncertain_active)
            else:
                candidate.apply_adjustment(cycles, active, rate)
            updated = bottle["fills"][0]
            updated["rate_g_per_second"] = previous_rate
            fills[position] = updated
            bottle["fills"] = fills
        candidate.active_bottle_id = selected

    def _alert_plan(self, candidate):
        summary = self.summary(candidate, include_error=False)
        status, fill_id = summary["status"], summary["fill_id"]
        enabled = self.options.get("refill_notifications", False)
        if enabled and status == "forecast_unavailable" and fill_id is not None:
            # Missing forecast evidence is not recovery. Preserve the existing
            # alert/ack until a complete forecast can establish a new status.
            if (self._alerts["key"] or "").startswith(f"{fill_id}:"):
                effect = "notify" if self._restore_notification and not self._alerts["acknowledged"] else None
                return deepcopy(self._alerts), effect
        if not enabled or status not in _SEVERITY or fill_id is None:
            return {"key": None, "acknowledged": False, "notified": False}, "dismiss" if self._alerts["key"] else None
        key = f"{fill_id}:{status}"
        if key == self._alerts["key"]:
            effect = "notify" if self._restore_notification and not self._alerts["acknowledged"] else None
            return deepcopy(self._alerts), effect
        previous = self._alerts["key"]
        old_fill, _, old_status = (previous or "").partition(":")
        notify = (old_fill != fill_id or _SEVERITY[status] > _SEVERITY.get(old_status, 0)
                  or status == "needs_weighing" or old_status == "needs_weighing")
        # A suppressed downgrade is handled, including after restart. A new
        # severity increase or a resolved weighing warning still rearms it.
        return {"key": key, "acknowledged": not notify, "notified": True}, "notify" if notify else "dismiss"

    def _publish(self, effect):
        if effect is None:
            return
        from homeassistant.components import persistent_notification

        notification_id = f"aromatech_ambience_refill_{self.entry_id}"
        if effect == "dismiss":
            persistent_notification.async_dismiss(self.hass, notification_id)
        else:
            summary = self.summary()
            alert_status = (self._alerts["key"] or "").partition(":")[2]
            persistent_notification.async_create(
                self.hass,
                f"{summary['active_bottle']}: {alert_status.replace('_', ' ')}. "
                "Oil remaining is estimated. Weigh the bottle before relying on depletion. "
                "Acknowledge through the integration's bottle management menu.",
                title="AromaTech bottle refill", notification_id=notification_id,
            )

    async def _commit(self, candidate, count, *, acknowledge=False, force=False):
        alerts, effect = self._alert_plan(candidate)
        if acknowledge:
            alerts["acknowledged"] = True
            effect = "dismiss"
        if not force and not self._dirty and not count and alerts == self._alerts and effect is None:
            return
        audit = list(self.audit)
        for ident, fill_id, rate, cycles, active, unknown, event, uncertain_active in self._pending[:count]:
            if event is not None:
                audit.append({**event, "bottle_id": ident, "fill_id": fill_id,
                              "consumed_g": active * rate if rate is not None else None})
        if getattr(self, "_action_audit", None) is not None:
            audit.append(self._action_audit)
        audit = audit[-500:]
        removed_bottle = getattr(self, "_removed_bottle", None)
        if removed_bottle is not None:
            audit = [event for event in audit if event.get("bottle_id") != removed_bottle]
        accounting = self.accounting_provider() if self.accounting_provider else self.accounting_snapshot
        payload = {"version": 2 if accounting is not None else 1,
                   "inventory": candidate.to_dict(), "alerts": alerts,
                   "observed_at": self._observed_at, "accounting": accounting, "audit": audit}
        revision = self._revision
        try:
            await self.store.async_save(payload)
        except (OSError, ValueError, RuntimeError) as err:
            self._dirty = True
            self.error = "Inventory could not be saved; operation not applied. Check storage and retry."
            raise HomeAssistantError(self.error) from err
        # Commit only after checked disk persistence; queued observations during
        # the await remain queued with their original bottle identity.
        self.inventory = candidate
        self.accounting_snapshot = accounting
        self.audit = audit
        del self._pending[:count]
        self._alerts = alerts
        self._dirty = self._revision != revision
        self.error = None
        self._restore_notification = False
        self._publish(effect)

    async def flush(self):
        if self._load_failed:
            return
        async with self._lock:
            while True:
                candidate, count = self._candidate(), len(self._pending)
                self._apply_pending(candidate, count)
                await self._commit(candidate, count)
                if not self._pending and not self._dirty:
                    return

    async def execute(self, action, **fields):
        if self._load_failed:
            raise HomeAssistantError(self.error)
        async with self._lock:
            candidate, count = self._candidate(), len(self._pending)
            try:
                self._apply_pending(candidate, count)
                result = candidate.execute(action, dt_util.utcnow(), **fields)
            except ValueError as err:
                raise HomeAssistantError(str(err)) from err
            # A concurrent usage delta must never become orphaned by deletion.
            if action == "delete_bottle" and fields.get("bottle_id") == self.inventory.active_bottle_id:
                raise HomeAssistantError("Store the active bottle before deletion")
            self._action_audit = None
            self._removed_bottle = fields.get("bottle_id") if action == "delete_bottle" else None
            if action in ("record_weight", "correct_weight"):
                ident = fields.get("bottle_id", candidate.active_bottle_id)
                before = self._candidate()
                self._apply_pending(before, count)
                old = before.bottles[ident]["fills"][-1]
                new = candidate.bottles[ident]["fills"][-1]
                self._action_audit = {"at": dt_util.utcnow().isoformat(), "reason": action,
                    "bottle_id": ident, "fill_id": new["id"],
                    "remaining_adjustment_g": new["remaining_g"] - old["remaining_g"],
                    "previous_remaining_g": old["remaining_g"], "measured_remaining_g": new["remaining_g"]}
            try:
                await self._commit(candidate, count, force=True)
            finally:
                self._action_audit = None
                self._removed_bottle = None
        return result

    async def acknowledge(self):
        if self._load_failed:
            raise HomeAssistantError(self.error)
        async with self._lock:
            candidate, count = self._candidate(), len(self._pending)
            self._apply_pending(candidate, count)
            await self._commit(candidate, count, acknowledge=True, force=True)

    def export(self):
        return {"inventory": self.inventory.to_dict(), "summary": self.summary(),
                "storage_error": self.error, "alert_acknowledged": self._alerts["acknowledged"],
                "accounting_audit": deepcopy(self.audit)}
