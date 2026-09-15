"""Tests for fm_bot.models.dynamics (spec 10.1, 10.2)."""
from __future__ import annotations

import unittest

from ..models import dynamics as dy
from . import fixtures as fx

TRUE = dy.FatigueParameters(alpha=0.5, beta=0.3)
OBS = dy.ObservationModel(offset=96.0, scale=4.0)


def exciting_schedule(days: int = 30) -> list[dy.DailyLoad]:
    """Varied training with a match every seven days: the inputs move the state."""
    return [dy.DailyLoad(d, 1.0 if d % 3 else 0.4, 3.0 if d % 7 == 0 else 0.0) for d in range(days)]


def flat_schedule(days: int = 30) -> list[dy.DailyLoad]:
    """Nearly constant training and no matches: little to learn from."""
    return [dy.DailyLoad(d, 1.0 if d % 2 else 0.95) for d in range(days)]


def synthetic_measurements(loads, days, *, decimals=0, model=None, obs=OBS) -> list[dy.ConditionMeasurement]:
    predicted = dy.predict_conditions(model or dy.DiscreteFatigueModel(TRUE), obs, 0.0, loads)
    return [dy.ConditionMeasurement(d, round(predicted[d], decimals)) for d in days]


class LatentModelTests(unittest.TestCase):
    def test_discrete_update_matches_formula_with_match_jump(self):
        model = dy.DiscreteFatigueModel(dy.FatigueParameters(0.5, 0.2))
        self.assertAlmostEqual(model.step(10.0, dy.DailyLoad(0, 2.0, 3.0)), 10.0 + 1.0 - 2.0 + 3.0)
        self.assertEqual(len(model.simulate(0.0, exciting_schedule(5))), 6)

    def test_continuous_relaxes_toward_steady_state(self):
        model = dy.ContinuousFatigueModel(dy.FatigueParameters(0.5, 0.5))
        path = model.simulate(0.0, [dy.DailyLoad(d, 1.0) for d in range(40)])
        self.assertAlmostEqual(path[-1], 1.0, places=3)     # alpha*w/beta
        self.assertLess(path[1], path[-1])

    def test_continuous_approximates_discrete_for_small_rates(self):
        params = dy.FatigueParameters(0.05, 0.05)
        loads = exciting_schedule(10)
        discrete = dy.DiscreteFatigueModel(params).simulate(0.0, loads)
        continuous = dy.ContinuousFatigueModel(params).simulate(0.0, loads)
        for a, b in zip(discrete, continuous):
            self.assertAlmostEqual(a, b, delta=0.05)

    def test_continuous_rejects_zero_substeps(self):
        with self.assertRaises(ValueError):
            dy.ContinuousFatigueModel(TRUE, substeps=0)

    def test_observation_model_is_linear_with_offset_and_invertible(self):
        self.assertEqual(OBS.condition(0.0), 96.0)
        self.assertEqual(OBS.condition(2.0), 88.0)
        self.assertAlmostEqual(OBS.latent(88.0), 2.0)
        with self.assertRaises(ValueError):
            dy.ObservationModel(96.0, 0.0).latent(90.0)

    def test_predict_conditions_keys_by_day(self):
        predicted = dy.predict_conditions(dy.DiscreteFatigueModel(TRUE), OBS, 0.0, exciting_schedule(3))
        self.assertEqual(sorted(predicted), [0, 1, 2, 3])
        self.assertEqual(predicted[0], 96.0)
        self.assertEqual(dy.predict_conditions(dy.DiscreteFatigueModel(TRUE), OBS, 0.0, []), {})


class MeasurementTests(unittest.TestCase):
    def test_only_current_readiness_becomes_a_measurement(self):
        current = fx.player_payload(1001, "A", ["ST"], 2, 1000, "Good", readiness_current=True, condition=91.0)
        stale = fx.player_payload(1001, "A", ["ST"], 2, 1000, "Good", readiness_current=False, condition=91.0)
        unknown = {"id": 1001, "condition": 91.0}
        kept, rejected = dy.collect_measurements([(0, current), (1, stale), (2, unknown)])
        self.assertEqual([m.day for m in kept], [0])
        self.assertEqual(kept[0].condition, 91.0)
        self.assertEqual([r["status"] for r in rejected], ["stale", "missing"])
        self.assertIn("2024-02-10", rejected[0]["reason"])


