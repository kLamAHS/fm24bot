"""Tests for fm_bot.experiments.evaluation (spec 13.2, 13.4, 13.5, MOD 01)."""
from __future__ import annotations

import unittest

from ..experiments.evaluation import (
    RESULT_BLOCKED,
    RESULT_IMPROVEMENT,
    RESULT_INCONCLUSIVE,
    RESULT_NO_IMPROVEMENT,
    MatchOutcome,
    PairedUnit,
    action_counts,
    classify_result,
    eligibility_violations,
    evidence_gate,
    forecast_error,
    forecast_error_money,
    goal_difference,
    match_outcomes_from_fixtures,
    minutes_plan_deviation,
    paired_comparison,
    pilot_sd,
    points_per_match,
    required_units,
    reserve_shortfalls,
    validity_split,
    wall_time,
)
from ..experiments.manifests import RunManifest, TrialManifest
from ..state.status import ValueStatus
from ..state.units import Money, Period, UnitError
from . import fixtures as fx


class SportingMetricTests(unittest.TestCase):
    def test_outcomes_from_fixture_payload_skip_unplayed(self):
        outcomes = match_outcomes_from_fixtures(fx.fixtures_payload(), fx.CLUB["id"])
        self.assertEqual([(o.goals_for, o.goals_against) for o in outcomes], [(1, 1), (0, 2), (0, 1)])   # home 1-1, away 2-0 lost, home 0-1
        self.assertEqual(points_per_match(outcomes).value, 1 / 3)
        self.assertEqual(goal_difference(outcomes).value, -3)
        self.assertEqual(match_outcomes_from_fixtures(fx.fixtures_payload(), 999), [])
        self.assertEqual(len(match_outcomes_from_fixtures(fx.fixtures_payload(), fx.CLUB["id"], competition_ids=[33])), 0)

    def test_no_matches_is_unavailable_not_zero(self):
        ppm = points_per_match([])
        self.assertFalse(ppm.available)
        self.assertIs(ppm.status, ValueStatus.MISSING)
        self.assertFalse(goal_difference([]).available)
        self.assertEqual(MatchOutcome(2, 0).points, 3)
        self.assertEqual(MatchOutcome(0, 0).points, 1)


class SelectionMetricTests(unittest.TestCase):
    def test_eligibility_violations_need_verified_records(self):
        ok = {"status": "eligible", "verified": True}
        bad = {"status": "ineligible", "verified": True}
        unknown = {"status": "missing", "verified": False}
        self.assertEqual(eligibility_violations([[ok, ok], [ok, bad]]).value, 1)
        result = eligibility_violations([[ok, unknown]])
        self.assertFalse(result.available)
        self.assertIn("unverified", result.reason)
        self.assertEqual(eligibility_violations([]).value, 0)

    def test_minutes_deviation_requires_actuals_for_planned_players(self):
        self.assertEqual(minutes_plan_deviation({1001: 90, 1002: 45}, {1001: 90, 1002: 15, 1003: 30}).value, 15.0)
        missing = minutes_plan_deviation({1001: 90, 1002: 45}, {1001: 90})
        self.assertFalse(missing.available)
        self.assertIn("1002", missing.reason)
        self.assertFalse(minutes_plan_deviation({}, {}).available)


class FinanceMetricTests(unittest.TestCase):
    def test_reserve_shortfalls_use_exact_money(self):
        reserve = Money.native_gbp(500_000)
        balances = [Money.native_gbp(600_000), Money.native_gbp(499_999), Money.native_gbp(500_000)]
        self.assertEqual(reserve_shortfalls(balances, reserve).value, 1)
        self.assertFalse(reserve_shortfalls([], reserve).available)
        with self.assertRaises(UnitError):
            reserve_shortfalls([Money.native_gbp(1, Period.WEEKLY)], reserve)

    def test_money_forecast_error_is_exact_and_rounded_explicitly(self):
        forecasts = [Money.native_gbp(100, Period.WEEKLY), Money.native_gbp(200, Period.WEEKLY), Money.native_gbp(300, Period.WEEKLY)]
        actuals = [Money.native_gbp(90, Period.WEEKLY), Money.native_gbp(220, Period.WEEKLY), Money.native_gbp(300, Period.WEEKLY)]
        error = forecast_error_money(forecasts, actuals).value
        self.assertEqual(error, Money.native_gbp(10, Period.WEEKLY))
        self.assertFalse(forecast_error_money(forecasts, actuals[:2]).available)
        self.assertAlmostEqual(forecast_error([1.0, 2.0], [1.5, 1.0]).value, 0.75)
        self.assertFalse(forecast_error([], []).available)


class ExecutionMetricTests(unittest.TestCase):
    def test_action_counts_and_duplicates(self):
        results = [
            {"outcome": "confirmed", "idempotency_key": "k1"},
            {"outcome": "confirmed", "idempotency_key": "k1"},   # duplicate effect
            {"outcome": "confirmed", "idempotency_key": "k2"},
            {"outcome": "uncertain", "idempotency_key": "k3"},
            {"outcome": "failed"},
            {"outcome": None},
        ]
        self.assertEqual(action_counts(results), {"confirmed": 3, "uncertain": 1, "failed": 1, "duplicates": 1, "unknown_outcome": 1})

    def test_wall_time_requires_measurements(self):
        def run(elapsed):
            manifest = RunManifest("r", None, "b", None, {}, [], {}, 1, 1, "c", "br", [], "ck", "p", {}, 1, {"name": "t"})
            manifest.elapsed_wall_seconds = elapsed
            return manifest
        self.assertEqual(wall_time([run(2.0), run(4.0)]).value, {"total_seconds": 6.0, "mean_seconds": 3.0, "runs": 2})
        self.assertFalse(wall_time([run(2.0), run(None)]).available)
        self.assertFalse(wall_time([]).available)


