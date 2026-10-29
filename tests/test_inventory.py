"""Pure inventory ledger invariants, independent of Home Assistant."""
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location(
    "inventory_under_test", Path(__file__).parents[1] / "custom_components/aromatech_ambience/inventory.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
Inventory = MODULE.Inventory
T = datetime(2026, 10, 28, tzinfo=timezone.utc)
DAILY = [(255, 8, 0, 9, 0, 1)]
PRESETS = [(10, 50)]


class InventoryTests(unittest.TestCase):
    def test_mixed_duration_consumption_and_full_day_forecast(self):
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        first, second = 2400 / 305, 7200 / 130
        self.inv.apply_usage(first, first * 5, 0)
        self.inv.apply_usage(second, second * 10, 0)
        summary = self.inv.summary([(255, 8, 0, 8, 0, 1)], [(5, 300)])
        active = first * 5 + second * 10
        self.assertAlmostEqual(summary["fill_active_seconds"], active)
        self.assertAlmostEqual(summary["fill_consumed_g"], active * .1)
        self.assertAlmostEqual(summary["remaining_g"], 100 - active * .1)
        self.assertEqual(summary["rate_mg_per_second"], 100)
        self.assertEqual(summary["five_second_dose_mg"], 500)
        self.assertAlmostEqual(summary["daily_g"], 86400 / 305 * 5 * .1)

    def setUp(self):
        self.inv = Inventory()
        self.time = T

    def action(self, action, **fields):
        self.time += timedelta(seconds=1)
        return self.inv.execute(action, self.time, **fields)

    def scent(self, name="Scent", rate=True):
        ident = self.action("add_scent", name=name, **(
            {"grams_per_spray": 1, "spray_seconds": 10} if rate else {}))
        if not rate:
            # Existing installations can still have an uncalibrated scent. New
            # name-only creations now get a default; exercise the legacy restore.
            stored = self.inv.to_dict()
            for key in ("grams_per_spray", "spray_seconds", "rate_g_per_second", "rate_source"):
                stored["scents"][ident][key] = None
            self.inv.restore(stored)
        return ident

    def bottle(self, scent=None, oil=100):
        return self.action("add_bottle", name="Bottle", empty_g=20,
                           scent_id=scent or self.scent(), oil_g=oil)

    def fill(self, bottle):
        return self.inv.bottles[bottle]["fills"][-1]

    def test_name_only_scent_and_unweighed_bottle_produce_honest_estimates(self):
        scent = self.action("add_scent", name="New scent")
        bottle = self.action("add_bottle", name="New bottle", scent_id=scent)
        self.assertIsNone(self.inv.bottles[bottle]["empty_g"])
        self.assertEqual(self.fill(bottle)["initial_g"], 60)
        self.assertIsNone(self.fill(bottle)["checkpoint"])
        self.action("install_bottle", bottle_id=bottle)
        summary = self.inv.summary([(255, 8, 0, 20, 0, 2)], [(5, 300), (10, 240)])
        self.assertEqual(summary["five_second_dose_mg"], 4)
        self.assertEqual(summary["rate_source"], "default_estimate")
        self.assertEqual(summary["mass_basis"], "initial_estimate")
        self.assertAlmostEqual(summary["daily_g"], 1.3824)
        self.assertAlmostEqual(summary["days_remaining"], 60 / 1.3824)
        self.inv.apply_usage(1, 10, 0)
        self.assertAlmostEqual(self.fill(bottle)["remaining_g"], 59.992)
        stored = self.inv.to_dict()
        self.inv.restore(deepcopy(stored))
        self.assertEqual(self.inv.to_dict(), stored)
        with self.assertRaisesRegex(ValueError, "empty bottle weight"):
            self.action("record_weight", bottle_id=bottle, gross_g=80)
        with self.assertRaisesRegex(ValueError, "empty bottle weight"):
            self.action("correct_weight", bottle_id=bottle, gross_g=80, reason="Correction")
        self.action("edit_bottle", bottle_id=bottle, empty_g=20)
        self.action("record_weight", bottle_id=bottle, gross_g=79)
        self.assertEqual(self.fill(bottle)["remaining_g"], 59)
        self.assertEqual(self.inv.summary()["mass_basis"], "weighed")
        self.assertEqual(self.fill(bottle)["rate_source"], "default_estimate")
        self.inv.apply_usage(1, 10, 0)
        self.action("record_weight", bottle_id=bottle, gross_g=78.99)
        self.assertEqual(self.fill(bottle)["rate_source"], "learned")

    def test_corrections_are_not_presented_as_measured_weight(self):
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        self.assertEqual(self.inv.summary()["mass_basis"], "initial_estimate")
        self.action("correct_weight", bottle_id=bottle, gross_g=119, reason="Projected starting balance")
        self.assertEqual(self.inv.summary()["mass_basis"], "corrected")
        self.assertIsNone(self.fill(bottle)["checkpoint"])
        self.action("record_weight", bottle_id=bottle, gross_g=118)
        self.assertEqual(self.inv.summary()["mass_basis"], "weighed")
        self.action("correct_weight", bottle_id=bottle, gross_g=117, reason="Correct current balance")
        self.assertEqual(self.inv.summary()["mass_basis"], "corrected")
        self.assertIsNone(self.fill(bottle)["checkpoint"])

    def test_default_rate_only_for_new_scents_and_preserves_existing_fill(self):
        scent = self.action("add_scent", name="Default")
        bottle = self.bottle(scent)
        before = deepcopy(self.inv.scents[scent])
        self.action("edit_scent", scent_id=scent, name="Renamed")
        self.assertEqual(self.inv.scents[scent], {**before, "name": "Renamed"})
        self.action("edit_scent", scent_id=scent, grams_per_spray=.01, spray_seconds=5)
        self.assertEqual(self.fill(bottle)["rate_g_per_second"], .0008)
        self.assertEqual(self.fill(bottle)["rate_source"], "default_estimate")
        self.assertEqual(self.inv.scents[scent]["rate_source"], "provisional")
        stored = self.inv.to_dict()
        self.inv.restore(deepcopy(stored))
        self.assertEqual(self.inv.to_dict(), stored)

    def test_unknown_tare_and_measured_zero_are_distinct(self):
        scent = self.action("add_scent", name="Default")
        with self.assertRaisesRegex(ValueError, "empty bottle weight"):
            self.action("add_bottle", name="Unknown", scent_id=scent, gross_g=60)
        bottle = self.action("add_bottle", name="Zero tare", scent_id=scent, empty_g=0, gross_g=60)
        self.assertEqual(self.inv.bottles[bottle]["empty_g"], 0)
        self.action("install_bottle", bottle_id=bottle)
        self.action("record_weight", gross_g=59)
        self.assertEqual(self.fill(bottle)["remaining_g"], 59)

    def test_explicit_starting_amounts_and_refill_default(self):
        scent = self.action("add_scent", name="Default")
        for mass in (30, 60, 120, 47.5, 0):
            bottle = self.action("add_bottle", name="Preset", scent_id=scent, oil_g=mass)
            self.assertEqual(self.fill(bottle)["initial_g"], mass)
        self.action("refill_bottle", bottle_id=bottle, scent_id=scent)
        self.assertEqual(self.fill(bottle)["initial_g"], 60)
        self.assertIsNone(self.fill(bottle)["checkpoint"])
        with self.assertRaisesRegex(ValueError, "both|exactly one"):
            self.action("refill_bottle", bottle_id=bottle, scent_id=scent, oil_g=60, gross_g=80)

    def test_swap_a_b_a_preserves_stored_balance_and_counts(self):
        a, b = self.bottle(), self.bottle()
        self.action("install_bottle", bottle_id=a)
        self.inv.apply_usage(2, 20, 0)
        self.action("install_bottle", bottle_id=b)
        self.inv.apply_usage(3, 30, 0)
        self.action("install_bottle", bottle_id=a)
        self.inv.apply_usage(1, 10, 0)
        self.assertEqual(self.fill(a)["cycles"], 3)
        self.assertEqual(self.fill(a)["remaining_g"], 97)
        self.assertEqual(self.fill(b)["cycles"], 3)
        self.assertEqual(self.fill(b)["remaining_g"], 97)
        self.action("store_bottle")
        self.inv.apply_usage(100, 1000, 1000)
        self.assertEqual(self.fill(a)["cycles"], 3)

    def test_names_duplicate_and_rename_keep_ids_counts(self):
        scent = self.scent()
        other = self.scent()
        self.assertNotEqual(scent, other)
        bottle = self.bottle(scent)
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(2, 20, 0)
        self.action("edit_scent", scent_id=scent, name="Renamed", brand="Brand")
        self.action("edit_bottle", bottle_id=bottle, name="Renamed bottle")
        summary = self.inv.summary()
        self.assertEqual(summary["active_scent"], "Renamed")
        self.assertEqual(summary["active_bottle"], "Renamed bottle")
        self.assertEqual(summary["fill_sprays"], 2)

    def test_refill_retains_history_and_changes_scent(self):
        a, b = self.scent(), self.scent("Other")
        bottle = self.bottle(a)
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(1, 10, 0)
        with self.assertRaises(ValueError):
            self.action("refill_bottle", bottle_id=bottle, scent_id=b, oil_g=50)
        self.action("store_bottle")
        old = deepcopy(self.fill(bottle))
        self.action("refill_bottle", bottle_id=bottle, scent_id=b, gross_g=70)
        fills = self.inv.bottles[bottle]["fills"]
        self.assertEqual(fills[0], old)
        self.assertEqual(fills[1]["initial_g"], 50)
        self.assertNotEqual(fills[0]["id"], fills[1]["id"])
        self.assertEqual(fills[1]["scent_id"], b)

    def test_default_rate_edit_only_seeds_future_fills(self):
        scent = self.scent()
        bottle = self.bottle(scent)
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(1, 10, 0)
        self.action("edit_scent", scent_id=scent, grams_per_spray=2, spray_seconds=10)
        self.assertEqual(self.fill(bottle)["remaining_g"], 99)
        self.inv.apply_usage(1, 10, 0)
        self.assertEqual(self.fill(bottle)["remaining_g"], 98)
        self.assertEqual(self.fill(bottle)["consumed_g"], 2)
        new_bottle = self.bottle(scent)
        self.assertEqual(self.fill(new_bottle)["rate_g_per_second"], .2)
        self.assertEqual(self.fill(bottle)["rate_g_per_second"], .1)

    def test_learned_rate_changes_this_fill_and_future_defaults_only(self):
        scent = self.scent()
        a, b = self.bottle(scent), self.bottle(scent)
        self.action("install_bottle", bottle_id=a)
        self.action("record_weight", gross_g=120)
        self.inv.apply_usage(1, 10, 0)
        self.action("record_weight", gross_g=118)
        self.assertEqual(self.fill(a)["rate_g_per_second"], .2)
        self.assertFalse(self.fill(a)["rate_inherited"])
        self.assertEqual(self.fill(b)["rate_g_per_second"], .1)
        self.assertEqual(self.fill(a)["consumed_g"], 1)
        c = self.bottle(scent)
        self.assertEqual(self.fill(c)["rate_g_per_second"], .2)
        self.assertTrue(self.fill(c)["rate_inherited"])
        self.inv.apply_usage(1, 10, 0)
        self.assertEqual(self.fill(a)["consumed_g"], 3)

    def test_metadata_does_not_initialize_legacy_takeover(self):
        bottle = self.bottle()
        self.assertFalse(self.inv.initialized)
        restored = Inventory()
        restored.restore(self.inv.to_dict())
        self.assertFalse(restored.initialized)
        self.action("install_bottle", bottle_id=bottle)
        self.assertTrue(self.inv.initialized)
        self.action("store_bottle")
        self.assertTrue(self.inv.initialized)

    def test_learning_only_covered_positive_weight_interval(self):
        scent = self.scent(rate=False)
        bottle = self.bottle(scent)
        self.action("install_bottle", bottle_id=bottle)
        self.action("record_weight", gross_g=120)
        self.inv.apply_usage(2, 20, 0)
        self.assertEqual(self.inv.summary()["status"], "needs_weighing")
        self.action("record_weight", gross_g=118)
        self.assertEqual(self.inv.scents[scent]["rate_g_per_second"], .1)
        self.assertEqual(self.inv.scents[scent]["rate_source"], "learned")
        self.assertEqual(self.fill(bottle)["remaining_g"], 98)
        self.assertEqual(self.fill(bottle)["consumed_g"], 0)  # No backpricing.
        self.inv.apply_usage(1, 10, 0)
        self.assertEqual(self.fill(bottle)["remaining_g"], 97)

    def test_unknown_no_active_or_weight_increase_do_not_learn(self):
        for delta, gross in [((1, 10, 1), 119), ((0, 0, 0), 119), ((1, 10, 0), 121)]:
            with self.subTest(delta=delta, gross=gross):
                self.setUp()
                scent = self.scent(rate=False)
                bottle = self.bottle(scent)
                self.action("install_bottle", bottle_id=bottle)
                self.action("record_weight", gross_g=120)
                self.inv.apply_usage(*delta)
                self.action("record_weight", gross_g=gross)
                self.assertIsNone(self.inv.scents[scent]["rate_g_per_second"])

    def test_no_learning_across_store_swap_or_restart(self):
        for boundary in ("store", "swap", "restart"):
            with self.subTest(boundary=boundary):
                self.setUp()
                scent = self.scent(rate=False)
                a, b = self.bottle(scent), self.bottle(scent)
                self.action("install_bottle", bottle_id=a)
                self.action("record_weight", gross_g=120)
                self.inv.apply_usage(1, 10, 0)
                if boundary == "restart":
                    self.inv.mark_restart()
                else:
                    self.action("store_bottle") if boundary == "store" else self.action("install_bottle", bottle_id=b)
                    self.action("install_bottle", bottle_id=a)
                self.action("record_weight", gross_g=119)
                self.assertIsNone(self.inv.scents[scent]["rate_g_per_second"])

    def test_correction_retains_old_measurement_and_cannot_learn(self):
        scent = self.scent(rate=False)
        bottle = self.bottle(scent)
        self.action("install_bottle", bottle_id=bottle)
        self.action("record_weight", gross_g=120)
        original = deepcopy(self.fill(bottle)["measurements"][0])
        self.inv.apply_usage(1, 10, 0)
        self.action("correct_weight", gross_g=119, reason="Scale correction")
        self.inv.apply_usage(1, 10, 0)
        self.action("record_weight", gross_g=118)
        self.assertIsNone(self.inv.scents[scent]["rate_g_per_second"])
        self.assertEqual(self.fill(bottle)["measurements"][0], original)
        self.assertEqual(self.fill(bottle)["measurements"][1]["reason"], "Scale correction")
        self.assertEqual(self.fill(bottle)["cycles"], 2)

    def test_tare_change_requires_weight_without_rewriting_history(self):
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        self.action("record_weight", gross_g=120)
        self.action("edit_bottle", bottle_id=bottle, empty_g=30)
        self.assertIsNone(self.inv.summary()["remaining_g"])
        self.action("record_weight", gross_g=120)
        self.assertEqual(self.inv.summary()["remaining_g"], 90)
        self.assertEqual(self.fill(bottle)["measurements"][0]["empty_g"], 20)

    def test_restart_only_invalidates_active_mass_restore_itself_does_not(self):
        a, b = self.bottle(), self.bottle()
        self.action("install_bottle", bottle_id=a)
        self.action("record_weight", gross_g=120)
        saved = json.loads(json.dumps(self.inv.to_dict(), allow_nan=False))
        restored = Inventory()
        restored.restore(saved)
        self.assertEqual(restored.to_dict(), saved)
        restored.mark_restart()
        self.assertTrue(restored.bottles[a]["fills"][-1]["unbounded_unknown"])
        self.assertEqual(restored.summary()["status"], "needs_weighing")
        self.assertTrue(restored.bottles[b]["fills"][-1]["mass_confident"])
        self.assertIsNone(restored.bottles[a]["fills"][-1]["checkpoint"])
        self.assertEqual(restored.scents, self.inv.scents)

    def test_delete_archive_and_references(self):
        scent = self.scent()
        bottle = self.bottle(scent)
        with self.assertRaises(ValueError):
            self.action("delete_scent", scent_id=scent, confirm=True)
        self.action("install_bottle", bottle_id=bottle)
        for action, fields in [("delete_bottle", {"confirm": True}), ("archive_bottle", {"archived": True})]:
            with self.assertRaises(ValueError):
                self.action(action, bottle_id=bottle, **fields)
        self.action("store_bottle")
        self.action("archive_bottle", bottle_id=bottle, archived=True)
        with self.assertRaises(ValueError):
            self.action("install_bottle", bottle_id=bottle)
        self.action("archive_bottle", bottle_id=bottle, archived=False)
        with self.assertRaises(ValueError):
            self.action("delete_bottle", bottle_id=bottle, confirm=1)
        self.action("delete_bottle", bottle_id=bottle, confirm=True)
        self.action("delete_scent", scent_id=scent, confirm=True)
        self.assertEqual(self.inv.bottles, {})
        self.assertEqual(self.inv.scents, {})

    def test_invalid_operations_leave_whole_inventory_unchanged(self):
        scent = self.scent()
        bottle = self.bottle(scent)
        bad = [("edit_scent", {"scent_id": scent, "name": "Changed", "grams_per_spray": 1}),
               ("edit_bottle", {"bottle_id": bottle, "name": "Changed", "empty_g": -1}),
               ("add_bottle", {"name": "B", "empty_g": 20, "scent_id": scent, "oil_g": 1, "gross_g": 21}),
               ("install_bottle", {"bottle_id": []}),
               ("record_weight", {"bottle_id": bottle, "gross_g": 19}),
               ("correct_weight", {"bottle_id": bottle, "gross_g": 100, "reason": " "})]
        for action, fields in bad:
            with self.subTest(action=action, fields=fields):
                before = self.inv.to_dict()
                with self.assertRaises(ValueError):
                    self.action(action, **fields)
                self.assertEqual(self.inv.to_dict(), before)

    def test_numeric_validation_nonfinite_boolean_overflow_and_usage_atomicity(self):
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        for bad in [float("nan"), float("inf"), -1, True, "1", 10 ** 1000]:
            with self.subTest(bad=repr(bad)[:20]):
                before = self.inv.to_dict()
                with self.assertRaises(ValueError):
                    self.action("record_weight", gross_g=bad)
                with self.assertRaises(ValueError):
                    self.inv.apply_usage(1, bad, 0)
                self.assertEqual(self.inv.to_dict(), before)
        with self.assertRaises(ValueError):
            self.inv.apply_usage(1e308, 1e308, 0)
            self.inv.apply_usage(1e308, 1e308, 0)

    def test_read_summary_never_mutates_and_status_thresholds(self):
        self.assertEqual(self.inv.summary()["status"], "no_bottle")
        bottle = self.bottle(oil=1000)
        self.action("install_bottle", bottle_id=bottle)
        before = self.inv.to_dict()
        summary = self.inv.summary(DAILY, PRESETS)
        self.assertEqual(summary["daily_g"], 60)
        self.assertAlmostEqual(summary["days_remaining"], 1000 / 60)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(self.inv.to_dict(), before)
        for gross, expected in [(120, "refill_soon"), (25, "low"), (20, "estimated_depleted")]:
            self.action("record_weight", gross_g=gross)
            self.assertEqual(self.inv.summary(DAILY, PRESETS)["status"], expected)

    def test_uncertain_mass_hides_all_numeric_forecasts(self):
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(1, 10, 1000)
        result = self.inv.summary(DAILY, PRESETS)
        for key in ("remaining_g", "remaining_percent", "daily_g", "days_remaining"):
            self.assertIsNone(result[key])
        self.assertEqual(result["status"], "needs_weighing")

    def test_short_gap_swap_resume_keeps_bounded_estimate(self):
        a, b = self.bottle(), self.bottle()
        self.action("install_bottle", bottle_id=a)
        self.action("record_weight", gross_g=120)
        self.inv.apply_usage(1, 10, 2)
        self.assertIsNone(self.fill(a)["checkpoint"])
        self.action("install_bottle", bottle_id=b)
        self.inv.apply_usage(1, 10, 100)
        self.action("install_bottle", bottle_id=a)
        result = self.inv.summary(DAILY, PRESETS)
        self.assertEqual(result["remaining_g"], 99)
        self.assertEqual(result["remaining_upper_g"], 99)
        self.assertAlmostEqual(result["remaining_lower_g"], 98.8)
        self.assertEqual(result["uncertainty_g"], .2)
        self.assertEqual(result["confidence"], "estimated_with_uncertainty")
        self.assertEqual(result["rate_source"], "scent_default_provisional")
        self.assertIsNotNone(result["days_remaining"])

    def test_uncertainty_crossing_threshold_does_not_claim_depletion(self):
        bottle = self.bottle(oil=10)
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(0, 0, 50)
        result = self.inv.summary(DAILY, PRESETS)
        self.assertEqual(result["status"], "needs_weighing")
        self.assertEqual(result["remaining_lower_g"], 5)
        self.assertEqual(result["remaining_upper_g"], 10)
        self.assertIsNone(result["remaining_g"])
        self.inv.apply_usage(0, 0, 1000)
        self.assertEqual(self.inv.summary()["status"], "needs_weighing")
        self.assertEqual(self.inv.summary()["remaining_lower_g"], 0)

    def test_bounded_uncertainty_preserves_low_and_depleted_upper_estimate(self):
        bottle = self.bottle(oil=5)
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(0, 0, 10)
        result = self.inv.summary(DAILY, PRESETS)
        self.assertEqual(result["status"], "low")
        self.assertEqual(result["remaining_g"], 5)
        self.assertEqual(result["remaining_lower_g"], 4)
        self.inv.apply_usage(5, 50, 0)
        result = self.inv.summary(DAILY, PRESETS)
        self.assertEqual(result["status"], "estimated_depleted")
        self.assertEqual(result["remaining_g"], 0)

    def test_display_names_are_bounded_without_losing_existing_records(self):
        scent = self.scent(name="x" * 100)
        bottle = self.bottle(scent)
        before = self.inv.to_dict()
        for action, fields in [
            ("add_scent", {}),
            ("edit_scent", {"scent_id": scent}),
            ("edit_bottle", {"bottle_id": bottle}),
            ("add_bottle", {"scent_id": scent, "empty_g": 20, "oil_g": 100}),
        ]:
            with self.assertRaises(ValueError):
                self.action(action, name="x" * 101, **fields)
            self.assertEqual(self.inv.to_dict(), before)

    def test_known_restart_bounds_only_active_and_reweight_clears_uncertainty(self):
        a, b = self.bottle(), self.bottle()
        self.action("install_bottle", bottle_id=a)
        self.inv.mark_restart(10)
        self.assertEqual(self.fill(a)["uncertainty_g"], 1)
        self.assertEqual(self.fill(b)["uncertainty_g"], 0)
        self.assertEqual(self.inv.summary()["confidence"], "estimated_with_uncertainty")
        saved = self.inv.to_dict()
        restored = Inventory()
        restored.restore(saved)
        self.assertEqual(restored.to_dict(), saved)
        self.action("record_weight", gross_g=119)
        self.assertEqual(self.fill(a)["uncertainty_g"], 0)
        self.inv.mark_restart()
        self.assertTrue(self.fill(a)["unbounded_unknown"])
        self.action("correct_weight", gross_g=118, reason="Fresh scale reading")
        self.assertFalse(self.fill(a)["unbounded_unknown"])
        self.assertIsNone(self.fill(a)["checkpoint"])

    def test_unknown_without_rate_is_unbounded_and_reweigh_restores_mass(self):
        bottle = self.bottle(self.scent(rate=False))
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(0, 0, 1)
        self.assertTrue(self.fill(bottle)["unbounded_unknown"])
        self.assertEqual(self.inv.summary()["status"], "needs_weighing")
        self.action("record_weight", gross_g=119)
        self.assertEqual(self.inv.summary()["remaining_g"], 99)
        self.assertEqual(self.inv.summary()["status"], "needs_calibration")

    def test_restore_rejects_invalid_uncertainty_fields(self):
        bottle = self.bottle()
        before = self.inv.to_dict()
        for key, bad in [("uncertainty_g", float("nan")), ("uncertainty_g", -1),
                         ("unbounded_unknown", 1)]:
            data = deepcopy(before)
            data["bottles"][bottle]["fills"][-1][key] = bad
            with self.assertRaises(ValueError):
                self.inv.restore(data)
            self.assertEqual(self.inv.to_dict(), before)

    def test_forecast_weekday_mapping_adjacent_and_intensity_five(self):
        forecast = MODULE.weekly_active_seconds
        self.assertEqual(forecast(DAILY, PRESETS), 600)
        self.assertAlmostEqual(forecast([(129, 8, 0, 9, 0, 1)], PRESETS), 600 / 7)
        self.assertEqual(forecast([(255, 8, 0, 8, 30, 1), (255, 8, 30, 9, 0, 1)], PRESETS), 600)
        slot = SimpleNamespace(days_mask=255, start_hour=8, start_minute=0,
                               end_hour=9, end_minute=0, intensity=5)
        self.assertEqual(forecast([slot], [(10, 50)] * 5), 600)
        self.assertEqual(forecast([(127, 8, 0, 9, 0, 1)], PRESETS), 0)

    def test_forecast_rejects_ambiguity_and_zero_has_no_infinity(self):
        forecast = MODULE.weekly_active_seconds
        for schedules, presets in [(DAILY * 2, PRESETS), ([(255, 22, 0, 8, 0, 1)], PRESETS),
                                   (DAILY, []), (DAILY, [(0, 10)]), (None, PRESETS),
                                   ([(255, 8, 60, 9, 0, 1)], PRESETS)]:
            self.assertIsNone(forecast(schedules, presets))
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        summary = self.inv.summary([], PRESETS)
        self.assertEqual(summary["daily_g"], 0)
        self.assertIsNone(summary["days_remaining"])

    def test_forecast_counts_only_mass_above_depleted_threshold(self):
        bottle = self.bottle(oil=100)
        self.action("install_bottle", bottle_id=bottle)
        summary = self.inv.summary(DAILY, PRESETS, low_g=20, depleted_g=10)
        self.assertEqual(summary["days_remaining"], 1.5)
        with self.assertRaises(ValueError):
            self.inv.summary(low_g=5, depleted_g=5)

    def test_restore_rejects_corruption_atomically(self):
        bottle = self.bottle()
        self.action("install_bottle", bottle_id=bottle)
        self.action("record_weight", gross_g=120)
        pristine = self.inv.to_dict()
        variants = []
        for key, value in [("version", 99), ("initialized", "yes"), ("active_bottle_id", "missing")]:
            data = deepcopy(pristine)
            data[key] = value
            variants.append(data)
        for key, value in [("scent_id", "missing"), ("cycles", float("nan")),
                           ("remaining_g", -1), ("mass_confident", 1), ("checkpoint", {})]:
            data = deepcopy(pristine)
            data["bottles"][bottle]["fills"][-1][key] = value
            variants.append(data)
        data = deepcopy(pristine)
        data["bottles"][bottle]["fills"][-1]["measurements"][0]["gross_g"] = 0
        variants.append(data)
        for data in variants:
            with self.assertRaises(ValueError):
                self.inv.restore(data)
            self.assertEqual(self.inv.to_dict(), pristine)

    def test_empty_restore_and_defensive_export(self):
        restored = Inventory()
        restored.restore(self.inv.to_dict())
        self.assertFalse(restored.initialized)
        bottle = self.bottle()
        exported = self.inv.to_dict()
        exported["bottles"][bottle]["name"] = "Mutation"
        self.assertEqual(self.inv.bottles[bottle]["name"], "Bottle")

    def test_backdated_and_naive_actions_rejected(self):
        self.scent()
        before = self.inv.to_dict()
        for now in (T, datetime(2026, 1, 1)):
            with self.assertRaises(ValueError):
                self.inv.execute("add_scent", now, name="No")
            self.assertEqual(self.inv.to_dict(), before)

    def test_schema_one_migrates_additively_and_schema_two_requires_balances(self):
        scent = self.scent()
        bottle = self.bottle(scent)
        self.action("install_bottle", bottle_id=bottle)
        self.inv.apply_usage(2, 10, 0)
        saved = self.inv.to_dict()
        self.assertEqual(saved["version"], 2)
        legacy = deepcopy(saved)
        legacy["version"] = 1
        fill = legacy["bottles"][bottle]["fills"][-1]
        del fill["balance_g"]
        del fill["uncertainty_upper_g"]
        self.inv.restore(legacy)
        self.assertEqual(self.inv.to_dict(), saved)
        malformed = deepcopy(legacy)
        malformed["version"] = 2
        with self.assertRaises(ValueError):
            self.inv.restore(malformed)
        self.assertEqual(self.inv.to_dict(), saved)


if __name__ == "__main__":
    unittest.main()