class FitTests(unittest.TestCase):
    def test_exciting_schedule_with_daily_current_measurements_is_identified(self):
        loads = exciting_schedule()
        measurements = synthetic_measurements(loads, range(0, 31))
        fit = dy.fit_parameters(loads, measurements)
        self.assertEqual(fit.status, "identified", fit.reasons)
        self.assertIsNotNone(fit.parameters)
        self.assertLess(fit.objective, 1.0)   # reproduces whole-point measurements within rounding
        self.assertTrue(all(v <= dy.IDENTIFIABLE_SPREAD_FRACTION for v in fit.near_optimal_spread.values()))
        predicted = dy.predict_conditions(dy.DiscreteFatigueModel(fit.parameters), fit.observation, 0.0, loads)
        self.assertLess(max(abs(predicted[m.day] - m.condition) for m in measurements), 3.0)

    def test_flat_schedule_is_unidentifiable_and_refuses_parameters(self):
        loads = flat_schedule()
        fit = dy.fit_parameters(loads, synthetic_measurements(loads, range(0, 31, 2)))
        self.assertEqual(fit.status, "unidentifiable")
        self.assertIsNone(fit.parameters)
        self.assertIsNone(fit.observation)
        self.assertIn("objective flat within tolerance", fit.reasons)
        self.assertTrue(any("spans" in r for r in fit.reasons))

    def test_identical_measurements_are_unidentifiable(self):
        loads = exciting_schedule()
        fit = dy.fit_parameters(loads, [dy.ConditionMeasurement(d, 95.0) for d in range(0, 31, 2)])
        self.assertEqual(fit.status, "unidentifiable")
        self.assertTrue(any("identical" in r for r in fit.reasons))

    def test_constant_workload_without_matches_is_unidentifiable(self):
        loads = [dy.DailyLoad(d, 1.0) for d in range(30)]
        fit = dy.fit_parameters(loads, synthetic_measurements(loads, range(0, 31, 2), decimals=1))
        self.assertEqual(fit.status, "unidentifiable")
        self.assertTrue(any("excite" in r for r in fit.reasons))

    def test_too_few_measurements_is_insufficient_data(self):
        loads = exciting_schedule()
        fit = dy.fit_parameters(loads, synthetic_measurements(loads, [0, 7, 14]))
        self.assertEqual(fit.status, "insufficient_data")
        self.assertIsNone(fit.parameters)
        self.assertEqual(fit.measurements_used, 3)

    def test_continuous_model_kind_fits(self):
        loads = exciting_schedule()
        continuous = dy.ContinuousFatigueModel(TRUE)
        fit = dy.fit_parameters(loads, synthetic_measurements(loads, range(0, 31), model=continuous), model_kind="continuous")
        self.assertEqual(fit.model_kind, "continuous")
        self.assertIn(fit.status, ("identified", "unidentifiable"))
        self.assertIsNotNone(fit.objective)

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            dy.fit_parameters([], [], model_kind="neural")
        with self.assertRaises(ValueError):
            dy.fit_parameters([], [], measurement_resolution=0.0)


class RecoveryCurveTests(unittest.TestCase):
    def test_groups_current_measurements_by_days_since_match(self):
        loads = [dy.DailyLoad(0, 1.0, 3.0), dy.DailyLoad(1, 0.5), dy.DailyLoad(2, 0.5), dy.DailyLoad(3, 1.0, 3.0), dy.DailyLoad(4, 0.5)]
        measurements = [dy.ConditionMeasurement(1, 80.0), dy.ConditionMeasurement(2, 88.0), dy.ConditionMeasurement(4, 82.0), dy.ConditionMeasurement(0, 95.0)]
        curve = dy.empirical_recovery_curve(loads, measurements)
        self.assertEqual(curve.status, "available")
        self.assertEqual(curve.by_days_since_match[1], (81.0, 2))
        self.assertEqual(curve.by_days_since_match[2], (88.0, 1))
        self.assertEqual(curve.predict(1).require(), 81.0)
        self.assertFalse(curve.predict(5).available)

    def test_no_matches_is_unavailable(self):
        curve = dy.empirical_recovery_curve([dy.DailyLoad(0, 1.0)], [dy.ConditionMeasurement(0, 90.0)])
        self.assertEqual(curve.status, "unavailable")
        self.assertFalse(curve.predict(1).available)


