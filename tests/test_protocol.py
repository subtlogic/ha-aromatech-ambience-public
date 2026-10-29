"""Regression contracts for Ambience schedule encoding and state decoding."""

import importlib.util
from pathlib import Path
import sys
import unittest


SOURCE = Path(__file__).parents[1] / "custom_components/aromatech_ambience/protocol.py"
SPEC = importlib.util.spec_from_file_location("ambience_protocol_under_test", SOURCE)
protocol = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = protocol
SPEC.loader.exec_module(protocol)


class ScheduleWriteTest(unittest.TestCase):
    def test_intensity_two_completes_second_fragment(self):
        reported = bytes.fromhex("01 01 00 01 01 ff 08 00 12 00 01 00 00 00 00")

        frames = protocol.schedule_from_raw(reported, intensity=2)

        self.assertEqual(frames, [
            bytes.fromhex("25 01 ff 01 03 e9 af 00 0f 01 01 01 01 01 ff 08 00 12 00 02"),
            bytes.fromhex("25 02 00 00 00 00"),
        ])
        self.assertEqual(reported[5:11], bytes.fromhex("ff 08 00 12 00 01"))

    def test_edit_preserves_other_schedule_fields(self):
        reported = bytes.fromhex("01 00 03 01 01 82 06 00 14 00 01 00 00 00 00")

        frames = protocol.schedule_from_raw(reported, intensity=4)

        self.assertEqual(frames[0][9:19], bytes.fromhex("01 01 01 01 01 82 06 00 14 00"))
        self.assertEqual(frames[0][19], 4)
        self.assertEqual(frames[1], bytes.fromhex("25 02 00 00 00 00"))


def batch(cmd, payload, *, kind=0xAF, reserved=0, length=None):
    return b"\x00\x01" + cmd.to_bytes(2, "big") + bytes((kind, reserved, len(payload) if length is None else length)) + payload


def report_payload(frames):
    return b"".join(frame[2:] for frame in frames)[7:]


class ScheduleListTest(unittest.TestCase):
    a = protocol.Schedule(255, 6, 30, 8, 45, 2)
    b = protocol.Schedule(255, 18, 15, 20, 0, 3)

    def test_exact_static_one_two_four_five_slot_frames(self):
        fixtures = {
            1: ["2501ff0103e9af000f0101010101ff061e082d02", "250200000000"],
            2: ["2501ff0103e9af00190101010201ff061e082d02", "250200000000ff120f14000300000000"],
            4: ["2501ff0103e9af002d0101010401ff061e082d02", "250200000000ff120f14000300000000ff061e08", "25032d0200000000ff120f14000300000000", "2504"],
            5: ["2501ff0103e9af00370101010501ff061e082d02", "250200000000ff120f14000300000000ff061e08", "25032d0200000000ff120f14000300000000ff06", "25041e082d0200000000", "2505"],
        }
        slots = [self.a, self.b, self.a, self.b, self.a]
        for count, expected in fixtures.items():
            with self.subTest(count=count):
                frames = protocol.encode_schedules(slots[:count])
                self.assertEqual(frames, list(map(bytes.fromhex, expected)))
                self.assertEqual(protocol.parse_schedules(report_payload(frames)), slots[:count])

    def test_delete_all_and_empty_report(self):
        frames = protocol.encode_schedules([])
        self.assertEqual(frames, [bytes.fromhex("2501ff0103e9af000f0100000001000000000000"), bytes.fromhex("250200000000")])
        for raw in (bytes.fromhex("0100000001"), report_payload(frames)):
            state = protocol.parse_batch(batch(protocol.CMD_POWER, raw))
            self.assertEqual(state.schedules, [])
            self.assertEqual(state.schedule_status, "complete")
            self.assertEqual(state.schedule_raw, raw)

    def test_indexed_edit_preserves_minutes_other_slots_and_opaque_bytes(self):
        first = protocol.Schedule(0xA5, 6, 37, 8, 37, 5, bytes.fromhex("2501ff42"))
        raw = bytes((1, 0, 3, 2, 1)) + first.to_bytes() + self.b.to_bytes()
        edited = report_payload(protocol.schedule_from_raw(raw, slot=1, start_minute=37, intensity=4))
        slots = protocol.parse_schedules(edited)
        self.assertEqual(slots[0], first)
        self.assertEqual(slots[1], protocol.Schedule(255, 18, 37, 20, 0, 4))
        self.assertEqual(edited[:5], bytes((1, 1, 1, 2, 1)))
        self.assertEqual(protocol.Schedule.from_bytes(first.to_bytes()), first)

    def test_invalid_payloads_never_become_write_bases(self):
        valid = bytes((1, 1, 0, 1, 1)) + self.a.to_bytes()
        invalid = [b"", valid[:-1], valid + b"\0", bytes((1, 1, 0, 2, 1)) + self.a.to_bytes(),
                   bytes((1, 1, 0, 6, 1)) + self.a.to_bytes() * 6,
                   bytes((1, 1, 0, 0, 1)) + self.a.to_bytes(),
                   bytes((1, 1, 0, 1, 0)) + self.a.to_bytes()]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    protocol.parse_schedules(raw)
                with self.assertRaises(ValueError):
                    protocol.schedule_from_raw(raw, intensity=2)
                state = protocol.parse_batch(batch(protocol.CMD_POWER, raw))
                self.assertEqual(state.schedule_status, "unparsed")
                self.assertIsNone(state.schedules)
                self.assertIsNone(state.schedule_raw)
        for slot in (-1, 1, True):
            with self.assertRaises(ValueError):
                protocol.schedule_from_raw(valid, slot=slot, intensity=2)
        with self.assertRaises(ValueError):
            protocol.encode_schedules([self.a] * 6)

    def test_slot_validation(self):
        for changes in ({"start_hour": 24}, {"end_hour": -1}, {"start_minute": 60},
                        {"end_minute": 60}, {"intensity": 0}, {"intensity": 6},
                        {"days_mask": 256}, {"unknown": b"\0"}, {"unknown": bytearray(4)}):
            args = dict(days_mask=255, start_hour=6, start_minute=30, end_hour=8, end_minute=45, intensity=2)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                protocol.Schedule(**(args | changes))


