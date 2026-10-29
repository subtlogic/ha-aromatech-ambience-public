"""Ambience BLE encoding and state decoding. Extended writes remain experimental."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

SERVICE_UUID = "0000ee01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ee02-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ee03-0000-1000-8000-00805f9b34fb"
CMD_VERSION = 0x0005
CMD_NAME = 0x0006
CMD_UNKNOWN_0007 = 0x0007
CMD_TIME = 0x000A
CMD_LIGHT = 0x000B
CMD_INTENSITY_PRESETS = 0x000E
CMD_UNKNOWN_0014 = 0x0014
CMD_SOUND = 0x0015
CMD_POWER = 0x03E9
START_CMD = 0x25
DIR_CMD = b"\xff\x01"
CONST_AF = b"\xaf\x00"
MAX_ATT = 20
LIGHT_MODES = ["off", "warm", "cool", "flow", "custom"]
POWER_ON_ARG = 0x00  # Inverted between command and reported power.
POWER_OFF_ARG = 0x01


def _integer_range(name: str, value: int, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")


@dataclass(frozen=True)
class Schedule:
    """One ten-byte slot, including four uninterpreted bytes to preserve."""

    days_mask: int
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int
    intensity: int
    unknown: bytes = b"\x00" * 4

    def __post_init__(self) -> None:
        for name, high in (("days_mask", 255), ("start_hour", 23),
                           ("start_minute", 59), ("end_hour", 23),
                           ("end_minute", 59)):
            _integer_range(name, getattr(self, name), 0, high)
        _integer_range("intensity", self.intensity, 1, 5)
        if not isinstance(self.unknown, bytes) or len(self.unknown) != 4:
            raise ValueError("unknown must be exactly four immutable bytes")

    def to_bytes(self) -> bytes:
        return bytes((self.days_mask, self.start_hour, self.start_minute,
                      self.end_hour, self.end_minute, self.intensity)) + self.unknown

    @classmethod
    def from_bytes(cls, raw: bytes) -> Schedule:
        if len(raw) != 10:
            raise ValueError("schedule slot must be exactly ten bytes")
        return cls(*raw[:6], unknown=bytes(raw[6:]))


def _valid_schedule_head(raw: bytes) -> bool:
    return (len(raw) >= 5 and raw[0] == 1 and raw[1] in (0, 1)
            and raw[4] == 1)


def parse_schedules(raw: bytes) -> list[Schedule]:
    """Require a complete recognized payload; do not invent missing slot bytes."""
    if not _valid_schedule_head(raw):
        raise ValueError("unrecognized schedule status/header")
    count = raw[3]
    if count == 0:
        if len(raw) == 5 or (len(raw) == 15 and raw[5:] == bytes(10)):
            return []
        raise ValueError("unrecognized empty schedule payload")
    if not 1 <= count <= 5 or len(raw) != 5 + 10 * count:
        raise ValueError("schedule count and payload length must agree exactly")
    return [Schedule.from_bytes(raw[5 + 10*i:15 + 10*i]) for i in range(count)]


def encode(cmd: int, payload: bytes, declared_len: int | None = None) -> list[bytes]:
    """Fragment commands without interpreting payload bytes; override is for fixtures."""
    body = (DIR_CMD + cmd.to_bytes(2, "big") + CONST_AF
            + bytes([len(payload) if declared_len is None else declared_len]) + payload)
    return [bytes((START_CMD, i // 18 + 1)) + body[i:i+18]
            for i in range(0, len(body), 18)]


def power(on: bool) -> list[bytes]:
    return encode(CMD_POWER, bytes((1, POWER_ON_ARG if on else POWER_OFF_ARG, 0, 0, 0)))


def sound(on: bool) -> list[bytes]:
    return encode(CMD_SOUND, bytes((1, 1 if on else 0)))


def light(mode: int, rgb: tuple[int, int, int] = (0, 0, 0),
          brightness: int = 0x64) -> list[bytes]:
    if not 0 <= mode < len(LIGHT_MODES):
        raise ValueError(f"light mode out of range: {mode}")
    return encode(CMD_LIGHT, bytes((4, *rgb, brightness, mode)))


def sync_time(now: datetime) -> list[bytes]:
    """Sent on connect; observed to elicit full state after its acknowledgement."""
    return encode(CMD_TIME, bytes((now.year % 100, now.month, now.day,
                                  now.isoweekday(), now.hour, now.minute, now.second)))


def encode_schedules(schedules: list[Schedule]) -> list[bytes]:
    """Encode the statically derived app list format, not hardware validated.

    Four/five-slot app normalization emits an extra empty terminal fragment.
    Retain that shape, but never clone its destructive handling of literal 0x25
    in minutes/masks/opaque data. Empty-list deletion has a padded special shape.
    """
    if len(schedules) > 5:
        raise ValueError("at most five schedule slots are supported")
    if any(not isinstance(slot, Schedule) for slot in schedules):
        raise ValueError("every schedule must be a Schedule")
    count = len(schedules)
    if count:
        payload = bytes((1, 1, 1, count, 1)) + b"".join(s.to_bytes() for s in schedules)
    else:
        payload = bytes((1, 0, 0, 0, 1)) + bytes(10)
    packets = encode(CMD_POWER, payload)
    if count in (4, 5):
        packets.append(bytes((START_CMD, len(packets) + 1)))
    return packets


def schedule_from_raw(raw: bytes, *, slot: int = 0,
                      days_mask: int | None = None,
                      start_hour: int | None = None,
                      start_minute: int | None = None,
                      end_hour: int | None = None,
                      end_minute: int | None = None,
                      intensity: int | None = None) -> list[bytes]:
    """Edit an existing zero-based slot using a complete report as the write base."""
    schedules = parse_schedules(raw)
    _integer_range("slot", slot, 0, len(schedules) - 1)
    updates = {key: value for key, value in (
        ("days_mask", days_mask), ("start_hour", start_hour),
        ("start_minute", start_minute), ("end_hour", end_hour),
        ("end_minute", end_minute), ("intensity", intensity),
    ) if value is not None}
    schedules[slot] = replace(schedules[slot], **updates)
    return encode_schedules(schedules)


@dataclass
class State:
    """Decoded fields; a partial ACK is explicitly not a complete schedule list."""

    power: bool | None = None
    days_mask: int | None = None
    start_hour: int | None = None
    start_minute: int | None = None
    end_hour: int | None = None
    end_minute: int | None = None
    intensity: int | None = None
    light_mode: int | None = None
    rgb: tuple[int, int, int] | None = None
    brightness: int | None = None
    sound: bool | None = None
    firmware: str | None = None
    name: str | None = None
    raw: dict[int, bytes] = field(default_factory=dict)
    schedules: list[Schedule] | None = None
    schedule_status: str = "unknown"
    schedule_raw: bytes | None = None
    intensity_presets: tuple[tuple[int, int], ...] | None = None
    run_code: int | None = None

    @property
    def schedule_enabled(self) -> bool | None:
        return None if self.days_mask is None else bool(self.days_mask & 0x80)

    @property
    def days(self) -> list[str]:
        """Bit 0 = Sunday remains a historical inference, not proven mapping."""
        if self.days_mask is None:
            return []
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        return [n for i, n in enumerate(names) if self.days_mask & (1 << i)]

    @property
    def light_mode_name(self) -> str | None:
        if self.light_mode is None or not 0 <= self.light_mode < len(LIGHT_MODES):
            return None
        return LIGHT_MODES[self.light_mode]


def decode_intensity_presets(payload: bytes) -> tuple[tuple[int, int], ...]:
    """Count followed by big-endian spray/pause uint16 seconds pairs."""
    if not payload or not 1 <= payload[0] <= 5 or len(payload) != 1 + 4 * payload[0]:
        raise ValueError("preset count and length must agree")
    pairs = tuple((int.from_bytes(payload[i:i+2], "big"),
                   int.from_bytes(payload[i+2:i+4], "big"))
                  for i in range(1, len(payload), 4))
    if any(on <= 0 or pause <= 0 for on, pause in pairs):
        raise ValueError("preset durations must be positive")
    return pairs


def _apply(state: State, cmd: int, payload: bytes, *, short_ack: bool = False) -> None:
    state.raw[cmd] = payload
    if cmd == CMD_POWER:
        state.schedule_status = "partial" if short_ack else "unparsed"
        if _valid_schedule_head(payload):
            state.power = payload[1] == 1
            state.run_code = payload[2]
        if short_ack:
            return
        try:
            schedules = parse_schedules(payload)
        except ValueError:
            return
        state.schedules = schedules
        state.schedule_raw = payload
        state.schedule_status = "complete"
        if schedules:
            first = schedules[0]
            for name in ("days_mask", "start_hour", "start_minute", "end_hour",
                         "end_minute", "intensity"):
                setattr(state, name, getattr(first, name))
    elif cmd == CMD_INTENSITY_PRESETS:
        try:
            state.intensity_presets = decode_intensity_presets(payload)
        except ValueError:
            pass
    elif cmd == CMD_LIGHT and len(payload) == 6:
        state.rgb = (payload[1], payload[2], payload[3])
        state.brightness = payload[4]
        state.light_mode = payload[5]
    elif cmd == CMD_SOUND and len(payload) == 2:
        state.sound = payload[1] == 1
    elif cmd == CMD_VERSION:
        state.firmware = payload.decode("ascii", "replace").strip("\x00 ")
    elif cmd == CMD_NAME:
        state.name = payload.decode("ascii", "replace").strip("\x00 ")


def _short_schedule_ack(count: int, cmd: int, length: int, payload: bytes) -> bool:
    """Only the observed single 03E9 response: declares 15, carries 11 bytes."""
    if (count != 1 or cmd != CMD_POWER or length != 15 or len(payload) != 11
            or not _valid_schedule_head(payload) or payload[3] != 1):
        return False
    try:
        Schedule.from_bytes(payload[5:] + bytes(4))
    except ValueError:
        return False
    return True


def parse_batch(body: bytes) -> State | None:
    """Parse messages atomically, with one explicitly partial ACK exception.

    Invalid kinds, reserved bytes, counts, excess data and incomplete messages do
    not publish earlier fields from a broken batch. A partial ACK supplies power
    and run code only, never a schedule list or write base.
    """
    if len(body) < 2:
        return None
    count = int.from_bytes(body[:2], "big")
    if count == 0:
        return None
    state = State()
    pos = 2
    for _ in range(count):
        if pos + 5 > len(body):
            return None
        cmd = int.from_bytes(body[pos:pos+2], "big")
        kind, reserved, length = body[pos+2:pos+5]
        expected_kind = 0xBF if cmd in (CMD_VERSION, CMD_NAME) else 0xAF
        if kind != expected_kind or reserved != 0 or cmd in state.raw:
            return None
        payload = body[pos+5:pos+5+length]
        short_ack = len(payload) < length and _short_schedule_ack(count, cmd, length, payload)
        if len(payload) < length and not short_ack:
            return None
        _apply(state, cmd, payload, short_ack=short_ack)
        pos += 5 + len(payload)
    return state if pos == len(body) else None


class Reassembler:
    """Require contiguous fragments; never publish a completed batch twice.

    A new sequence may replace an incomplete sequence only at fragment one.
    A short ACK is indistinguishable from a full reply's first fragment: emit it
    as partial, and accept a contiguous continuation as complete if one follows.
    """

    def __init__(self) -> None:
        self._seq: int | None = None
        self._last_frag = 0
        self._buf = bytearray()
        self._invalid = False
        self._complete = False

    def feed(self, data: bytes) -> State | None:
        if not 2 <= len(data) <= MAX_ATT or data[1] == 0:
            self._invalid = True
            return None
        seq, frag = data[:2]
        if frag == 1 and seq != self._seq:
            self._seq = seq
            self._buf = bytearray(data[2:])
            self._last_frag = 1
            self._invalid = False
            self._complete = False
        elif (self._invalid or self._complete or seq != self._seq
              or frag != self._last_frag + 1):
            self._invalid = True
            return None
        else:
            self._buf.extend(data[2:])
            self._last_frag = frag
        state = parse_batch(bytes(self._buf))
        if state is not None and state.schedule_status != "partial":
            self._complete = True
        return state
