"""Tests for fm_bot.models.fracdiff (spec 8.4, 13.3)."""
from __future__ import annotations

import random
import unittest

from ..models import fracdiff as fd


def mean_reverting(n: int, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    series = [0.0]
    for _ in range(n - 1):
        series.append(0.8 * series[-1] + rng.gauss(0.0, 1.0))
    return series


class WeightTests(unittest.TestCase):
    def test_d_zero_is_identity(self):
        self.assertEqual(fd.fracdiff_weights(0.0, 3), [1.0, 0.0, 0.0, 0.0])

    def test_d_one_is_first_difference(self):
        self.assertEqual(fd.fracdiff_weights(1.0, 3), [1.0, -1.0, 0.0, 0.0])

    def test_fractional_weights_follow_recurrence(self):
        self.assertEqual(fd.fracdiff_weights(0.5, 3), [1.0, -0.5, -0.125, -0.0625])

    def test_negative_window_rejected(self):
        with self.assertRaises(fd.TransformError):
            fd.fracdiff_weights(0.5, -1)


class TransformTests(unittest.TestCase):
    def test_transform_marks_insufficient_history_as_none(self):
        z = fd.transform([1.0, 2.0, 4.0, 7.0], 1.0, 1)
        self.assertEqual(z, [None, 1.0, 2.0, 3.0])

    def test_transform_d_zero_returns_levels_after_window(self):
        self.assertEqual(fd.transform([5.0, 6.0, 7.0], 0.0, 2), [None, None, 7.0])

    def test_seasonal_difference(self):
        self.assertEqual(fd.seasonal_difference([1.0, 2.0, 3.0, 5.0], 2), [None, None, 2.0, 3.0])
        with self.assertRaises(fd.TransformError):
            fd.seasonal_difference([1.0], 0)


class GuardTests(unittest.TestCase):
    def test_log_refuses_nonpositive_balances(self):
        result = fd.log_levels([100.0, 0.0, -500.0])
        self.assertEqual(result.status, "refused")
        self.assertIsNone(result.values)
        self.assertIn("positions [1, 2]", result.reason)

    def test_log_of_positive_balances(self):
        result = fd.log_levels([1.0, 100.0])
        self.assertEqual(result.status, "available")
        self.assertEqual(result.values[0], 0.0)

    def test_repeated_unchanged_balances_are_deduplicated(self):
        observations = [("2024-02-17", 1000.0), ("2024-02-17", 1000.0), ("2024-02-18", 1000.0), ("2024-02-19", 1200.0), ("2024-02-20", 1200.0), ("2024-02-21", 1000.0)]
        result = fd.dedupe_unchanged_balances(observations)
        self.assertEqual(result.points, [("2024-02-17", 1000.0), ("2024-02-19", 1200.0), ("2024-02-21", 1000.0)])
        self.assertEqual(result.dropped, 3)
        self.assertIn("not independent samples", result.reason)

    def test_dedupe_sorts_by_date_first(self):
        result = fd.dedupe_unchanged_balances([("2024-02-19", 5.0), ("2024-02-17", 5.0)])
        self.assertEqual(result.points, [("2024-02-17", 5.0)])


class FoldChoiceTests(unittest.TestCase):
    def test_short_history_refuses_to_choose(self):
        choice = fd.choose_d_within_fold([1.0] * 5, [0.5])
        self.assertEqual(choice.status, "history_too_short")
        self.assertIsNone(choice.d)

    def test_chooses_a_candidate_with_errors_recorded(self):
        # Candidates are passed worst-first for this fold, so returning the first (or a fixed) candidate fails.
        choice = fd.choose_d_within_fold(mean_reverting(40), [0.7, 0.5, 0.3], window=5)
        self.assertEqual(choice.status, "chosen")
        self.assertEqual(choice.d, 0.3)
        self.assertEqual(set(choice.errors), {"d=0.3", "d=0.5", "d=0.7"})
        self.assertEqual(choice.errors[f"d={choice.d:g}"], min(choice.errors.values()))

    def test_folds_with_different_training_halves_select_different_d(self):
        """EXP 02 / spec 8.4, 13.3: d is selected from each training fold's own points, so different folds can differ.

        A steady drift is forecast exactly by a first difference (d=1); a
        level-stationary zigzag is forecast best on the levels (d=0). If the
        order were fixed, or read off anything but the fold it is given, the
        two folds could not disagree.
        """
        candidates = [0.0, 0.5, 1.0]
        drifting = [1000.0 + 25.0 * t for t in range(24)]
        level_stationary = [1000.0 + (40.0 if t % 2 else -40.0) for t in range(24)]
        drift_fold = fd.choose_d_within_fold(drifting, candidates, window=5)
        level_fold = fd.choose_d_within_fold(level_stationary, candidates, window=5)
        self.assertEqual((drift_fold.status, drift_fold.d), ("chosen", 1.0))
        self.assertEqual((level_fold.status, level_fold.d), ("chosen", 0.0))
        self.assertEqual(drift_fold.errors["d=1"], 0.0)
        self.assertLess(level_fold.errors["d=0"], level_fold.errors["d=1"])

    def test_held_out_points_never_change_the_fold_choice(self):
        """EXP 02 / spec 13.3: appending held-out (future) points leaves the training fold's order and its scores untouched.

        Two series share the first 28 points and differ completely afterwards.
        The training fold is the same 28 points in both, so the selected d and
        every candidate score must be identical - no transform parameter may be
        fitted on the held-out part - while the out-of-sample errors differ,
        which shows the holdout really was scored.
        """
        candidates = [0.0, 0.5, 1.0]
        train = [1000.0 + 25.0 * t for t in range(28)]
        calm = train + [1675.0 + 25.0 * t for t in range(12)]
        contaminating = train + [(-1.0) ** t * 1_000_000.0 for t in range(12)]
        fold_only = fd.choose_d_within_fold(train, candidates, window=5)
        calm_result = fd.admission_test(calm, candidates, margin=0.0, window=5, holdout_fraction=0.3)
        wild_result = fd.admission_test(contaminating, candidates, margin=0.0, window=5, holdout_fraction=0.3)
        self.assertEqual(fold_only.d, 1.0)
        for result in (calm_result, wild_result):
            self.assertEqual(result.fold.train_points, len(train))
            self.assertEqual(result.chosen_d, fold_only.d)
            self.assertEqual(result.fold.errors, fold_only.errors)
        self.assertNotEqual(calm_result.test_errors, wild_result.test_errors)


class AdmissionTests(unittest.TestCase):
    def test_short_history_keeps_accounting_baseline(self):
        result = fd.admission_test(mean_reverting(10), [0.5], margin=0.0)
        self.assertEqual(result.decision, "keep_accounting_baseline")
        self.assertIn("history too short", result.reason)
        self.assertIsNone(result.improvement)

    def test_decision_follows_caller_margin(self):
        series = mean_reverting(80)
        lenient = fd.admission_test(series, [0.3, 0.5, 0.7], margin=0.0, window=5, seasonal_period=4)
        strict = fd.admission_test(series, [0.3, 0.5, 0.7], margin=10.0, window=5, seasonal_period=4)
        self.assertEqual(strict.decision, "keep_accounting_baseline")
        self.assertIn("does not clear margin", strict.reason)
        self.assertEqual(set(strict.test_errors), {"d=0", "d=1", "seasonal", f"fractional d={strict.chosen_d:g}"})
        self.assertEqual(lenient.decision, "admit_fractional")
        self.assertAlmostEqual(lenient.improvement, 0.0316, places=4)
        self.assertEqual(lenient.chosen_d, strict.chosen_d)
        self.assertEqual(lenient.improvement, strict.improvement)
        at_margin = fd.admission_test(series, [0.3, 0.5, 0.7], margin=lenient.improvement, window=5, seasonal_period=4)
        self.assertEqual(at_margin.decision, "keep_accounting_baseline")   # the margin must be cleared, not merely matched

    def test_fractional_d_is_chosen_inside_the_training_fold_only(self):
        series = mean_reverting(80)
        result = fd.admission_test(series, [0.3, 0.5, 0.7], margin=0.0, window=5, holdout_fraction=0.3)
        train = series[: int(80 * 0.7)]
        self.assertEqual(result.fold.train_points, len(train))
        self.assertEqual(result.chosen_d, fd.choose_d_within_fold(train, [0.3, 0.5, 0.7], window=5).d)

    def test_random_walk_does_not_beat_first_difference_by_a_wide_margin(self):
        rng = random.Random(3)
        walk = [100000.0]
        for _ in range(80):
            walk.append(walk[-1] + rng.gauss(0.0, 2000.0))
        result = fd.admission_test(walk, [0.3, 0.5, 0.7], margin=0.5, window=5)
        self.assertEqual(result.decision, "keep_accounting_baseline")
        # It does not beat the baselines at all: the improvement is negative, so even a zero margin keeps the ledger.
        self.assertLess(result.improvement, 0.0)
        self.assertEqual(fd.admission_test(walk, [0.3, 0.5, 0.7], margin=0.0, window=5).decision, "keep_accounting_baseline")

    def test_invalid_arguments(self):
        with self.assertRaises(fd.TransformError):
            fd.admission_test(mean_reverting(80), [0.5], margin=-0.1)
        with self.assertRaises(fd.TransformError):
            fd.admission_test(mean_reverting(80), [0.5], margin=0.1, holdout_fraction=1.0)


if __name__ == "__main__":
    unittest.main()