class PoolingTests(unittest.TestCase):
    def identified(self, alpha, beta):
        return dy.FitResult("identified", dy.FatigueParameters(alpha, beta), dy.ObservationModel(95.0, 3.0), 0.1)

    def test_pooled_prior_needs_two_identified_players(self):
        self.assertEqual(dy.pooled_prior([self.identified(0.4, 0.3)]).status, "unavailable")
        prior = dy.pooled_prior([self.identified(0.4, 0.3), self.identified(0.6, 0.5), dy.FitResult("unidentifiable", None, None, None)])
        self.assertEqual(prior.status, "available")
        self.assertEqual(prior.players, 2)
        self.assertAlmostEqual(prior.alpha_mean, 0.5)
        self.assertAlmostEqual(prior.beta_mean, 0.4)

    def test_unidentified_player_gets_pooled_values_with_inflated_spread(self):
        prior = dy.pooled_prior([self.identified(0.4, 0.3), self.identified(0.6, 0.5)])
        estimate = dy.individual_estimate(dy.FitResult("unidentifiable", None, None, None, ["flat"]), prior)
        self.assertEqual(estimate.basis, "pooled")
        self.assertAlmostEqual(estimate.alpha, 0.5)
        self.assertAlmostEqual(estimate.alpha_sd, prior.alpha_sd * dy.UNIDENTIFIED_SD_INFLATION)
        self.assertIn("flat", estimate.reasons)

    def test_identified_player_is_shrunk_toward_pool(self):
        prior = dy.pooled_prior([self.identified(0.4, 0.3), self.identified(0.6, 0.5)])
        estimate = dy.individual_estimate(self.identified(1.0, 0.8), prior, shrinkage=0.5)
        self.assertEqual(estimate.basis, "shrunk")
        self.assertAlmostEqual(estimate.alpha, 0.75)
        self.assertAlmostEqual(estimate.beta, 0.6)

    def test_no_prior_and_no_fit_is_unavailable(self):
        estimate = dy.individual_estimate(dy.FitResult("insufficient_data", None, None, None, ["3 measurements"]), dy.pooled_prior([]))
        self.assertEqual(estimate.basis, "unavailable")
        self.assertIsNone(estimate.alpha)

    def test_invalid_shrinkage(self):
        with self.assertRaises(ValueError):
            dy.individual_estimate(self.identified(0.4, 0.3), dy.pooled_prior([]), shrinkage=2.0)


class SindyTests(unittest.TestCase):
    def setUp(self):
        self.loads = exciting_schedule(40)
        predicted = dy.predict_conditions(dy.DiscreteFatigueModel(TRUE), OBS, 0.0, self.loads)
        self.states = [predicted[d] for d in range(41)]
        self.inputs = [l.workload for l in self.loads] + [0.0]

    def test_fits_daily_unrounded_states(self):
        result = dy.sindy_fit(self.states, self.inputs, 1.0)
        self.assertEqual(result.status, "fitted")
        self.assertIn("F", result.coefficients)
        self.assertLess(result.coefficients["F"], 0.0)   # recovery pulls condition down toward its steady state

    def test_refuses_infrequent_sampling(self):
        result = dy.sindy_fit(self.states, self.inputs, 2.0)
        self.assertEqual(result.status, "refused")
        self.assertTrue(any("coarser" in r for r in result.reasons))

    def test_refuses_rounded_states_with_changes_inside_rounding_noise(self):
        slow = [90.0 + (i % 2) for i in range(40)]
        result = dy.sindy_fit(slow, [1.0] * 40, 1.0)
        self.assertEqual(result.status, "refused")
        self.assertTrue(any("rounded" in r for r in result.reasons))

    def test_refuses_too_few_samples(self):
        result = dy.sindy_fit(self.states[:5], self.inputs[:5], 1.0)
        self.assertEqual(result.status, "refused")
        self.assertTrue(any("below minimum" in r for r in result.reasons))

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            dy.sindy_fit(self.states, self.inputs[:-1], 1.0)


if __name__ == "__main__":
    unittest.main()
