"""Crash-safe previous-schedule journal, independent of bottle/usage storage."""
from copy import deepcopy
from dataclasses import replace

from .protocol import Schedule


def encode(slots):
    return [slot.to_bytes().hex() for slot in slots]


def decode(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 5:
        raise ValueError("A saved schedule requires one to five slots")
    if any(not isinstance(raw, str) or len(raw) != 20 for raw in values):
        raise ValueError("Invalid saved schedule bytes")
    return [Schedule.from_bytes(bytes.fromhex(raw)) for raw in values]


class ScheduleMode:
    """Persist before writing; never overwrite an unreadable journal."""

    def __init__(self, store):
        self.store = store
        self.backup = None
        self.error = None

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("Unsupported schedule-mode data")
        backup = data.get("backup")
        if backup is None:
            if set(data) != {"version", "backup"}:
                raise ValueError("Unexpected schedule-mode data")
            return None
        if not isinstance(backup, dict) or set(backup) != {"previous", "active", "restoring"}:
            raise ValueError("Invalid previous-schedule journal")
        previous, active = decode(backup["previous"]), decode(backup["active"])
        if len(previous) != len(active):
            raise ValueError("Saved schedule counts differ")
        expected = [replace(previous[0], days_mask=255, start_hour=8, start_minute=0,
                            end_hour=8, end_minute=0)]
        expected += [replace(slot, days_mask=slot.days_mask & 127) for slot in previous[1:]]
        if not same_timing(active, expected):
            raise ValueError("Saved full-day schedule does not match previous schedule")
        if backup["restoring"] is not None and not same_timing(decode(backup["restoring"]), previous):
            raise ValueError("Invalid restoring schedule")
        return deepcopy(backup)

    async def initialize(self):
        try:
            data = await self.store.async_load()
            self.backup = self.validate(data) if data is not None else None
        except Exception as err:
            self.error = f"Previous schedule storage cannot be read: {type(err).__name__}"

    async def save(self, backup):
        if self.error:
            raise ValueError(self.error)
        data = {"version": 1, "backup": deepcopy(backup)}
        checked = self.validate(data)
        await self.store.async_save(data)
        self.backup = checked


def same_timing(actual, expected):
    """Slot-one intensity is user-adjustable while full-day mode is active."""
    if len(actual) != len(expected) or not actual:
        return False
    return all(
        a == b if index else a.to_bytes()[:5] + a.unknown == b.to_bytes()[:5] + b.unknown
        for index, (a, b) in enumerate(zip(actual, expected))
    )


def is_full_day(slots):
    if not slots:
        return False
    first = slots[0]
    start = first.start_hour * 60 + first.start_minute
    end = first.end_hour * 60 + first.end_minute
    return first.days_mask == 255 and start == end and start != 0
