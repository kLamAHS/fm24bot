"""Tests for fm_bot.planning.objective (spec 6.2, 15.1)."""
from __future__ import annotations

import json
import unittest

from ..planning import objective as ob
from ..state.status import Observed, ValueStatus
from ..state.units import Money, Period, UnitError


def av(value, what="value"):
    return Observed.available_value(value, "test", what=what)


def inputs(sporting=9.0, development=0.5, continuity=0.8, loss=Money.native_gbp(100_000), changes=2):
    return ob.ObjectiveInputs(av(sporting), av(development), av(continuity), av(loss), av(changes))


class ProfileTests(unittest.TestCase):
    def test_default_profile_round_trips_through_json(self):
        profile = ob.default_profile()
        data = json.loads(json.dumps(profile.to_json()))
        restored = ob.ClubObjectiveProfile.from_json(data)
        self.assertEqual(restored, profile)
        self.assertEqual(restored.version, ob.OBJECTIVE_PROFILE_VERSION)
        self.assertEqual(restored.scales.downside_loss, Money.native_gbp(500_000))

    def test_profile_validation(self):
        base = ob.default_profile().to_json()
        with self.assertRaises(ValueError):
            ob.ClubObjectiveProfile.from_json({**base, "w_risk": -1})
        with self.assertRaises(ValueError):
            ob.ClubObjectiveProfile.from_json({**base, "development_emphasis": 1.5})
        with self.assertRaises(ValueError):
            ob.NormalisationScales(0, 1, 1, Money.native_gbp(1), 1)
        with self.assertRaises(ValueError):
            ob.NormalisationScales(1, 1, 1, Money.native_gbp(1, Period.WEEKLY), 1)

    def test_competition_priorities_are_keyed_by_text_id(self):
        profile = ob.ClubObjectiveProfile.from_json({**ob.default_profile().to_json(), "competition_priorities": {14: 1.0, "33": 0.3}})
        self.assertEqual(profile.priority(14), 1.0)
        self.assertEqual(profile.priority("33"), 0.3)
        self.assertIsNone(profile.priority(99))


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.profile = ob.default_profile()

    def test_components_are_reported_separately_and_combined_with_declared_scales(self):
        result = ob.score_components(self.profile, inputs())
        c = result.components
        self.assertAlmostEqual(c["sporting"].normalised.value, 9.0 / 15.0)
        self.assertAlmostEqual(c["development"].normalised.value, 0.5)
        self.assertAlmostEqual(c["continuity"].normalised.value, 0.8)
        self.assertAlmostEqual(c["risk"].normalised.value, 0.2)          # 100k / 500k, exact fraction
        self.assertAlmostEqual(c["change"].normalised.value, 0.4)
        expected = 0.6 + 0.2 * 0.5 + 0.1 * 0.8 - 0.5 * 0.2 - 0.05 * 0.4
        self.assertAlmostEqual(result.total.value, expected)
        self.assertAlmostEqual(c["risk"].contribution.value, -0.1)
        self.assertEqual(c["risk"].sign, -1)
        self.assertEqual(c["risk"].unit, "money_once")
        self.assertIs(result.sporting_failure, False)
        self.assertEqual(result.sporting_status, "at_or_above_floor")

    def test_sporting_failure_flag_survives_a_high_total(self):
        rich = inputs(sporting=1.0, development=5.0, continuity=5.0, loss=Money.native_gbp(0), changes=0)
        result = ob.score_components(self.profile, rich)
        self.assertGreater(result.total.value, 1.0)
        self.assertIs(result.sporting_failure, True)
        self.assertEqual(result.sporting_status, "below_floor")
        self.assertTrue(any("below the floor" in n for n in result.notes))

    def test_unavailable_risk_makes_the_total_unavailable_not_zero(self):
        missing = ob.ObjectiveInputs(av(9.0), av(0.5), av(0.8), Observed.unavailable(ValueStatus.MISSING, "downside_loss", "no cash projection"), av(1))
        result = ob.score_components(self.profile, missing)
        self.assertFalse(result.total.available)
        self.assertIn("risk missing", result.total.reason)
        self.assertTrue(result.components["sporting"].contribution.available)
        self.assertFalse(result.components["risk"].contribution.available)
        self.assertIs(result.components["risk"].normalised.status, ValueStatus.MISSING)
        self.assertIs(result.sporting_failure, False)

    def test_unavailable_sporting_value_leaves_failure_unknown(self):
        unknown = ob.ObjectiveInputs(Observed.unavailable(ValueStatus.STALE, "sporting_value", "forecast expired"), av(0.5), av(0.8), av(Money.native_gbp(1)), av(0))
        result = ob.score_components(self.profile, unknown)
        self.assertIsNone(result.sporting_failure)
        self.assertTrue(result.sporting_status.startswith("unavailable"))
        self.assertFalse(result.total.available)

    def test_money_is_never_mixed_with_other_units(self):
        with self.assertRaises(UnitError):
            ob.score_components(self.profile, inputs(loss=Money.native_gbp(1000, Period.WEEKLY)))
        with self.assertRaises(UnitError):
            ob.score_components(self.profile, inputs(loss=Money(100000, "EUR")))
        with self.assertRaises(UnitError):
            ob.score_components(self.profile, inputs(loss=100000))
        with self.assertRaises(TypeError):
            ob.score_components(self.profile, inputs(sporting="nine"))

    def test_weights_change_only_their_own_component(self):
        heavier = ob.ClubObjectiveProfile.from_json({**self.profile.to_json(), "w_risk": 1.0})
        base = ob.score_components(self.profile, inputs())
        alt = ob.score_components(heavier, inputs())
        self.assertAlmostEqual(alt.components["sporting"].contribution.value, base.components["sporting"].contribution.value)
        self.assertAlmostEqual(alt.components["risk"].contribution.value, -0.2)
        self.assertAlmostEqual(base.total.value - alt.total.value, 0.1)

    def test_to_json_is_plain_data(self):
        data = json.loads(json.dumps(ob.score_components(self.profile, inputs()).to_json()))
        self.assertEqual(set(data["components"]), set(ob.COMPONENT_NAMES))
        self.assertEqual(data["components"]["risk"]["raw"]["currency"], "GBP")
        self.assertEqual(data["profile_version"], ob.OBJECTIVE_PROFILE_VERSION)
        self.assertIs(data["sporting_failure"], False)


if __name__ == "__main__":
    unittest.main()