class BatchTest(unittest.TestCase):
    raw = bytes.fromhex("0101030101ff06250825052501ff42")

    def test_unknown_run_code_is_preserved_without_losing_schedule(self):
        raw = self.raw[:2] + b"\x09" + self.raw[3:]
        state = protocol.parse_batch(batch(protocol.CMD_POWER, raw))
        self.assertEqual(state.run_code, 9)
        self.assertEqual(state.schedule_status, "complete")
        self.assertEqual(state.schedule_raw, raw)
        self.assertEqual(state.schedules, protocol.parse_schedules(self.raw))

    def test_complete_exposes_minutes_run_code_and_preserved_raw(self):
        state = protocol.parse_batch(batch(protocol.CMD_POWER, self.raw))
        self.assertEqual(state.schedule_status, "complete")
        self.assertEqual(state.schedule_raw, self.raw)
        self.assertEqual((state.start_minute, state.end_minute, state.run_code), (37, 37, 3))
        self.assertTrue(state.power)
        self.assertEqual(state.schedules[0].unknown, bytes.fromhex("2501ff42"))

    def test_only_observed_short_ack_is_accepted(self):
        state = protocol.parse_batch(batch(protocol.CMD_POWER, self.raw[:11], length=15))
        self.assertEqual(state.schedule_status, "partial")
        self.assertIsNone(state.schedule_raw)
        self.assertIsNone(state.schedules)
        self.assertIsNone(state.intensity)
        for size in (0, 5, 10, 12, 13, 14):
            self.assertIsNone(protocol.parse_batch(batch(protocol.CMD_POWER, self.raw[:size], length=15)))
        self.assertIsNone(protocol.parse_batch(batch(protocol.CMD_LIGHT, self.raw[:11], length=15)))
        self.assertIsNone(protocol.parse_batch(batch(protocol.CMD_POWER, self.raw[:11], length=16)))

    def test_rejects_invalid_framing_atomically(self):
        good = batch(protocol.CMD_SOUND, b"\x01\x01")
        malformed = [b"", b"\0\0", good + b"\0", good[:1], good[:-1],
                     batch(protocol.CMD_SOUND, b"\1\1", reserved=1),
                     batch(protocol.CMD_SOUND, b"\1\1", kind=0xBF),
                     batch(protocol.CMD_NAME, b"name", kind=0xAF),
                     b"\0\2" + good[2:], b"\0\2" + good[2:] * 2,
                     b"\0\2" + good[2:] + batch(protocol.CMD_POWER, self.raw[:11], length=15)[2:]]
        for raw in malformed:
            with self.subTest(raw=raw):
                self.assertIsNone(protocol.parse_batch(raw))
        self.assertTrue(protocol.parse_batch(good).sound)
        self.assertEqual(protocol.parse_batch(batch(protocol.CMD_NAME, b"Ambience", kind=0xBF)).name, "Ambience")

    def test_presets_use_big_endian_and_validate_complete_pairs(self):
        payload = bytes.fromhex("02003c00780078012c")
        self.assertEqual(protocol.decode_intensity_presets(payload), ((60, 120), (120, 300)))
        self.assertEqual(protocol.parse_batch(batch(protocol.CMD_INTENSITY_PRESETS, payload)).intensity_presets,
                         ((60, 120), (120, 300)))
        for bad in (b"", b"\0", payload[:-1], payload + b"\0", bytes.fromhex("0100000078")):
            with self.assertRaises(ValueError):
                protocol.decode_intensity_presets(bad)
            self.assertIsNone(protocol.parse_batch(batch(protocol.CMD_INTENSITY_PRESETS, bad)).intensity_presets)

    def test_reassembler_complete_and_partial_continuation(self):
        body = batch(protocol.CMD_POWER, self.raw)
        receiver = protocol.Reassembler()
        first = bytes((7, 1)) + body[:18]
        last = bytes((7, 2)) + body[18:]
        self.assertEqual(receiver.feed(first).schedule_status, "partial")
        self.assertEqual(receiver.feed(last).schedule_raw, self.raw)
        self.assertIsNone(receiver.feed(last))
        self.assertTrue(receiver.feed(bytes((8, 1)) + batch(protocol.CMD_SOUND, b"\1\1")).sound)

    def test_reassembler_rejects_duplicate_missing_out_of_order_and_oversized(self):
        raw = bytes((1, 1, 0, 5, 1)) + protocol.Schedule(255, 6, 30, 8, 45, 2).to_bytes() * 5
        body = batch(protocol.CMD_POWER, raw)
        frames = [bytes((9, i // 18 + 1)) + body[i:i+18] for i in range(0, len(body), 18)]
        for indexes in ((0, 0, 1, 2, 3), (0, 2, 3), (1, 0, 2, 3), (0, 2, 1, 3)):
            receiver = protocol.Reassembler()
            for index in indexes:
                self.assertIsNone(receiver.feed(frames[index]))
        receiver = protocol.Reassembler()
        for frame in frames[:-1]:
            self.assertIsNone(receiver.feed(frame))
        self.assertEqual(len(receiver.feed(frames[-1]).schedules), 5)
        for invalid in (b"", b"\1", b"\1\0", bytes(21)):
            self.assertIsNone(protocol.Reassembler().feed(invalid))


if __name__ == "__main__":
    unittest.main()
