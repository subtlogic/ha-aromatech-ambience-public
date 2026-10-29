"""Conservative expected usage, not measured spray events or oil volume.

Weekdays retain the integration's inferred Sunday=bit0 mapping. One measured
g/active-second rate is assumed across intensities. Weight checkpoints accompany
fresh BLE observations; they are not exact physical spray timestamps.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

UTC = timezone.utc
_FIELDS = ("days_mask", "start_hour", "start_minute", "end_hour", "end_minute", "intensity")


def _number(value, *, positive=False, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Expected a finite number")
    try:
        result = float(value)
    except OverflowError as err:
        raise ValueError("Expected a finite number") from err
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError("Expected a finite nonnegative number")
    if maximum is not None and result > maximum:
        raise ValueError("Value exceeds permitted maximum")
    return result


def _instant(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return now.astimezone(UTC)


@dataclass(frozen=True)
class _Inputs:
    power: bool
    schedules: tuple
    presets: tuple


def _inputs(power, schedules, presets):
    if power is not True and power is not False:
        return None
    if power is False:
        return _Inputs(False, (), ())
    if schedules is None or presets is None:
        return None
    try:
        slots = []
        for schedule in schedules:
            if isinstance(schedule, (tuple, list)):
                if len(schedule) not in (6, 7):
                    return None
                values = tuple(schedule[:6])
                unknown = bytes(schedule[6]) if len(schedule) == 7 else b""
            else:
                values = tuple(getattr(schedule, key) for key in _FIELDS)
                unknown = bytes(next((getattr(schedule, key) for key in
                                      ("unknownbytes", "unknown", "reserved")
                                      if hasattr(schedule, key)), b""))
            if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
                return None
            mask, sh, sm, eh, em, intensity = values
            if not (0 <= mask <= 255 and 0 <= sh <= 23 and 0 <= eh <= 23
                    and 0 <= sm <= 59 and 0 <= em <= 59 and 1 <= intensity <= 5):
                return None
            slots.append((*values, unknown))
        if len(slots) > 5:
            return None
        timings = tuple((_number(on, positive=True), _number(pause)) for on, pause in presets)
        if not timings or len(timings) > 5:
            return None
        if any(not math.isfinite(on + pause) for on, pause in timings):
            return None
        return _Inputs(True, tuple(slots), timings)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


class UsageEstimator:
    """Continuous elapsed-time estimates, independently confirmed by Bluetooth."""

    def __init__(self, time_zone: str, max_gap_seconds: float):
        self.time_zone = time_zone
        self._zone = ZoneInfo(time_zone)
        self.max_gap_seconds = _number(max_gap_seconds, positive=True)
        self.expected_sprays = 0.0
        self.active_seconds = 0.0
        self.observed_seconds = 0.0
        self.unobserved_seconds = 0.0
        self._at = None
        self._inputs = None
        self.last_observation_at = None
        self.last_run_code = None
        self._checkpoint = None
        self._rate = None
        self._needs_reanchor = False
        self.calibration_status = "needs_first_weight"
        self.last_contact_at = None
        self.consecutive_failures = 0
        self._uncertain = False
        self._confirmed_observed = 0.0
        self.uncertain_active_seconds = 0.0
        self._confirmed_active = 0.0
        self._confirmed_uncertain_active = 0.0
        self.adjustment_cycles = 0.0
        self.adjustment_active_seconds = 0.0
        self.adjustments = []
        self.command_status = "idle"
        self._pending_command = None

    @property
    def coverage(self):
        total = self.observed_seconds + self.unobserved_seconds
        return self.observed_seconds / total if total else None

    @property
    def unknown_seconds_since_checkpoint(self):
        if self._checkpoint is None or self._needs_reanchor:
            return None
        return max(0.0, self.unobserved_seconds - self._checkpoint["unobserved_seconds"])

    @property
    def grams_per_active_second(self):
        return self._rate

    @property
    def estimated_remaining_g(self):
        if self._checkpoint is None or self._rate is None or self._needs_reanchor:
            return None
        if self.unknown_seconds_since_checkpoint:
            return None
        remaining = self._checkpoint["net_g"] - self._rate * (
            self.active_seconds - self._checkpoint["active_seconds"])
        return max(0.0, remaining) if math.isfinite(remaining) else None

    def _unknown(self, seconds):
        self.unobserved_seconds += seconds
        if seconds and self._rate is not None:
            self.calibration_status = "unobserved_since_calibration"

    @staticmethod
    def encode_inputs(value):
        if value is None:
            return None
        return {"power": value.power,
                "schedules": [list(slot[:6]) + [list(slot[6])] for slot in value.schedules],
                "presets": [list(pair) for pair in value.presets]}

    def advance(self, now: datetime, *, uncertain=False):
        """Settle each half-open interval once, even without a Bluetooth report."""
        now = _instant(now)
        if self._at is None:
            self._at = now
            return
        if now <= self._at:
            return
        start = self._at
        self._at = now
        if self._inputs is None:
            self._unknown((now - start).total_seconds())
            return
        # Split at confirmation expiry so one long tick and many short ticks agree.
        expiry = (self.last_contact_at + timedelta(seconds=self.max_gap_seconds)
                  if self.last_contact_at else start)
        boundary = min(now, max(start, expiry))
        if boundary > start:
            self._integrate(start, boundary, self._inputs,
                            uncertain=uncertain or self._uncertain)
        if now > boundary:
            self._integrate(boundary, now, self._inputs, uncertain=True)

    def invalidate(self, now: datetime):
        """A failed contact reduces confidence, never erases counting assumptions."""
        self.advance(now)
        self._uncertain = True
        self.consecutive_failures += 1
        self.last_observation_at = None

    def _audit(self, now, reason, start, old, new, cycles=0.0, active=0.0):
        self.adjustments.append({"at": now.isoformat(), "reason": reason,
            "from": start.isoformat() if start else None,
            "onset_known": reason == "confirmed_command",
            "previous": self.encode_inputs(old), "current": self.encode_inputs(new),
            "cycles": cycles, "active_seconds": active})
        # Totals remain lifetime values; recent records are a bounded diagnostic log.
        self.adjustments = self.adjustments[-100:]

    def begin_command(self, now, *, power, schedules, presets):
        self.advance(now)
        target = _inputs(power, schedules, presets)
        if target is None:
            raise ValueError("Invalid command accounting assumptions")
        self._pending_command = {"at": _instant(now), "previous": self._inputs, "target": target,
                                 "observed": self.observed_seconds, "active": self.active_seconds,
                                 "cycles": self.expected_sprays,
                                 "uncertain_active": self.uncertain_active_seconds}
        self.command_status = "pending"

    def fail_command(self, now):
        self.advance(now)
        if self._pending_command is not None:
            pending = self._pending_command
            reclassified = max(0, self.observed_seconds - pending["observed"])
            self.observed_seconds -= reclassified
            self._unknown(reclassified)
            self.uncertain_active_seconds += max(0, self.active_seconds - pending["active"]
                - (self.uncertain_active_seconds - pending["uncertain_active"]))
            self._audit(_instant(now), "command_unconfirmed", self._pending_command["at"],
                        self._inputs, self._pending_command["target"])
        self._pending_command = None
        self.command_status = "failed"
        self._uncertain = True

    def _amount(self, start, end, values):
        calculator = UsageEstimator(self.time_zone, self.max_gap_seconds)
        if values is not None and end > start:
            calculator._integrate(start, end, values)
        return calculator.expected_sprays, calculator.active_seconds

    def observe(self, now: datetime, *, power, schedules, presets,
                complete=True, run_code=None):
        now = _instant(now)
        if self._at is not None and now < self._at:
            return  # Never rewind the accounting watermark.
        incoming = _inputs(power, schedules, presets) if complete else None
        if incoming is None:
            self.invalidate(now)
            return
        previous = self._inputs
        self.advance(now)
        pending = self._pending_command
        if pending is not None and incoming == pending["target"]:
            # The command timestamp is known; correct only its bounded pending window.
            new_cycles, new_active = self._amount(pending["at"], now, incoming)
            # Anchor to pre-command totals, avoiding subtraction cancellation
            # across differently partitioned timer sums or weight checkpoints.
            totals = (pending["cycles"] + new_cycles, pending["active"] + new_active)
            dc = totals[0] - self.expected_sprays
            da = totals[1] - self.active_seconds
            self.expected_sprays, self.active_seconds = totals
            self.adjustment_cycles += dc
            self.adjustment_active_seconds += da
            self._audit(now, "confirmed_command", pending["at"], previous, incoming, dc, da)
            self._pending_command = None
            self.command_status = "confirmed"
        elif previous is not None and incoming != previous:
            # Unknown vendor/device change time: keep the estimate, mark the whole
            # since-confirmation interval uncertain and record a zero signed adjustment.
            reclassified = max(0, self.observed_seconds - self._confirmed_observed)
            self.observed_seconds -= reclassified
            self._unknown(reclassified)
            newly_uncertain_active = max(0, self.active_seconds - self._confirmed_active
                - (self.uncertain_active_seconds - self._confirmed_uncertain_active))
            self.uncertain_active_seconds += newly_uncertain_active
            self._audit(now, "external_change_unknown_onset", self.last_contact_at, previous, incoming)
            if pending is not None:
                self._pending_command = None
                self.command_status = "failed"
        self._inputs = incoming
        self._at = now
        self.last_observation_at = self.last_contact_at = now
        self._confirmed_observed = self.observed_seconds
        self._confirmed_active = self.active_seconds
        self._confirmed_uncertain_active = self.uncertain_active_seconds
        self.last_run_code = run_code
        self.consecutive_failures = 0
        self._uncertain = False

    def _integrate(self, start, end, values, *, uncertain=False):
        # UTC slices count repeated DST minutes once per actual elapsed minute.
        # Streaming keeps catchup memory bounded even after long downtime.
        cursor = start
        while cursor < end:
            local = cursor.astimezone(self._zone)
            stop = min(end, cursor.replace(second=0, microsecond=0) + timedelta(minutes=1))
            seconds = (stop - cursor).total_seconds()
            cursor = stop
            if not values.power:
                self._unknown(seconds) if uncertain else self._observed(seconds)
                continue
            weekday_bit = 1 << ((local.weekday() + 1) % 7)
            minute = local.hour * 60 + local.minute
            matches = []
            ambiguous = False
            for mask, sh, sm, eh, em, intensity, _ in values.schedules:
                if not mask & 0x80 or not mask & weekday_bit:
                    continue
                begin, finish = sh * 60 + sm, eh * 60 + em
                # App overlap logic treats equal non-midnight endpoints as all day.
                if begin == finish and begin != 0:
                    begin, finish = 0, 1440
                if finish <= begin:
                    ambiguous = True
                    break
                if begin <= minute < finish:
                    matches.append(intensity)
            if not ambiguous and not matches and values.schedules:
                self._unknown(seconds) if uncertain else self._observed(seconds)
                continue
            if ambiguous or len(matches) != 1 or matches[0] > len(values.presets):
                self._unknown(seconds)
                continue
            on, pause = values.presets[matches[0] - 1]
            cycles = seconds / (on + pause)
            active = cycles * on
            if (not math.isfinite(cycles) or not math.isfinite(active)
                    or not math.isfinite(self.expected_sprays + cycles)
                    or not math.isfinite(self.active_seconds + active)):
                self._unknown(seconds)
                continue
            self.expected_sprays += cycles
            self.active_seconds += active
            if uncertain:
                self.uncertain_active_seconds += active
            self._unknown(seconds) if uncertain else self._observed(seconds)

    def _observed(self, seconds):
        self.observed_seconds += seconds

    def record_weight(self, now: datetime, gross_g: float, empty_g: float):
        """Record net grams at a fresh observation; no historical backfill.

        A later timestamp marks intervening time unknown. The service should use
        the exact timestamp of its fresh observation accompanying the weighing.
        """
        now = _instant(now)
        gross = _number(gross_g, positive=True, maximum=10000)
        empty = _number(empty_g, maximum=10000)
        if gross < empty:
            raise ValueError("Gross weight must be at least empty-container weight")
        if (self._at is not None and now < self._at) or (
                self._checkpoint and now < datetime.fromisoformat(self._checkpoint["at"])):
            raise ValueError("Weight checkpoints cannot be backdated")
        if self._at is None or now > self._at:
            self.advance(now)
        current = {"at": now.isoformat(), "gross_g": gross, "empty_g": empty,
                   "net_g": gross - empty, "active_seconds": self.active_seconds,
                   "unobserved_seconds": self.unobserved_seconds}
        old = self._checkpoint
        old_rate = self._rate
        self._rate = None
        if old is None:
            self.calibration_status = "needs_second_weight"
        elif current["empty_g"] != old["empty_g"] or current["net_g"] > old["net_g"]:
            self.calibration_status = "new_bottle_checkpoint"
        elif self._needs_reanchor:
            # A flow rate is learned knowledge, but the old bottle mass cannot
            # bridge downtime. Fresh weight establishes a new mass anchor.
            self._rate = old_rate
            self.calibration_status = "calibrated_reanchored" if old_rate else "needs_second_weight"
        elif current["unobserved_seconds"] != old["unobserved_seconds"]:
            self.calibration_status = "insufficient_coverage"
        elif current["active_seconds"] <= old["active_seconds"]:
            self.calibration_status = "no_active_time"
        elif current["net_g"] == old["net_g"]:
            self.calibration_status = "no_measured_loss"
        else:
            rate = (old["net_g"] - current["net_g"]) / (current["active_seconds"] - old["active_seconds"])
            if math.isfinite(rate) and rate > 0:
                self._rate = rate
                self.calibration_status = "calibrated"
            else:
                self.calibration_status = "invalid_rate"
        self._checkpoint = current
        self._needs_reanchor = False

    def to_dict(self):
        return {"version": 2, "at": self._at.isoformat() if self._at else None,
                "inputs": self.encode_inputs(self._inputs),
                "last_contact_at": self.last_contact_at.isoformat() if self.last_contact_at else None,
                "uncertain_active_seconds": self.uncertain_active_seconds,
                "adjustment_cycles": self.adjustment_cycles,
                "adjustment_active_seconds": self.adjustment_active_seconds,
                "adjustments": list(self.adjustments),
                "command_status": self.command_status,
                "expected_sprays": self.expected_sprays,
                "active_seconds": self.active_seconds, "observed_seconds": self.observed_seconds,
                "unobserved_seconds": self.unobserved_seconds,
                "checkpoint": dict(self._checkpoint) if self._checkpoint else None,
                "grams_per_active_second": self._rate,
                "calibration_status": self.calibration_status}

    def restore(self, data):
        """Restore v2 counting assumptions; legacy v1 has no backfill anchor.

        Downtime is estimated with uncertainty, never used to learn a rate.
        """
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            raise ValueError("Unsupported estimation storage version")
        names = ("expected_sprays", "active_seconds", "observed_seconds", "unobserved_seconds")
        totals = {name: _number(data.get(name)) for name in names}
        if totals["active_seconds"] > totals["observed_seconds"] + totals["unobserved_seconds"]:
            raise ValueError("Active time cannot exceed observed time")
        checkpoint = data.get("checkpoint")
        rate = data.get("grams_per_active_second")
        rate = None if rate is None else _number(rate, positive=True)
        if checkpoint is not None:
            if not isinstance(checkpoint, dict):
                raise ValueError("Invalid stored weight checkpoint")
            try:
                at = _instant(datetime.fromisoformat(checkpoint["at"]))
                gross = _number(checkpoint["gross_g"], positive=True, maximum=10000)
                empty = _number(checkpoint["empty_g"], maximum=10000)
                net = _number(checkpoint["net_g"], maximum=10000)
                active = _number(checkpoint["active_seconds"])
                unknown = _number(checkpoint["unobserved_seconds"])
            except (KeyError, TypeError) as err:
                raise ValueError("Invalid stored weight checkpoint") from err
            if (gross < empty or net != gross - empty
                    or active > totals["active_seconds"]
                    or unknown > totals["unobserved_seconds"]):
                raise ValueError("Inconsistent stored weight checkpoint")
            checkpoint = {"at": at.isoformat(), "gross_g": gross, "empty_g": empty,
                          "net_g": net, "active_seconds": active, "unobserved_seconds": unknown}
        elif rate is not None:
            raise ValueError("A stored rate requires a weight checkpoint")
        at = inputs = contact = None
        adjustments, ac, aa, uncertain_active = [], 0.0, 0.0, 0.0
        if data["version"] == 2:
            try:
                at = _instant(datetime.fromisoformat(data["at"])) if data.get("at") else None
                contact = _instant(datetime.fromisoformat(data["last_contact_at"])) if data.get("last_contact_at") else None
                raw = data.get("inputs")
                inputs = _inputs(**raw) if raw is not None else None
                if raw is not None and inputs is None:
                    raise ValueError("Invalid saved assumptions")
                if inputs is not None and at is None or contact is not None and (at is None or contact > at):
                    raise ValueError("Invalid saved accounting timestamps")
                uncertain_active = _number(data.get("uncertain_active_seconds", 0))
                ac, aa = data.get("adjustment_cycles", 0), data.get("adjustment_active_seconds", 0)
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (ac, aa)):
                    raise ValueError("Invalid saved signed adjustments")
                if totals["expected_sprays"] - ac < 0 or totals["active_seconds"] - aa < 0:
                    raise ValueError("Invalid accumulated usage")
                adjustments = data.get("adjustments", [])
                if not isinstance(adjustments, list) or len(adjustments) > 100:
                    raise ValueError("Invalid adjustment audit")
                for item in adjustments:
                    if not isinstance(item, dict) or not isinstance(item.get("reason"), str):
                        raise ValueError("Invalid adjustment audit")
                    _instant(datetime.fromisoformat(item["at"]))
                    for key in ("cycles", "active_seconds"):
                        if isinstance(item[key], bool) or not isinstance(item[key], (int, float)) or not math.isfinite(item[key]):
                            raise ValueError("Invalid adjustment audit")
            except (KeyError, TypeError) as err:
                raise ValueError("Invalid saved accounting data") from err
        for name, value in totals.items():
            setattr(self, name, value)
        self._at = self._inputs = self.last_observation_at = None
        self.last_run_code = None
        self._checkpoint = checkpoint
        self._rate = rate
        self._at, self._inputs, self.last_contact_at = at, inputs, contact
        self.uncertain_active_seconds = uncertain_active
        self._confirmed_active = self.active_seconds
        self._confirmed_uncertain_active = uncertain_active
        self.adjustment_cycles, self.adjustment_active_seconds = ac, aa
        self.adjustments = list(adjustments)
        self._uncertain = True  # Downtime uses the saved model but is never calibration proof.
        self._confirmed_observed = self.observed_seconds
        self._needs_reanchor = True
        self.command_status = "failed" if data.get("command_status") == "pending" else "idle"
        self._pending_command = None
        self.calibration_status = "restart_requires_weight"