class ComparisonTests(unittest.TestCase):
    def units(self, shift: float) -> list[PairedUnit]:
        import random
        rng = random.Random(1)
        out = []
        for career in range(8):
            base = rng.uniform(-0.3, 0.3)
            for _ in range(5):
                out.append(PairedUnit(f"career-{career}", base + shift + rng.uniform(-0.2, 0.2), base + rng.uniform(-0.2, 0.2)))
        return out

    def test_clustered_bootstrap_is_seeded_and_brackets_the_effect(self):
        result = paired_comparison(self.units(0.5), metric="goal_difference", seed=42, resamples=500)
        self.assertTrue(result.available)
        self.assertEqual((result.n_units, result.n_clusters), (40, 8))
        self.assertLess(result.ci_low, result.mean_difference)
        self.assertGreater(result.ci_high, result.mean_difference)
        self.assertGreater(result.ci_low, 0.2)
        again = paired_comparison(self.units(0.5), metric="goal_difference", seed=42, resamples=500)
        self.assertEqual(result.to_json(), again.to_json())
        self.assertNotEqual(result.ci_low, paired_comparison(self.units(0.5), metric="gd", seed=43, resamples=500).ci_low)

    def test_single_cluster_has_no_interval(self):
        units = [PairedUnit("career-1", 1.0, 0.5), PairedUnit("career-1", 1.2, 0.4)]
        result = paired_comparison(units, metric="gd", seed=1)
        self.assertEqual(result.status, "insufficient_clusters")
        self.assertIsNone(result.ci_low)
        self.assertAlmostEqual(result.mean_difference, 0.65)
        self.assertEqual(paired_comparison([], metric="gd", seed=1).status, "no_units")


class PowerTests(unittest.TestCase):
    def test_required_units_matches_the_normal_approximation(self):
        # (1.96 + 0.84)^2 * (1/0.5)^2 = 31.4 -> 32
        self.assertEqual(required_units(0.5, 1.0), 32)
        self.assertEqual(required_units(0.5, 1.0, paired=False), 63)
        self.assertGreater(required_units(0.25, 1.0), required_units(0.5, 1.0))
        with self.assertRaises(ValueError):
            required_units(0, 1.0)
        with self.assertRaises(ValueError):
            required_units(0.5, 1.0, alpha=1.5)
        self.assertFalse(pilot_sd([1.0]).available)
        self.assertAlmostEqual(pilot_sd([1.0, 3.0]).value, 2 ** 0.5)


class GateTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify_result(0.4, 0.9, 0.3, False), RESULT_IMPROVEMENT)
        self.assertEqual(classify_result(-0.2, 0.3, 0.3, False), RESULT_NO_IMPROVEMENT)
        self.assertEqual(classify_result(0.1, 0.6, 0.3, False), RESULT_INCONCLUSIVE)
        self.assertEqual(classify_result(0.4, 0.9, 0.3, True), RESULT_BLOCKED)
        self.assertEqual(classify_result(0.4, 0.9, 0.3, False, underpowered=True), RESULT_INCONCLUSIVE)
        self.assertEqual(classify_result(None, None, 0.3, False), RESULT_INCONCLUSIVE)

    def test_evidence_gate_combines_guardrails_and_power(self):
        units = [PairedUnit(f"c{i}", 1.0 + i * 0.01, 0.2) for i in range(6)]
        comparison = paired_comparison(units, metric="ppm", seed=5, resamples=300)
        report = evidence_gate(comparison, threshold=0.3, guardrails={"reserve_shortfalls": False, "duplicates": False})
        self.assertEqual(report.verdict, RESULT_IMPROVEMENT)
        blocked = evidence_gate(comparison, threshold=0.3, guardrails={"reserve_shortfalls": True})
        self.assertEqual(blocked.verdict, RESULT_BLOCKED)
        self.assertIn("reserve_shortfalls", blocked.reasons[0])
        underpowered = evidence_gate(comparison, threshold=0.3, guardrails={}, required_clusters=30)
        self.assertEqual(underpowered.verdict, RESULT_INCONCLUSIVE)
        self.assertIn("below the 30 required", underpowered.reasons[0])
        self.assertEqual(underpowered.to_json()["required_clusters"], 30)


class ValidityTests(unittest.TestCase):
    def trial(self, trial_id, valid, outcomes, reasons=()):
        run = RunManifest("r", "e", "b", None, {}, [], {}, 1, 1, "c", "br", [], "ck", "p", {}, 1, {"name": "t"})
        return TrialManifest(trial_id, "e", "ck", run, "treatment", [], outcomes, valid, list(reasons))

    def test_technical_invalidity_is_separate_from_poor_performance(self):
        trials = [
            self.trial("good", True, {"result": "win"}),
            self.trial("lost", True, {"result": "defeat"}),                                    # inconvenient, still analysed
            self.trial("wrong_save", False, {}, ["wrong_save_identity: game date differs"]),
            self.trial("undecided", None, {}),
        ]
        split = validity_split(trials, poor_performance=lambda outcomes: outcomes.get("result") == "defeat")
        self.assertEqual(split.analysable, ["good", "lost"])
        self.assertEqual(split.poor_performance, ["lost"])
        self.assertEqual(split.technically_invalid, {"wrong_save": ["wrong_save_identity: game date differs"]})
        self.assertEqual(split.undecided, ["undecided"])
        self.assertIn("lost", split.to_json()["analysable"])


if __name__ == "__main__":
    unittest.main()
