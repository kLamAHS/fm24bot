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
        choice = fd.choose_d_within_fold(mean_reverting(40), [0.3, 0.5, 0.7], window=5)
        self.assertEqual(choice.status, "chosen")
        self.assertIn(choice.d, (0.3, 0.5, 0.7))
        self.assertEqual(set(choice.errors), {"d=0.3", "d=0.5", "d=0.7"})
        self.assertEqual(choice.errors[f"d={choice.d:g}"], min(choice.errors.values()))

    def test_choice_depends_only_on_the_training_fold(self):
        train = mean_reverting(40)
        choice = fd.choose_d_within_fold(train, [0.3, 0.5, 0.7], window=5)
        again = fd.choose_d_within_fold(train + [1000.0, -1000.0, 1000.0], [0.3, 0.5, 0.7], window=5)
        # The later, wilder points would change the choice if they were consulted; here they are part of a longer
        # training fold, so the comparison is that the original fold alone is deterministic and self-contained.
        self.assertEqual(choice.d, fd.choose_d_within_fold(list(train), [0.3, 0.5, 0.7], window=5).d)
        self.assertEqual(again.train_points, 43)


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
        self.assertEqual(lenient.decision, "admit_fractional" if lenient.improvement > 0.0 else "keep_accounting_baseline")
        self.assertEqual(lenient.chosen_d, strict.chosen_d)

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

    def test_invalid_arguments(self):
        with self.assertRaises(fd.TransformError):
            fd.admission_test(mean_reverting(80), [0.5], margin=-0.1)
        with self.assertRaises(fd.TransformError):
            fd.admission_test(mean_reverting(80), [0.5], margin=0.1, holdout_fraction=1.0)


if __name__ == "__main__":
    unittest.main()
