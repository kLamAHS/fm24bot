"""Tests for fm_bot.models.baselines (spec 6.3, 17.3)."""
from __future__ import annotations

import unittest

from ..models import baselines as bl
from ..state.status import Observed, ValueStatus


class ForecastContractTests(unittest.TestCase):
    def test_unavailable_forecast_is_a_valid_result_with_reason(self):
        f = bl.unavailable_forecast("rolling_average", "required_current_readiness_observation_missing", missing=["condition"], target="validated_readiness_at_next_fixture")
        self.assertFalse(f.available)
        self.assertIsNone(f.point)
        self.assertIsNone(f.interval)
        self.assertEqual(f.to_json()["status"], "unavailable")
        self.assertEqual(f.to_json()["reason"], "required_current_readiness_observation_missing")

    def test_unavailable_forecast_cannot_carry_a_point(self):
        with self.assertRaises(ValueError):
            bl.Forecast(1.0, None, "x", status="unavailable", reason="r")

    def test_available_forecast_needs_a_point(self):
        with self.assertRaises(ValueError):
            bl.Forecast(None, None, "x")

    def test_forecast_json_carries_scope_and_validity(self):
        f = bl.rolling_average([1.0, 3.0], 2, target="points", scope={"leagues": [14]})
        data = f.to_json()
        self.assertEqual(data["training_scope"], {"leagues": [14]})
        self.assertEqual(data["validity_period"]["window"], 2)
        self.assertEqual(data["uncertainty_method"], f"heuristic: sample_std x {bl.INTERVAL_Z}")
        self.assertEqual(data["model_version"], bl.BASELINE_VERSION)


class RollingAverageTests(unittest.TestCase):
    def test_uses_only_last_window_points(self):
        f = bl.rolling_average([0.0, 0.0, 3.0, 1.0, 3.0], 3)
        self.assertAlmostEqual(f.point, 7.0 / 3.0)
        self.assertEqual(f.validity_period["points_used"], 3)

    def test_missing_points_are_excluded_not_zeroed(self):
        f = bl.rolling_average([3.0, None, 3.0], 3)
        self.assertEqual(f.point, 3.0)
        self.assertEqual(f.known_missing_inputs, ["1 missing values in window"])

    def test_no_known_points_is_unavailable(self):
        f = bl.rolling_average([None, None], 2)
        self.assertFalse(f.available)
        self.assertIn("no known points", f.reason)

    def test_single_point_has_no_interval(self):
        f = bl.rolling_average([2.0], 5)
        self.assertTrue(f.available)
        self.assertIsNone(f.interval)
        self.assertTrue(f.uncertainty_method.startswith("none"))

    def test_interval_is_symmetric_heuristic(self):
        f = bl.rolling_average([1.0, 3.0], 2)
        sd = bl.sample_std([1.0, 3.0])
        self.assertAlmostEqual(f.interval[0], 2.0 - bl.INTERVAL_Z * sd)
        self.assertAlmostEqual(f.interval[1], 2.0 + bl.INTERVAL_Z * sd)

    def test_rejects_zero_window(self):
        with self.assertRaises(ValueError):
            bl.rolling_average([1.0], 0)


class EwmaTests(unittest.TestCase):
    def test_alpha_one_returns_latest(self):
        self.assertEqual(bl.exponentially_weighted_average([1.0, 2.0, 5.0], 1.0).point, 5.0)

    def test_recent_points_weigh_more(self):
        low_alpha = bl.exponentially_weighted_average([0.0, 0.0, 3.0], 0.2).point
        high_alpha = bl.exponentially_weighted_average([0.0, 0.0, 3.0], 0.8).point
        self.assertLess(low_alpha, high_alpha)

    def test_invalid_alpha(self):
        with self.assertRaises(ValueError):
            bl.exponentially_weighted_average([1.0], 0.0)

    def test_all_missing_unavailable(self):
        self.assertFalse(bl.exponentially_weighted_average([None], 0.5).available)


