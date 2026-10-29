"""Behavioral coverage for conservative interval and weight estimation."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

SOURCE = Path(__file__).parents[1] / "custom_components/aromatech_ambience/estimation.py"
SPEC = importlib.util.spec_from_file_location("ambience_estimation_under_test", SOURCE)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)
UsageEstimator = module.UsageEstimator
T = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)  # Monday 08:00 Toronto
PRESETS = ((5, 300), (10, 240), (10, 120), (15, 90), (25, 90))
ALL = (255, 0, 0, 23, 59, 2)


def observation(est, now, *, power=True, schedules=(ALL,), presets=PRESETS, **kwargs):
    est.observe(now, power=power, schedules=schedules, presets=presets, **kwargs)


class EstimationTests(unittest.TestCase):
    def test_full_day_non_midnight_endpoints_cover_midnight(self):
        e = self.estimator(gap=90000)
        slots = ((255, 8, 0, 8, 0, 1),)
        observation(e, T, schedules=slots)
        observation(e, T + timedelta(days=1), schedules=slots)
        self.assertAlmostEqual(e.active_seconds, 86400 / 305 * 5)
        self.assertEqual(e.unobserved_seconds, 0)

    def test_mixed_levels_accumulate_active_seconds_not_level_multiples(self):
        e = self.estimator(gap=10000)
        slots = ((255, 8, 0, 8, 40, 1), (255, 8, 40, 10, 40, 3))
        observation(e, T, schedules=slots)
        observation(e, T + timedelta(minutes=160), schedules=slots)
        self.assertAlmostEqual(e.expected_sprays, 2400 / 305 + 7200 / 130)
        self.assertAlmostEqual(e.active_seconds, 2400 / 305 * 5 + 7200 / 130 * 10)
        self.assertEqual(e.unobserved_seconds, 0)

    def estimator(self, gap=7200):
        return UsageEstimator("America/Toronto", gap)

    def test_expected_fractional_cycles_and_active_seconds(self):
        e = self.estimator()
        observation(e, T)
        observation(e, T + timedelta(hours=1), run_code=999)
        self.assertAlmostEqual(e.expected_sprays, 14.4)
        self.assertAlmostEqual(e.active_seconds, 144)
        self.assertEqual(e.observed_seconds, 3600)
        self.assertEqual(e.coverage, 1)
        self.assertEqual(e.last_run_code, 999)

    def test_halfopen_minutes_and_partial_seconds(self):
        e = self.estimator()
        schedule = ((255, 8, 1, 8, 2, 2),)
        begin = T + timedelta(seconds=30)
        observation(e, begin, schedules=schedule)
        observation(e, T + timedelta(minutes=2, seconds=30), schedules=schedule)
        self.assertEqual(e.observed_seconds, 120)
        self.assertEqual(e.unobserved_seconds, 0)
        self.assertAlmostEqual(e.active_seconds, 2.4)

    def test_day_mask_and_disabled_schedule(self):
        for mask, observed in ((0x82, 60), (0x81, 60), (0x02, 60)):
            with self.subTest(mask=mask):
                e = self.estimator()
                slots = ((mask, 8, 0, 9, 0, 1),)
                observation(e, T, schedules=slots)
                observation(e, T + timedelta(minutes=1), schedules=slots)
                self.assertEqual(e.observed_seconds, observed)

    def test_local_midnight_changes_inferred_weekday(self):
        e = self.estimator()
        begin = datetime(2026, 10, 27, 3, 59, tzinfo=timezone.utc)
        slots = ((0x84, 0, 0, 0, 1, 2),)  # Tuesday only
        observation(e, begin, schedules=slots)
        observation(e, begin + timedelta(minutes=2), schedules=slots)
        self.assertEqual(e.observed_seconds, 120)
        self.assertEqual(e.unobserved_seconds, 0)

    def test_multiple_adjacent_slots_and_level_five(self):
        e = self.estimator()
        slots = ((255, 8, 0, 8, 1, 2), (255, 8, 1, 8, 2, 5))
        observation(e, T, schedules=slots)
        observation(e, T + timedelta(minutes=2), schedules=slots)
        self.assertEqual(e.observed_seconds, 120)
        self.assertAlmostEqual(e.expected_sprays, 60 / 250 + 60 / 115)
        self.assertAlmostEqual(e.active_seconds, 60 / 250 * 10 + 60 / 115 * 25)

    def test_overlap_never_double_counts(self):
        e = self.estimator()
        slots = ((255, 8, 0, 8, 2, 2), (255, 8, 1, 8, 3, 5))
        observation(e, T, schedules=slots)
        observation(e, T + timedelta(minutes=3), schedules=slots)
        self.assertEqual(e.observed_seconds, 120)
        self.assertEqual(e.unobserved_seconds, 60)

    def test_overnight_and_empty_are_unknown(self):
        for slots in ((), ((255, 22, 0, 6, 0, 2),), ((255, 0, 0, 0, 0, 2),)):
            with self.subTest(slots=slots):
                e = self.estimator()
                observation(e, T, schedules=slots)
                observation(e, T + timedelta(minutes=1), schedules=slots)
                self.assertEqual(e.unobserved_seconds, 60)
                self.assertEqual(e.active_seconds, 0)

    def test_power_off_known_zero_without_presets(self):
        e = self.estimator()
        for t in (T, T + timedelta(minutes=1)):
            observation(e, t, power=False, schedules=None, presets=None)
        self.assertEqual(e.observed_seconds, 60)
        self.assertEqual(e.expected_sprays, 0)
        observation(e, T + timedelta(minutes=2))
        self.assertEqual(e.unobserved_seconds, 60)

    def test_changes_presets_power_or_schedule_are_unknown(self):
        changes = ({"power": False}, {"schedules": ((255, 9, 0, 10, 0, 2),)},
                   {"presets": ((5, 300), (10, 200))})
        for change in changes:
            with self.subTest(change=change):
                e = self.estimator()
                observation(e, T)
                observation(e, T + timedelta(minutes=1), **change)
                self.assertEqual(e.unobserved_seconds, 60)

    def test_failed_poll_preserves_counting_with_uncertainty(self):
        for fail in (False, True):
            e = self.estimator()
            observation(e, T)
            if fail:
                observation(e, T + timedelta(seconds=30), complete=False)
            else:
                e.invalidate(T + timedelta(seconds=30))
            observation(e, T + timedelta(seconds=60))
            self.assertEqual(e.unobserved_seconds, 30)
            self.assertAlmostEqual(e.active_seconds, 60 / 250 * 10)
            observation(e, T + timedelta(seconds=90))
            self.assertEqual(e.observed_seconds, 60)

    def test_gap_limit_and_recovery(self):
        e = self.estimator(60)
        observation(e, T)
        observation(e, T + timedelta(seconds=61))
        observation(e, T + timedelta(seconds=121))
        self.assertEqual(e.unobserved_seconds, 1)
        self.assertEqual(e.observed_seconds, 120)
        self.assertAlmostEqual(e.active_seconds, 121 / 250 * 10)

    def test_clock_backwards_does_not_double_count(self):
        e = self.estimator()
        observation(e, T)
        observation(e, T + timedelta(seconds=60))
        observation(e, T + timedelta(seconds=30))
        observation(e, T + timedelta(seconds=90))
        self.assertEqual(e.observed_seconds, 90)
        self.assertEqual(e.unobserved_seconds, 0)
        observation(e, T + timedelta(seconds=90))
        self.assertEqual(e.observed_seconds, 90)

    def test_dst_actual_elapsed_seconds_count_once(self):
        for begin in (datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc),
                      datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)):
            e = self.estimator()
            observation(e, begin)
            observation(e, begin + timedelta(minutes=2))
            self.assertEqual(e.unobserved_seconds, 0)
            self.assertEqual(e.observed_seconds, 120)
            self.assertAlmostEqual(e.active_seconds, 120 / 250 * 10)

    def test_malformed_or_nonfinite_presets_are_unknown(self):
        for presets in (None, (), ((0, 60),), ((float("nan"), 60),),
                        ((1, float("inf")),), ((1, -1),), ((1,),)):
            with self.subTest(presets=presets):
                e = self.estimator()
                observation(e, T, presets=presets)
                observation(e, T + timedelta(seconds=60), presets=presets)
                self.assertEqual(e.unobserved_seconds, 60)

    def test_schedule_object_contract_preserves_unknown_signature(self):
        e = self.estimator()
        slot = SimpleNamespace(days_mask=255, start_hour=8, start_minute=0,
                               end_hour=9, end_minute=0, intensity=2, unknown=b"1234")
        observation(e, T, schedules=(slot,))
        slot.unknown = b"5678"
        observation(e, T + timedelta(minutes=1), schedules=(slot,))
        self.assertEqual(e.unobserved_seconds, 60)

    def test_invalid_schedule_shapes_are_unknown(self):
        for slots in (((255, 24, 0, 25, 0, 2),), ((255, 8, 60, 9, 0, 2),),
                      ((255, 8, 0, 9, 0, 6),), ((255,),), (ALL,) * 6):
            with self.subTest(slots=slots):
                e = self.estimator()
                observation(e, T, schedules=slots)
                observation(e, T + timedelta(minutes=1), schedules=slots)
                self.assertEqual(e.unobserved_seconds, 60)

    def test_restart_restores_anchor_and_catches_up_uncertain(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        observation(e, T + timedelta(minutes=1))
        e.record_weight(T + timedelta(minutes=1), 199, 100)
        state = json.loads(json.dumps(e.to_dict(), allow_nan=False))
        fresh = self.estimator()
        fresh.restore(state)
        observation(fresh, T + timedelta(days=1))
        self.assertEqual(fresh.observed_seconds, 60)
        self.assertEqual(fresh.unobserved_seconds, 86340)
        self.assertGreater(fresh.active_seconds, e.active_seconds)
        self.assertIsNone(fresh.estimated_remaining_g)
        self.assertEqual(fresh.calibration_status, "unobserved_since_calibration")
        self.assertAlmostEqual(fresh.grams_per_active_second, e.grams_per_active_second)
        observation(fresh, T + timedelta(days=1, seconds=60))
        self.assertEqual(fresh.observed_seconds, 120)

    def test_restart_weight_reanchors_rate_without_learning_downtime(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        observation(e, T + timedelta(seconds=250))
        e.record_weight(T + timedelta(seconds=250), 199, 100)
        fresh = self.estimator()
        fresh.restore(e.to_dict())
        later = T + timedelta(days=1)
        observation(fresh, later)
        fresh.record_weight(later, 150, 100)
        self.assertAlmostEqual(fresh.grams_per_active_second, .1)
        self.assertEqual(fresh.estimated_remaining_g, 50)
        self.assertEqual(fresh.calibration_status, "calibrated_reanchored")
        observation(fresh, later + timedelta(seconds=250))
        self.assertAlmostEqual(fresh.estimated_remaining_g, 49)

    def test_restart_new_bottle_clears_learned_rate(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        observation(e, T + timedelta(seconds=250))
        e.record_weight(T + timedelta(seconds=250), 199, 100)
        for gross, empty in ((201, 100), (150, 99)):
            with self.subTest(gross=gross, empty=empty):
                fresh = self.estimator()
                fresh.restore(e.to_dict())
                later = T + timedelta(days=1)
                observation(fresh, later)
                fresh.record_weight(later, gross, empty)
                self.assertIsNone(fresh.grams_per_active_second)
                self.assertIsNone(fresh.estimated_remaining_g)
                self.assertEqual(fresh.calibration_status, "new_bottle_checkpoint")

    def test_restart_uncalibrated_cannot_learn_across_downtime(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        observation(e, T + timedelta(seconds=250))
        fresh = self.estimator()
        fresh.restore(e.to_dict())
        later = T + timedelta(days=1)
        observation(fresh, later)
        fresh.record_weight(later, 150, 100)
        self.assertIsNone(fresh.grams_per_active_second)
        self.assertEqual(fresh.calibration_status, "needs_second_weight")

    def test_weights_calibrate_and_deplete_after_valid_interval(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        self.assertIsNone(e.estimated_remaining_g)
        observation(e, T + timedelta(seconds=250))
        e.record_weight(T + timedelta(seconds=250), 199, 100)
        self.assertAlmostEqual(e.grams_per_active_second, 0.1)
        self.assertEqual(e.estimated_remaining_g, 99)
        observation(e, T + timedelta(seconds=500))
        self.assertAlmostEqual(e.estimated_remaining_g, 98)

    def test_increasing_weight_or_tare_change_resets_rate_not_totals(self):
        for gross, tare in ((201, 100), (201, 101)):
            e = self.estimator()
            observation(e, T)
            e.record_weight(T, 200, 100)
            observation(e, T + timedelta(seconds=250))
            e.record_weight(T + timedelta(seconds=250), 199, 100)
            e.record_weight(T + timedelta(seconds=250), gross, tare)
            self.assertIsNone(e.estimated_remaining_g)
            self.assertAlmostEqual(e.active_seconds, 10)
            self.assertEqual(e.calibration_status, "new_bottle_checkpoint")

    def test_missing_coverage_prevents_rate_and_remaining(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        observation(e, T + timedelta(seconds=250))
        e.record_weight(T + timedelta(seconds=250), 199, 100)
        e.invalidate(T + timedelta(seconds=251))
        e.advance(T + timedelta(seconds=252))
        self.assertIsNone(e.estimated_remaining_g)
        observation(e, T + timedelta(seconds=500))
        e.record_weight(T + timedelta(seconds=500), 198, 100)
        self.assertEqual(e.calibration_status, "insufficient_coverage")
        self.assertIsNone(e.grams_per_active_second)

    def test_zero_active_or_zero_loss_does_not_calibrate(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        e.record_weight(T, 199, 100)
        self.assertEqual(e.calibration_status, "no_active_time")
        observation(e, T + timedelta(seconds=250))
        e.record_weight(T + timedelta(seconds=250), 199, 100)
        self.assertEqual(e.calibration_status, "no_measured_loss")
        self.assertIsNone(e.estimated_remaining_g)

    def test_reject_invalid_weights_and_backdating_without_mutation(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        before = e.to_dict()
        for gross, empty in ((float("nan"), 1), (1, float("inf")), (-1, 0),
                             (0, 0), (10, 11), (10001, 0), (10**400, 0),
                             (10, -1), (True, 0)):
            with self.subTest(gross=gross, empty=empty), self.assertRaises(ValueError):
                e.record_weight(T, gross, empty)
            self.assertEqual(e.to_dict(), before)
        with self.assertRaises(ValueError):
            e.record_weight(T - timedelta(seconds=1), 199, 100)

    def test_late_checkpoint_advances_known_interval(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        observation(e, T + timedelta(seconds=250))
        e.record_weight(T + timedelta(seconds=251), 199, 100)
        self.assertEqual(e.unobserved_seconds, 0)
        self.assertEqual(e.calibration_status, "calibrated")

    def test_restore_rejects_bad_totals_atomically(self):
        e = self.estimator()
        saved = e.to_dict()
        for key, value in (("active_seconds", float("nan")), ("observed_seconds", -1),
                           ("expected_sprays", float("inf")), ("active_seconds", 1)):
            bad = dict(saved, **{key: value})
            with self.assertRaises(ValueError):
                e.restore(bad)
            self.assertEqual(e.to_dict(), saved)

    def test_restore_rejects_invalid_rate_and_checkpoint_atomically(self):
        e = self.estimator()
        observation(e, T)
        e.record_weight(T, 200, 100)
        saved = e.to_dict()
        for changes in ({"grams_per_active_second": float("nan")},
                        {"grams_per_active_second": -1},
                        {"checkpoint": {}},
                        {"checkpoint": dict(saved["checkpoint"], net_g=101)},
                        {"checkpoint": dict(saved["checkpoint"], active_seconds=1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                e.restore(dict(saved, **changes))
            self.assertEqual(e.to_dict(), saved)

    def test_naive_time_rejected(self):
        e = self.estimator()
        with self.assertRaises(ValueError):
            observation(e, datetime(2026, 10, 26))


if __name__ == "__main__":
    unittest.main()
