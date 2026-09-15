"""Tests for fm_bot.models.voi (spec 11.1)."""
from __future__ import annotations

import unittest

from ..models import voi
from ..state.visibility import InformationMode


class ValueOfInformationTests(unittest.TestCase):
    def test_direct_formula(self):
        result = voi.value_of_information(10.0, 14.0, 1.0, 0.5)
        self.assertAlmostEqual(result.voi, 2.5)
        self.assertTrue(result.worthwhile)
        self.assertEqual(result.status, "available")

    def test_scenario_weighted_expectation(self):
        scenarios = [voi.Scenario("good", 0.5, 10.0, 16.0), voi.Scenario("bad", 0.5, 10.0, 10.0)]
        result = voi.value_of_information(None, None, 1.0, 0.0, scenarios=scenarios)
        self.assertAlmostEqual(result.expected_value_after_evidence, 13.0)
        self.assertAlmostEqual(result.voi, 2.0)

    def test_probabilities_must_sum_to_one(self):
        with self.assertRaises(voi.VoiError):
            voi.scenario_expectation([voi.Scenario("a", 0.7, 1.0, 1.0), voi.Scenario("b", 0.7, 1.0, 1.0)])
        with self.assertRaises(voi.VoiError):
            voi.scenario_expectation([])

    def test_unknown_values_are_unavailable_not_zero(self):
        result = voi.value_of_information(None, 5.0, 0.0, 0.0)
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.worthwhile)

    def test_negative_costs_rejected(self):
        with self.assertRaises(voi.VoiError):
            voi.value_of_information(1.0, 2.0, -1.0, 0.0)


class ReportTests(unittest.TestCase):
    def test_report_weight_decays_with_age(self):
        fresh = voi.report_weight(1.0, "2024-02-17", "2024-02-17")
        half = voi.report_weight(1.0, "2023-11-19", "2024-02-17")   # 90 days earlier
        self.assertEqual(fresh, 1.0)
        self.assertAlmostEqual(half, 0.5)
        self.assertEqual(voi.report_weight(0.6, None, "2024-02-17"), 0.6)

    def test_future_report_is_leakage(self):
        with self.assertRaises(voi.VoiError):
            voi.report_weight(1.0, "2024-03-01", "2024-02-17")

    def test_quality_bounds(self):
        with self.assertRaises(voi.VoiError):
            voi.report_weight(1.5, None, "2024-02-17")

    def test_belief_update_is_precision_weighted(self):
        prior = voi.Belief(10.0, 4.0)
        updated = voi.update_belief(prior, 14.0, 4.0, 1.0)
        self.assertAlmostEqual(updated.mean, 12.0)
        self.assertAlmostEqual(updated.variance, 2.0)
        weak = voi.update_belief(prior, 14.0, 4.0, 0.1)
        self.assertLess(weak.mean, updated.mean)
        self.assertEqual(voi.update_belief(prior, 14.0, 4.0, 0.0), prior)


def candidate(pid, name, *, known, attr_gain=4.0, ctx_gain=1.0, cost=0.5, report=None, quality=None) -> voi.ScoutingCandidate:
    attr = [voi.Scenario("s", 1.0, 0.0, attr_gain)] if attr_gain is not None else []
    ctx = [voi.Scenario("s", 1.0, 0.0, ctx_gain)] if ctx_gain is not None else []
    return voi.ScoutingCandidate(pid, name, known, attr, ctx, report, quality, cost, 0.0)


class RankingTests(unittest.TestCase):
    def test_bridge_observed_flags_known_attributes_and_credits_only_context(self):
        rankings = voi.rank_assignments([candidate(2001, "Known", known=True), candidate(2002, "Unknown", known=False)], InformationMode.BRIDGE_OBSERVED, "2024-02-17")
        by_id = {r.player_id: r for r in rankings}
        self.assertIn(voi.ATTRIBUTE_KNOWN_FLAG, by_id[2001].flags)
        self.assertEqual(by_id[2001].components["attributes"], 0.0)
        self.assertAlmostEqual(by_id[2001].voi, 1.0 - 0.5)
        self.assertNotIn(voi.ATTRIBUTE_KNOWN_FLAG, by_id[2002].flags)
        self.assertAlmostEqual(by_id[2002].voi, 5.0 - 0.5)
        self.assertEqual(rankings[0].player_id, 2002)

    def test_manager_visible_never_uses_bridge_attribute_knowledge(self):
        rankings = voi.rank_assignments([candidate(2001, "Known", known=True)], InformationMode.MANAGER_VISIBLE, "2024-02-17")
        self.assertEqual(rankings[0].flags, [])
        self.assertAlmostEqual(rankings[0].voi, 4.5)

    def test_fresh_high_quality_report_reduces_remaining_value(self):
        fresh = candidate(2001, "Reported", known=False, report="2024-02-17", quality=1.0)
        unreported = candidate(2002, "Unreported", known=False)
        rankings = voi.rank_assignments([fresh, unreported], InformationMode.MANAGER_VISIBLE, "2024-02-17")
        by_id = {r.player_id: r for r in rankings}
        self.assertEqual(by_id[2001].report_status, "stored_report")
        self.assertEqual(by_id[2001].report_weight, 1.0)
        self.assertAlmostEqual(by_id[2001].voi, -0.5)
        self.assertEqual(by_id[2002].report_status, "no_stored_report")
        self.assertIsNone(by_id[2002].report_weight)

    def test_no_scenarios_is_unavailable_and_sorted_last(self):
        rankings = voi.rank_assignments([candidate(2001, "Empty", known=False, attr_gain=None, ctx_gain=None), candidate(2002, "Loss", known=False, attr_gain=0.0, ctx_gain=0.0, cost=3.0)], InformationMode.MANAGER_VISIBLE, "2024-02-17")
        self.assertEqual([r.player_id for r in rankings], [2002, 2001])
        self.assertEqual(rankings[1].status, "unavailable")
        self.assertIsNone(rankings[1].voi)
        self.assertIn("no_decision_scenarios", rankings[1].flags)
        self.assertEqual(rankings[1].to_json()["voi"], None)


if __name__ == "__main__":
    unittest.main()
