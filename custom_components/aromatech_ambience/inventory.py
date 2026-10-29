"""Per-diffuser bottle ledger and conditional weekly consumption forecasts.

No Home Assistant or BLE dependencies. The caller serializes mutations and
supplies fresh observations for swaps/weighing. Rates price future usage only;
stored balances are never recomputed from lifetime counts and a newer rate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
from uuid import uuid4


DEFAULT_GRAMS_PER_SPRAY = 0.004
DEFAULT_SPRAY_SECONDS = 5.0
DEFAULT_OIL_G = 60.0


def _number(value, *, positive=False, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A finite number is required")
    try:
        value = float(value)
    except OverflowError as err:
        raise ValueError("A finite number is required") from err
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError("A finite nonnegative number is required")
    if maximum is not None and value > maximum:
        raise ValueError("Weight exceeds 10000 grams")
    return value


def _text(value, *, required=False):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Text must contain at most 4096 characters")
    value = value.strip()
    if required and not value:
        raise ValueError("A name or reason is required")
    return value


def _time(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return now.astimezone(timezone.utc).isoformat()


def _name(value):
    value = _text(value, required=True)
    if len(value) > 100:
        raise ValueError("Display names must contain at most 100 characters")
    return value


def _stored_time(value):
    if not isinstance(value, str):
        raise ValueError("Invalid stored timestamp")
    try:
        return _time(datetime.fromisoformat(value))
    except (TypeError, ValueError) as err:
        raise ValueError("Invalid stored timestamp") from err


def _bool(value):
    if type(value) is not bool:
        raise ValueError("A boolean is required")
    return value


def _id():
    return uuid4().hex


def _net(fields, tare):
    if ("gross_g" in fields) == ("oil_g" in fields):
        raise ValueError("Supply exactly one of gross_g or oil_g")
    if "oil_g" in fields:
        return _number(fields["oil_g"], maximum=10000)
    if tare is None:
        raise ValueError("Set the empty bottle weight in Edit bottle before recording a gross weight")
    gross = _number(fields["gross_g"], positive=True, maximum=10000)
    if gross < tare:
        raise ValueError("Gross weight cannot be below empty-bottle weight")
    return gross - tare


def _rate(fields):
    if ("grams_per_spray" in fields) != ("spray_seconds" in fields):
        raise ValueError("Supply both grams_per_spray and spray_seconds")
    if "grams_per_spray" not in fields:
        return {}
    grams = _number(fields["grams_per_spray"], positive=True, maximum=10000)
    seconds = _number(fields["spray_seconds"], positive=True)
    rate = _number(grams / seconds, positive=True)
    return {"grams_per_spray": grams, "spray_seconds": seconds,
            "rate_g_per_second": rate, "rate_source": "provisional"}


def weekly_active_seconds(schedules, presets):
    """Seven-day mean active seconds; None means unsupported/ambiguous input.

    Half-open local windows use the inherited, inferred Sunday=bit0 mapping.
    This recurring forecast does not predict DST-day lengths or future edits.
    """
    if schedules is None or presets is None:
        return None
    try:
        slots = list(schedules)
        timings = list(presets)
        if len(slots) > 5 or not 1 <= len(timings) <= 5:
            return None
        timings = [(_number(on, positive=True), _number(pause)) for on, pause in timings]
        windows = [[] for _ in range(7)]
        total = 0.0
        for slot in slots:
            values = tuple(slot[:6]) if isinstance(slot, (list, tuple)) else tuple(
                getattr(slot, key) for key in ("days_mask", "start_hour", "start_minute",
                                             "end_hour", "end_minute", "intensity"))
            if len(values) != 6 or any(type(value) is not int for value in values):
                return None
            mask, sh, sm, eh, em, intensity = values
            if not (0 <= mask <= 255 and 0 <= sh <= 23 and 0 <= eh <= 23
                    and 0 <= sm <= 59 and 0 <= em <= 59 and 1 <= intensity <= 5):
                return None
            if not mask & 128 or not mask & 127:
                continue
            start, end = sh * 60 + sm, eh * 60 + em
            if start == end and start != 0:
                start, end = 0, 1440
            if end <= start or intensity > len(timings):
                return None
            on, pause = timings[intensity - 1]
            cycle = _number(on + pause, positive=True)
            for day in range(7):
                if mask & (1 << day):
                    if any(start < old_end and old_start < end for old_start, old_end in windows[day]):
                        return None
                    windows[day].append((start, end))
                    total += (end - start) * 60 * (on / cycle)
        return _number(total / 7)
    except (TypeError, ValueError, AttributeError, OverflowError, ZeroDivisionError):
        return None


class Inventory:
    """Transactional in-memory inventory; persistence is owned by the caller."""

    _FIELDS = {
        "add_scent": {"name", "brand", "notes", "grams_per_spray", "spray_seconds"},
        "edit_scent": {"scent_id", "name", "brand", "notes", "grams_per_spray", "spray_seconds"},
        "delete_scent": {"scent_id", "confirm"},
        "add_bottle": {"name", "notes", "empty_g", "scent_id", "gross_g", "oil_g"},
        "edit_bottle": {"bottle_id", "name", "notes", "empty_g"},
        "install_bottle": {"bottle_id"},
        "store_bottle": {"bottle_id"},
        "refill_bottle": {"bottle_id", "scent_id", "gross_g", "oil_g"},
        "record_weight": {"bottle_id", "gross_g"},
        "correct_weight": {"bottle_id", "gross_g", "reason"},
        "archive_bottle": {"bottle_id", "archived"},
        "delete_bottle": {"bottle_id", "confirm"},
    }

    def __init__(self):
        self.scents = {}
        self.bottles = {}
        self.active_bottle_id = None
        self.initialized = False
        self._last_action_at = None

    def execute(self, action, now, **fields):
        """Validate and commit a whole operation, or leave all state unchanged.

        Install/store require fresh power-off confirmation at the HA boundary.
        Weight checkpoints must accompany a fresh complete observation there.
        """
        if not isinstance(action, str) or action not in self._FIELDS or set(fields) - self._FIELDS[action]:
            raise ValueError("Unknown inventory action or field")
        stamp = _time(now)
        if self._last_action_at and stamp < self._last_action_at:
            raise ValueError("Inventory operations cannot be backdated")
        candidate = deepcopy(self)
        try:
            result = candidate._execute(action, stamp, fields)
        except KeyError as err:
            raise ValueError(f"Missing or unknown inventory field: {err.args[0]}") from err
        except (TypeError, OverflowError) as err:
            raise ValueError("Invalid inventory field value") from err
        if action == "install_bottle":
            candidate.initialized = True
        candidate._last_action_at = stamp
        self.__dict__.update(candidate.__dict__)
        return result

    def _execute(self, action, stamp, fields):
        if action == "add_scent":
            default_rate = "grams_per_spray" not in fields and "spray_seconds" not in fields
            if default_rate:
                fields = {**fields, "grams_per_spray": DEFAULT_GRAMS_PER_SPRAY,
                          "spray_seconds": DEFAULT_SPRAY_SECONDS}
            ident = _id()
            self.scents[ident] = {"id": ident, "name": _name(fields["name"]),
                "brand": _text(fields.get("brand", "")), "notes": _text(fields.get("notes", "")),
                "rate_g_per_second": None, "rate_source": None,
                "grams_per_spray": None, "spray_seconds": None, **_rate(fields)}
            if default_rate:
                self.scents[ident]["rate_source"] = "default_estimate"
            return ident
        if action in ("edit_scent", "delete_scent"):
            scent = self.scents[fields["scent_id"]]
            if action == "delete_scent":
                if fields.get("confirm") is not True:
                    raise ValueError("Permanent deletion requires confirm=True")
                if any(f["scent_id"] == scent["id"] for b in self.bottles.values() for f in b["fills"]):
                    raise ValueError("A scent referenced by fill history cannot be deleted")
                del self.scents[scent["id"]]
            else:
                for key in ("name", "brand", "notes"):
                    if key in fields:
                        scent[key] = _name(fields[key]) if key == "name" else _text(fields[key])
                scent.update(_rate(fields))
            return scent["id"]
        if action == "add_bottle":
            ident = _id()
            bottle = {"id": ident, "name": _name(fields["name"]),
                      "notes": _text(fields.get("notes", "")),
                      "empty_g": _number(fields["empty_g"], maximum=10000) if "empty_g" in fields else None,
                      "archived": False, "fills": []}
            bottle["fills"].append(self._new_fill(fields, bottle["empty_g"], stamp))
            self.bottles[ident] = bottle
            return ident
        bottle_id = fields.get("bottle_id", self.active_bottle_id)
        bottle = self.bottles[bottle_id]
        fill = bottle["fills"][-1]
        active = bottle_id == self.active_bottle_id
        if action == "edit_bottle":
            for key in ("name", "notes"):
                if key in fields:
                    bottle[key] = _name(fields[key]) if key == "name" else _text(fields[key])
            if "empty_g" in fields:
                tare = _number(fields["empty_g"], maximum=10000)
                if tare != bottle["empty_g"]:
                    fill["mass_confident"] = False
                    fill["checkpoint"] = None
                bottle["empty_g"] = tare
        elif action == "install_bottle":
            if bottle["archived"]:
                raise ValueError("An archived bottle cannot be installed")
            if not active:
                if self.active_bottle_id:
                    self.bottles[self.active_bottle_id]["fills"][-1]["checkpoint"] = None
                self.active_bottle_id = bottle_id
                fill["checkpoint"] = None
        elif action == "store_bottle":
            if not active:
                raise ValueError("Only the active bottle can be stored")
            fill["checkpoint"] = None
            self.active_bottle_id = None
        elif action == "refill_bottle":
            if active:
                raise ValueError("Store the bottle before refilling it")
            bottle["fills"].append(self._new_fill(fields, bottle["empty_g"], stamp))
        elif action in ("record_weight", "correct_weight"):
            self._weigh(bottle, fill, fields, stamp, correction=action == "correct_weight", active=active)
        elif action == "archive_bottle":
            archived = _bool(fields["archived"])
            if active and archived:
                raise ValueError("The active bottle cannot be archived")
            bottle["archived"] = archived
        elif action == "delete_bottle":
            if active:
                raise ValueError("The active bottle cannot be deleted")
            if fields.get("confirm") is not True:
                raise ValueError("Permanent deletion requires confirm=True")
            del self.bottles[bottle_id]
        return bottle_id

    def _new_fill(self, fields, tare, stamp):
        scent_id = fields["scent_id"]
        if scent_id not in self.scents:
            raise ValueError("Unknown scent")
        if "gross_g" not in fields and "oil_g" not in fields:
            fields = {**fields, "oil_g": DEFAULT_OIL_G}
        net = _net(fields, tare)
        scent = self.scents[scent_id]
        return {"id": _id(), "scent_id": scent_id, "created_at": stamp,
                "initial_g": net, "remaining_g": net, "balance_g": net, "cycles": 0.0,
                "active_seconds": 0.0, "unknown_seconds": 0.0, "consumed_g": 0.0,
                "mass_confident": True, "measurements": [], "checkpoint": None,
                "uncertainty_g": 0.0, "uncertainty_upper_g": 0.0, "unbounded_unknown": False,
                "rate_g_per_second": scent["rate_g_per_second"],
                "rate_source": scent["rate_source"], "rate_inherited": True}

    def _weigh(self, bottle, fill, fields, stamp, *, correction, active):
        net = _net(fields, bottle["empty_g"])
        reason = _text(fields["reason"], required=True) if correction else ""
        measurement = {"id": _id(), "at": stamp, "kind": "correction" if correction else "weight",
                       "reason": reason, "gross_g": float(fields["gross_g"]),
                       "empty_g": bottle["empty_g"], "net_g": net,
                       "cycles": fill["cycles"], "active_seconds": fill["active_seconds"],
                       "unknown_seconds": fill["unknown_seconds"]}
        previous = fill["checkpoint"]
        if active and not correction and previous:
            seconds = fill["active_seconds"] - previous["active_seconds"]
            loss = previous["net_g"] - net
            if seconds > 0 and loss > 0 and fill["unknown_seconds"] == previous["unknown_seconds"]:
                rate = _number(loss / seconds, positive=True)
                scent = self.scents[fill["scent_id"]]
                scent.update(rate_g_per_second=rate, rate_source="learned",
                             grams_per_spray=None, spray_seconds=None)
                fill.update(rate_g_per_second=rate, rate_source="learned", rate_inherited=False)
        fill["measurements"].append(measurement)
        fill["remaining_g"] = fill["balance_g"] = net
        fill["mass_confident"] = True
        fill["uncertainty_g"] = fill["uncertainty_upper_g"] = 0.0
        fill["unbounded_unknown"] = False
        # Corrections never create a learning baseline, even for the next weigh.
        fill["checkpoint"] = None if correction or not active else {
            "measurement_id": measurement["id"], "net_g": net,
            "active_seconds": fill["active_seconds"], "unknown_seconds": fill["unknown_seconds"]}

    def apply_usage(self, cycles, active_seconds, unknown_seconds, uncertain_active_seconds=0):
        """Apply newly observed deltas exactly once; reads never call this."""
        cycles, seconds, unknown = (_number(value) for value in (cycles, active_seconds, unknown_seconds))
        uncertain_active = _number(uncertain_active_seconds)
        if not self.active_bottle_id:
            return
        fill = deepcopy(self.bottles[self.active_bottle_id]["fills"][-1])
        for key, value in (("cycles", cycles), ("active_seconds", seconds), ("unknown_seconds", unknown)):
            fill[key] = _number(fill[key] + value)
        rate = fill["rate_g_per_second"]
        if unknown:
            fill["checkpoint"] = None
            if rate is None:
                fill["unbounded_unknown"] = True
            else:
                # Worst-case continuous spray, without assuming preset duty.
                fill["uncertainty_g"] = _number(fill["uncertainty_g"] + max(0, unknown - uncertain_active) * rate)
        if rate is not None and uncertain_active:
            fill["uncertainty_upper_g"] += uncertain_active * rate
        if seconds and rate is None:
            fill["unbounded_unknown"] = True
        if rate is not None:
            consumed = _number(seconds * rate)
            fill["consumed_g"] = _number(fill["consumed_g"] + consumed)
            fill["balance_g"] -= consumed
            fill["remaining_g"] = max(0.0, fill["balance_g"])
        self.bottles[self.active_bottle_id]["fills"][-1] = fill

    def apply_adjustment(self, cycles, active_seconds, rate):
        """Apply a separately audited signed correction to the exact selected fill."""
        if not self.active_bottle_id:
            return
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (cycles, active_seconds)):
            raise ValueError("Invalid signed usage correction")
        fill = deepcopy(self.bottles[self.active_bottle_id]["fills"][-1])
        if fill["cycles"] + cycles < -1e-9 or fill["active_seconds"] + active_seconds < -1e-9:
            raise ValueError("Correction exceeds usage owned by this fill")
        fill["cycles"] = _number(max(0, fill["cycles"] + cycles))
        fill["active_seconds"] = _number(max(0, fill["active_seconds"] + active_seconds))
        if rate is not None:
            correction = active_seconds * _number(rate, positive=True)
            if fill["consumed_g"] + correction < -1e-9:
                raise ValueError("Correction exceeds consumption owned by this fill")
            fill["consumed_g"] = _number(max(0, fill["consumed_g"] + correction))
            fill["balance_g"] -= correction
            fill["remaining_g"] = max(0.0, fill["balance_g"])
        self.bottles[self.active_bottle_id]["fills"][-1] = fill

    def mark_restart(self, unknown_seconds=None):
        """Bound known downtime for the active fill; unknown elapsed is unbounded."""
        if unknown_seconds is not None:
            unknown_seconds = _number(unknown_seconds)
        if self.active_bottle_id:
            if unknown_seconds is not None:
                self.apply_usage(0, 0, unknown_seconds)
            fill = self.bottles[self.active_bottle_id]["fills"][-1]
            if unknown_seconds is None:
                fill["unbounded_unknown"] = True
            fill["checkpoint"] = None

    def summary(self, schedules=None, presets=None, refill_days=7, low_g=5, depleted_g=0):
        refill_days, low_g, depleted_g = (_number(v) for v in (refill_days, low_g, depleted_g))
        if depleted_g >= low_g:
            raise ValueError("Depleted threshold must be below low threshold")
        result = dict(active_bottle=None, active_scent=None, fill_id=None, fill_sprays=None,
                      fill_active_seconds=None, fill_consumed_g=None,
                      rate_mg_per_second=None, five_second_dose_mg=None,
                      remaining_g=None, remaining_percent=None, daily_g=None,
                      days_remaining=None, status="no_bottle", confidence="no_bottle",
                      remaining_lower_g=None, remaining_upper_g=None, uncertainty_g=None,
                      uncertainty_model="continuous_spray_upper_bound", rate_source=None, mass_basis=None)
        if not self.active_bottle_id:
            return result
        bottle = self.bottles[self.active_bottle_id]
        fill = bottle["fills"][-1]
        scent = self.scents[fill["scent_id"]]
        result.update(active_bottle=bottle["name"], active_scent=scent["name"],
                      fill_id=fill["id"], fill_sprays=fill["cycles"],
                      fill_active_seconds=fill["active_seconds"],
                      fill_consumed_g=fill["consumed_g"] if fill["rate_g_per_second"] is not None else None,
                      rate_mg_per_second=fill["rate_g_per_second"] * 1000 if fill["rate_g_per_second"] is not None else None,
                      five_second_dose_mg=fill["rate_g_per_second"] * 5000 if fill["rate_g_per_second"] is not None else None)
        result["rate_source"] = ("default_estimate" if fill["rate_source"] == "default_estimate" else
                                 (("scent_default_" if fill["rate_inherited"] else "")
                                  + fill["rate_source"]) if fill["rate_source"] else None)
        result["mass_basis"] = ({"weight": "weighed", "correction": "corrected"}[
            fill["measurements"][-1]["kind"]] if fill["measurements"] else "initial_estimate")
        remaining = fill["remaining_g"]
        uncertainty = fill["uncertainty_g"]
        lower = max(fill["balance_g"] - uncertainty, 0)
        anchor = fill["measurements"][-1]["net_g"] if fill["measurements"] else fill["initial_g"]
        upper = min(anchor, max(0, fill["balance_g"] + fill["uncertainty_upper_g"]))
        result["uncertainty_g"] = uncertainty
        if not fill["mass_confident"] or fill["unbounded_unknown"]:
            result.update(status="needs_weighing", confidence="uncertain")
            return result
        result.update(remaining_lower_g=lower, remaining_upper_g=upper)
        if uncertainty > 0 and lower <= low_g < remaining:
            result.update(status="needs_weighing", confidence="uncertainty_crosses_low_threshold")
            return result
        result.update(remaining_g=remaining, remaining_percent=(
            min(100.0, remaining / fill["initial_g"] * 100) if fill["initial_g"] else None))
        rate = fill["rate_g_per_second"]
        if rate is None:
            result.update(status="needs_calibration", confidence="weighed_uncalibrated")
            return result
        result["confidence"] = result["rate_source"]
        if uncertainty > 0:
            result["confidence"] = "estimated_with_uncertainty"
        active = weekly_active_seconds(schedules, presets)
        if active is not None:
            daily = active * rate
            if math.isfinite(daily):
                result["daily_g"] = daily
                days = max(remaining - depleted_g, 0) / daily if daily > 0 else None
                if days is None or math.isfinite(days):
                    result["days_remaining"] = days
        if remaining <= depleted_g:
            result["status"] = "estimated_depleted"
        elif remaining <= low_g:
            result["status"] = "low"
        elif result["days_remaining"] is not None and result["days_remaining"] <= refill_days:
            result["status"] = "refill_soon"
        elif active is None:
            result["status"] = "forecast_unavailable"
        else:
            result["status"] = "ok"
        return result

    def to_dict(self):
        return deepcopy({"version": 2, "initialized": self.initialized,
                         "active_bottle_id": self.active_bottle_id, "scents": self.scents,
                         "bottles": self.bottles, "last_action_at": self._last_action_at})

    def restore(self, data):
        """Validate atomically; callers invoke mark_restart only on real startup."""
        try:
            candidate = self._validated(data)
        except (KeyError, TypeError, AttributeError, OverflowError) as err:
            raise ValueError("Invalid inventory store") from err
        self.__dict__.update(candidate.__dict__)

    @classmethod
    def _validated(cls, data):
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2):
            raise ValueError("Unsupported inventory store version")
        def keys(record, required):
            if not isinstance(record, dict) or set(record) != set(required.split()):
                raise ValueError("Invalid inventory record fields")

        keys(data, "version initialized active_bottle_id scents bottles last_action_at")
        obj = cls()
        obj.initialized = _bool(data["initialized"])
        obj._last_action_at = None if data["last_action_at"] is None else _stored_time(data["last_action_at"])
        obj.scents, obj.bottles = deepcopy(data["scents"]), deepcopy(data["bottles"])
        if not isinstance(obj.scents, dict) or not isinstance(obj.bottles, dict):
            raise ValueError("Invalid inventory records")
        seen = set()

        def identity(record, expected=None):
            ident = _text(record["id"], required=True)
            if ident in seen or (expected is not None and ident != expected):
                raise ValueError("Duplicate or inconsistent inventory ID")
            seen.add(ident)

        for ident, scent in obj.scents.items():
            keys(scent, "id name brand notes rate_g_per_second rate_source grams_per_spray spray_seconds")
            identity(scent, ident)
            for key in ("name", "brand", "notes"):
                scent[key] = _name(scent[key]) if key == "name" else _text(scent[key])
            source, rate = scent["rate_source"], scent["rate_g_per_second"]
            if rate is None:
                if source is not None or scent["grams_per_spray"] is not None or scent["spray_seconds"] is not None:
                    raise ValueError("Invalid rate provenance")
            else:
                scent["rate_g_per_second"] = _number(rate, positive=True)
                if source in ("provisional", "default_estimate"):
                    checked = _rate(scent)
                    if source == "default_estimate" and (
                            scent["grams_per_spray"] != DEFAULT_GRAMS_PER_SPRAY
                            or scent["spray_seconds"] != DEFAULT_SPRAY_SECONDS):
                        raise ValueError("Invalid default estimate")
                    if checked["rate_g_per_second"] != rate:
                        raise ValueError("Inconsistent provisional rate")
                elif source != "learned" or scent["grams_per_spray"] is not None or scent["spray_seconds"] is not None:
                    raise ValueError("Invalid rate provenance")
        for ident, bottle in obj.bottles.items():
            keys(bottle, "id name notes empty_g archived fills")
            identity(bottle, ident)
            bottle["name"] = _name(bottle["name"])
            bottle["notes"] = _text(bottle["notes"])
            if bottle["empty_g"] is not None:
                bottle["empty_g"] = _number(bottle["empty_g"], maximum=10000)
            _bool(bottle["archived"])
            if not isinstance(bottle["fills"], list) or not bottle["fills"]:
                raise ValueError("A bottle must have fill history")
            for fill in bottle["fills"]:
                if data["version"] == 1:
                    fill.setdefault("balance_g", fill.get("remaining_g"))
                    fill.setdefault("uncertainty_upper_g", 0.0)
                keys(fill, "id scent_id created_at initial_g remaining_g cycles active_seconds unknown_seconds consumed_g mass_confident measurements checkpoint rate_g_per_second rate_source rate_inherited uncertainty_g uncertainty_upper_g balance_g unbounded_unknown")
                identity(fill)
                if fill["scent_id"] not in obj.scents:
                    raise ValueError("Dangling scent reference")
                fill["created_at"] = _stored_time(fill["created_at"])
                for key in ("initial_g", "remaining_g", "cycles", "active_seconds", "unknown_seconds", "consumed_g"):
                    fill[key] = _number(fill[key], maximum=10000 if key in ("initial_g", "remaining_g") else None)
                _bool(fill["mass_confident"])
                _bool(fill["unbounded_unknown"])
                _number(fill["uncertainty_g"])
                _number(fill["uncertainty_upper_g"])
                balance = fill["balance_g"]
                if (isinstance(balance, bool) or not isinstance(balance, (int, float))
                        or not math.isfinite(balance) or max(0, balance) != fill["remaining_g"]):
                    raise ValueError("Invalid fill accounting balance")
                _bool(fill["rate_inherited"])
                if fill["rate_g_per_second"] is None:
                    if fill["rate_source"] is not None:
                        raise ValueError("Invalid fill rate provenance")
                else:
                    _number(fill["rate_g_per_second"], positive=True)
                    if fill["rate_source"] not in ("provisional", "learned", "default_estimate"):
                        raise ValueError("Invalid fill rate provenance")
                if not isinstance(fill["measurements"], list):
                    raise ValueError("Invalid measurement history")
                last_at = fill["created_at"]
                last_totals = (0.0, 0.0, 0.0)
                for measurement in fill["measurements"]:
                    keys(measurement, "id at kind reason gross_g empty_g net_g cycles active_seconds unknown_seconds")
                    identity(measurement)
                    at = _stored_time(measurement["at"])
                    if at < last_at:
                        raise ValueError("Backdated measurement")
                    last_at = at
                    gross = _number(measurement["gross_g"], positive=True, maximum=10000)
                    tare = _number(measurement["empty_g"], maximum=10000)
                    if gross < tare or measurement["net_g"] != gross - tare:
                        raise ValueError("Invalid measurement mass")
                    _number(measurement["net_g"], maximum=10000)
                    kind = measurement["kind"]
                    if kind not in ("weight", "correction"):
                        raise ValueError("Invalid measurement kind")
                    _text(measurement["reason"], required=kind == "correction")
                    values = tuple(_number(measurement[key]) for key in ("cycles", "active_seconds", "unknown_seconds"))
                    if any(value < old or value > fill[key] for value, old, key in zip(
                            values, last_totals, ("cycles", "active_seconds", "unknown_seconds"))):
                        raise ValueError("Inconsistent measurement totals")
                    last_totals = values
                checkpoint = fill["checkpoint"]
                if checkpoint is not None:
                    keys(checkpoint, "measurement_id net_g active_seconds unknown_seconds")
                    for key in ("net_g", "active_seconds", "unknown_seconds"):
                        _number(checkpoint[key])
                    if not fill["measurements"]:
                        raise ValueError("Checkpoint lacks measurement")
                    last = fill["measurements"][-1]
                    expected = {"measurement_id": last["id"], "net_g": last["net_g"],
                                "active_seconds": last["active_seconds"], "unknown_seconds": last["unknown_seconds"]}
                    if checkpoint != expected or last["kind"] != "weight" or last["empty_g"] != bottle["empty_g"]:
                        raise ValueError("Invalid learning checkpoint")
                if obj._last_action_at is None or last_at > obj._last_action_at:
                    raise ValueError("Invalid action chronology")
        active = data["active_bottle_id"]
        if active is not None and (active not in obj.bottles or obj.bottles[active]["archived"]):
            raise ValueError("Invalid active bottle")
        obj.active_bottle_id = active
        if not obj.initialized and active is not None:
            raise ValueError("Uninitialized inventory cannot have an installed bottle")
        return obj
