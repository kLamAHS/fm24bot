"""Tests for fm_bot.models.calibration (spec 6.3, 13.4, BOT 012)."""
from __future__ import annotations

import math
import unittest

from ..models import calibration as cal

PROBS = [0.9, 0.8, 0.2, 0.1, 0.7, 0.3]
OUTCOMES = [1, 1, 0, 0, 1, 0]
THRESHOLDS = cal.CalibrationThresholds(max_brier=0.2, max_ece=0.25, min_samples=6)


class ScoringRuleTests(unittest.TestCase):
    def test_brier_perfect_and_worst(self):
        self.assertEqual(cal.brier_score([1.0, 0.0], [1, 0]), 0.0)
        self.assertEqual(cal.brier_score([0.0, 1.0], [1, 0]), 1.0)

    def test_brier_example(self):
        self.assertAlmostEqual(cal.brier_score(PROBS, OUTCOMES), (0.01 + 0.04 + 0.04 + 0.01 + 0.09 + 0.09) / 6)

    def test_log_score_certain_wrong_is_infinite_not_clipped(self):
        self.assertTrue(math.isinf(cal.log_score([1.0, 0.0], [0, 1])))
        self.assertAlmostEqual(cal.log_score([0.5, 0.5], [1, 0]), math.log(2))

    def test_invalid_inputs_raise(self):
        with self.assertRaises(cal.ScoringError):
            cal.brier_score([1.2], [1])
        with self.assertRaises(cal.ScoringError):
            cal.brier_score([0.5], [None])
        with self.assertRaises(cal.ScoringError):
            cal.brier_score([0.5, 0.5], [1])
        with self.assertRaises(cal.ScoringError):
            cal.brier_score([], [])


class ReliabilityTests(unittest.TestCase):
    def test_empty_bins_have_none_not_zero(self):
        table = cal.reliability_table([0.05, 0.95], [0, 1], bins=10)
        self.assertEqual(table[0].count, 1)
        self.assertEqual(table[0].observed_frequency, 0.0)
        self.assertEqual(table[9].observed_frequency, 1.0)
        self.assertEqual(table[5].count, 0)
        self.assertIsNone(table[5].observed_frequency)
        self.assertIsNone(table[5].mean_predicted)

    def test_probability_one_lands_in_last_bin(self):
        table = cal.reliability_table([1.0], [1], bins=4)
        self.assertEqual(table[3].count, 1)

    def test_ece_of_perfectly_calibrated_bins(self):
        probs = [0.25] * 4 + [0.75] * 4
        outcomes = [1, 0, 0, 0, 1, 1, 1, 0]
        self.assertAlmostEqual(cal.expected_calibration_error(probs, outcomes, bins=2), 0.0)

    def test_ece_detects_overconfidence(self):
        self.assertAlmostEqual(cal.expected_calibration_error([0.9, 0.9], [1, 0], bins=10), 0.4)


class CoverageTests(unittest.TestCase):
    def test_unavailable_intervals_are_excluded_not_misses(self):
        result = cal.interval_coverage([(0.0, 2.0), None, (0.0, 1.0)], [1.0, 1.0, 5.0])
        self.assertEqual((result.covered, result.scored, result.excluded), (1, 2, 1))
        self.assertEqual(result.coverage, 0.5)

    def test_missing_outcome_excluded(self):
        result = cal.interval_coverage([(0.0, 1.0)], [None])
        self.assertIsNone(result.coverage)
        self.assertEqual(result.excluded, 1)

    def test_inverted_interval_raises(self):
        with self.assertRaises(cal.ScoringError):
            cal.interval_coverage([(2.0, 1.0)], [1.5])


class ComparisonTests(unittest.TestCase):
    def test_candidate_beats_climatology(self):
        reference = [0.5] * 6
        comparison = cal.compare_to_reference(PROBS, reference, OUTCOMES)
        self.assertTrue(comparison.beats_reference)
        self.assertGreater(comparison.brier_skill, 0.0)
        self.assertAlmostEqual(comparison.reference_brier, 0.25)

    def test_skill_undefined_for_perfect_reference(self):
        comparison = cal.compare_to_reference([0.5], [1.0], [1])
        self.assertIsNone(comparison.brier_skill)


class CalibrationReportTests(unittest.TestCase):
    def test_passes_caller_thresholds(self):
        report = cal.evaluate_calibration("m", "1", PROBS, OUTCOMES, THRESHOLDS)
        self.assertTrue(report.passed)
        self.assertEqual(report.status, "passed")
        self.assertEqual(report.thresholds["max_brier"], 0.2)
        self.assertEqual(report.to_json()["samples"], 6)

    def test_fails_when_brier_exceeds_threshold(self):
        report = cal.evaluate_calibration("m", "1", PROBS, OUTCOMES, cal.CalibrationThresholds(max_brier=0.01, max_ece=None, min_samples=1))
        self.assertFalse(report.passed)
        self.assertEqual(report.status, "failed")
        self.assertTrue(any("brier" in f for f in report.failures))

    def test_too_few_samples_is_inconclusive_not_passed(self):
        report = cal.evaluate_calibration("m", "1", PROBS, OUTCOMES, cal.CalibrationThresholds(max_brier=1.0, max_ece=1.0, min_samples=100))
        self.assertEqual(report.status, "inconclusive")
        self.assertFalse(report.passed)
        self.assertIsNone(report.brier)

    def test_skill_threshold_requires_reference(self):
        thresholds = cal.CalibrationThresholds(max_brier=None, max_ece=None, min_samples=1, min_brier_skill=0.0)
        without = cal.evaluate_calibration("m", "1", PROBS, OUTCOMES, thresholds)
        self.assertFalse(without.passed)
        with_reference = cal.evaluate_calibration("m", "1", PROBS, OUTCOMES, thresholds, reference=[0.5] * 6)
        self.assertTrue(with_reference.passed)
        self.assertGreater(with_reference.brier_skill, 0.0)

    def test_coverage_threshold(self):
        thresholds = cal.CalibrationThresholds(max_brier=None, max_ece=None, min_samples=1, min_interval_coverage=0.9)
        report = cal.evaluate_calibration("m", "1", PROBS, OUTCOMES, thresholds, intervals=[(0, 1), (0, 1)], interval_outcomes=[0.5, 2.0])
        self.assertFalse(report.passed)
        self.assertEqual(report.coverage, 0.5)

    def test_proposed_thresholds_are_only_a_proposal(self):
        self.assertIsInstance(cal.PROPOSED_THRESHOLDS, cal.CalibrationThresholds)
        self.assertGreaterEqual(cal.PROPOSED_THRESHOLDS.min_samples, 100)


if __name__ == "__main__":
    unittest.main()