class LinearAlgebraTests(unittest.TestCase):
    def test_solve_linear(self):
        x = bl.solve_linear([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
        self.assertAlmostEqual(x[0], 0.8)
        self.assertAlmostEqual(x[1], 1.4)

    def test_singular_raises(self):
        with self.assertRaises(ValueError):
            bl.solve_linear([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])

    def test_ridge_shrinks_toward_zero(self):
        design = [[1.0, 1.0], [1.0, 2.0], [1.0, 3.0]]
        targets = [2.0, 4.0, 6.0]
        plain = bl.ridge_regression(design, targets, 0.0)
        shrunk = bl.ridge_regression(design, targets, 5.0)
        self.assertAlmostEqual(plain[1], 2.0)
        self.assertLess(abs(shrunk[1]), abs(plain[1]))

    def test_negative_lambda_rejected(self):
        with self.assertRaises(ValueError):
            bl.ridge_regression([[1.0]], [1.0], -1.0)


class OpponentAdjustedRegressionTests(unittest.TestCase):
    def rows(self):
        # Strong team beats everyone, weak loses to everyone; home worth an extra goal.
        rows = []
        teams = ["strong", "mid", "weak"]
        truth = {"strong": 1.0, "mid": 0.0, "weak": -1.0}
        for home in teams:
            for away in teams:
                if home == away:
                    continue
                margin = truth[home] - truth[away] + 0.5
                rows.append({"team": home, "opponent": away, "home": True, "outcome": margin})
                rows.append({"team": away, "opponent": home, "home": False, "outcome": -margin})
        return rows

    def test_recovers_ordering_and_home_effect(self):
        fit = bl.opponent_adjusted_regression(self.rows(), ridge_lambda=0.01)
        self.assertGreater(fit.team_strength["strong"], fit.team_strength["mid"])
        self.assertGreater(fit.team_strength["mid"], fit.team_strength["weak"])
        # Each match appears from both perspectives, so home vs away differ by 2 x 0.5 and the intercept absorbs -0.5.
        self.assertAlmostEqual(fit.home_effect, 1.0, places=1)
        self.assertAlmostEqual(fit.intercept, -0.5, places=1)
        self.assertEqual(fit.rows_used, 12)

    def test_missing_outcomes_are_skipped_not_treated_as_draws(self):
        rows = self.rows() + [{"team": "strong", "opponent": "weak", "home": True, "outcome": None}]
        self.assertEqual(bl.opponent_adjusted_regression(rows).rows_used, 12)
        self.assertIsNone(bl.opponent_adjusted_regression([{"team": "a", "opponent": "b", "home": True, "outcome": None}]))

    def test_predict_unseen_team_widens_and_lists_missing(self):
        fit = bl.opponent_adjusted_regression(self.rows(), ridge_lambda=0.01)
        seen = fit.predict("strong", "weak", True)
        unseen = fit.predict("strong", "newcomer", True)
        self.assertEqual(unseen.known_missing_inputs, ["team newcomer not in training rows"])
        self.assertGreater(unseen.interval[1] - unseen.interval[0], seen.interval[1] - seen.interval[0])
        self.assertIn("widened", unseen.uncertainty_method)


class UnfamiliarContextTests(unittest.TestCase):
    SCOPE = {"leagues": [14], "clubs": [742], "builds": ["24.4.2+2081827"]}

    def test_familiar_context_has_no_widening(self):
        report = bl.unfamiliar_context({"league": 14, "club": 742, "build": "24.4.2+2081827"}, self.SCOPE)
        self.assertEqual(report.widen_factor, 1.0)
        self.assertFalse(report.unfamiliar)

    def test_unfamiliar_league_and_changed_build_multiply(self):
        report = bl.unfamiliar_context({"league": 99, "club": 742, "build": "25.0.0"}, self.SCOPE)
        self.assertAlmostEqual(report.widen_factor, bl.UNFAMILIAR_LEAGUE_WIDEN * bl.CHANGED_BUILD_WIDEN)
        self.assertEqual(len(report.reasons), 2)

    def test_unknown_context_key_is_unfamiliar_not_familiar(self):
        report = bl.unfamiliar_context({"league": 14, "club": 742}, self.SCOPE)
        self.assertEqual(report.widen_factor, bl.CHANGED_BUILD_WIDEN)
        self.assertIn("build unknown", report.reasons)


class ResultForecastTests(unittest.TestCase):
    SCOPE = {"leagues": [14], "clubs": [742], "builds": ["b1"]}

    def test_familiar_fixture_keeps_baseline_interval(self):
        f = bl.result_forecast([3.0, 1.0, 0.0, 3.0], {"date": "2024-02-24", "league": 14, "club": 742, "build": "b1"}, self.SCOPE, window=4)
        self.assertAlmostEqual(f.point, 1.75)
        self.assertEqual(f.validity_period["fixture_date"], "2024-02-24")
        self.assertEqual(f.forecast_target, "points_at_fixture")
        self.assertNotIn("widened", f.uncertainty_method)

    def test_unfamiliar_league_widens_interval(self):
        familiar = bl.result_forecast([3.0, 1.0, 0.0, 3.0], {"date": "d", "league": 14, "club": 742, "build": "b1"}, self.SCOPE)
        unfamiliar = bl.result_forecast([3.0, 1.0, 0.0, 3.0], {"date": "d", "league": 33, "club": 742, "build": "b1"}, self.SCOPE)
        self.assertEqual(familiar.point, unfamiliar.point)
        width = lambda f: f.interval[1] - f.interval[0]  # noqa: E731
        self.assertAlmostEqual(width(unfamiliar), width(familiar) * bl.UNFAMILIAR_LEAGUE_WIDEN)

    def test_no_history_is_unavailable(self):
        f = bl.result_forecast([], {"date": "d", "league": 14, "club": 742, "build": "b1"}, self.SCOPE)
        self.assertFalse(f.available)


class ReadinessForecastTests(unittest.TestCase):
    def test_stale_condition_yields_unavailable_forecast_spec_17_3(self):
        stale = Observed.unavailable(ValueStatus.STALE, "condition", "cache dated 2024-02-10")
        f = bl.readiness_forecast(stale, 3, 4.0)
        self.assertEqual(f.status, "unavailable")
        self.assertEqual(f.reason, "required_current_readiness_observation_missing")
        self.assertEqual(f.known_missing_inputs, ["condition:stale"])
        self.assertEqual(f.forecast_target, "validated_readiness_at_next_fixture")
        self.assertIsNone(f.to_json()["point_estimate"])

    def test_current_condition_projects_capped_recovery(self):
        current = Observed.available_value(90.0, "obs-1", what="condition")
        f = bl.readiness_forecast(current, 4, 4.0)
        self.assertEqual(f.point, 100.0)
        self.assertEqual(bl.readiness_forecast(current, 1, 4.0).point, 94.0)


if __name__ == "__main__":
    unittest.main()
