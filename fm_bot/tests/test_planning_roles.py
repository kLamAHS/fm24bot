"""Tests for fm_bot.planning.roles (spec 7.1, 10.1; BOT 008)."""
from __future__ import annotations

import unittest

from ..bridge_client.schemas import ATTRIBUTE_NAMES
from ..planning import roles
from ..state.status import ValueStatus
from ..state.views import player_state
from . import fixtures as fx


def state(pid: int = 1004, **kw):
    spec = next(s for s in fx.SQUAD_SPEC if s[0] == pid)
    return player_state(fx.player_payload(*spec, **kw), source="test")


class WeightTableTests(unittest.TestCase):
    def test_every_weighted_attribute_is_a_bridge_attribute(self):
        for role, weights in roles.ROLE_WEIGHTS.items():
            for name in weights:
                self.assertIn(name, ATTRIBUTE_NAMES, f"{role}: {name}")

    def test_fifteen_roles_are_versioned(self):
        self.assertEqual(len(roles.KNOWN_ROLES), 15)
        self.assertEqual(roles.ROLE_WEIGHTS_VERSION, "roles-v1")

    def test_perfect_player_scores_one_and_worst_scores_one_twentieth(self):
        perfect = state(1004)
        perfect.attributes = {name: 20 for name in ATTRIBUTE_NAMES}
        self.assertAlmostEqual(roles.attribute_fit(perfect, "Central Defender").value, 1.0)
        worst = state(1004)
        worst.attributes = {name: 1 for name in ATTRIBUTE_NAMES}
        self.assertAlmostEqual(roles.attribute_fit(worst, "Central Defender").value, 0.05)


class SlotMappingTests(unittest.TestCase):
    def test_slot_codes_map_to_base_positions(self):
        for code, base in (("DCR", "DC"), ("DCL", "DC"), ("MCR", "MC"), ("MCL", "MC"), ("STCR", "ST"), ("STCL", "ST"), ("GK", "GK"), ("AMC", "AMC")):
            self.assertEqual(roles.base_position(code), base)

    def test_unknown_code_gives_unsupported_score(self):
        detail = roles.explain_role_score(state(1004), "Central Defender", "XYZ")
        self.assertIs(detail.score.status, ValueStatus.UNSUPPORTED)
        self.assertIsNone(detail.base_position)


class RoleScoreTests(unittest.TestCase):
    def test_score_is_within_unit_interval_and_explained(self):
        detail = roles.explain_role_score(state(1004), "Central Defender", "DCR")
        self.assertTrue(detail.score.available)
        self.assertTrue(0.0 < detail.score.value <= 1.0)
        self.assertEqual(detail.base_position, "DC")
        self.assertIn(detail.familiarity, ("natural", "accomplished", "competent"))
        self.assertNotIn(roles.FLAG_READINESS_UNAVAILABLE, detail.flags)
        self.assertIsNotNone(detail.readiness_factor)

    def test_unfamiliar_position_is_allowed_but_heavily_penalised(self):
        keeper = state(1001)
        natural = roles.explain_role_score(keeper, "Goalkeeper", "GK")
        striker = roles.explain_role_score(keeper, "Advanced Forward", "STCR")
        self.assertEqual(striker.familiarity, roles.UNFAMILIAR_LABEL)
        self.assertIn(roles.FLAG_UNFAMILIAR, striker.flags)
        self.assertEqual(striker.familiarity_multiplier, roles.UNFAMILIAR_MULTIPLIER)
        self.assertTrue(striker.score.available)
        self.assertLess(striker.score.value, natural.score.value)

    def test_familiarity_table_is_monotone(self):
        player = state(1004)
        previous = None
        for rating in range(15, 21):
            player.position_ratings["DC"] = rating
            score = roles.role_score(player, "Central Defender", "DC").value
            if previous is not None:
                self.assertGreater(score, previous)
            previous = score
        player.position_ratings["DC"] = 14
        self.assertLess(roles.role_score(player, "Central Defender", "DC").value, previous * roles.UNFAMILIAR_MULTIPLIER / roles.FAMILIARITY_TABLE[15] + 1e-9)

    def test_stale_readiness_gives_no_adjustment_and_a_flag(self):
        current = roles.explain_role_score(state(1004), "Central Defender", "DC")
        stale = roles.explain_role_score(state(1004, readiness_current=False), "Central Defender", "DC")
        self.assertIn(roles.FLAG_READINESS_UNAVAILABLE, stale.flags)
        self.assertIsNone(stale.readiness_factor)
        # The unadjusted score equals fit * familiarity exactly; the current one is lower because condition < 100.
        self.assertAlmostEqual(stale.score.value, stale.attribute_fit * stale.familiarity_multiplier)
        self.assertLess(current.score.value, stale.score.value)

    def test_readiness_uses_condition_and_sharpness_only_when_current(self):
        fresh = state(1004, condition=100.0, sharpness=100.0)
        tired = state(1004, condition=60.0, sharpness=100.0)
        self.assertAlmostEqual(roles.readiness_factor(fresh)[0], 1.0)
        self.assertLess(roles.readiness_factor(tired)[0], 1.0)
        self.assertIsNone(roles.readiness_factor(state(1004, readiness_current=False))[0])

    def test_masked_attributes_make_the_score_missing_not_zero(self):
        player = state(1004)
        player.attributes = {}
        detail = roles.explain_role_score(player, "Central Defender", "DC")
        self.assertIs(detail.score.status, ValueStatus.MISSING)
        self.assertIsNone(detail.score.value)
        player.attributes = {name: 12 for name in ATTRIBUTE_NAMES if name != "marking"}
        self.assertIs(roles.role_score(player, "Central Defender", "DC").status, ValueStatus.MISSING)

    def test_unknown_role_is_unsupported_and_undecoded_role_falls_back(self):
        self.assertIs(roles.role_score(state(1004), "Libero", "DC").status, ValueStatus.UNSUPPORTED)
        detail = roles.explain_role_score(state(1004), None, "DCL")
        self.assertTrue(detail.score.available)
        self.assertEqual(detail.role, "Central Defender")
        self.assertIn(roles.FLAG_ROLE_FALLBACK, detail.flags)

    def test_missing_ratings_fall_back_to_positions_list_with_flag(self):
        player = state(1004)
        player.position_ratings = {}
        detail = roles.explain_role_score(player, "Central Defender", "DC")
        self.assertTrue(detail.score.available)
        self.assertIn(roles.FLAG_FAMILIARITY_FROM_LIST, detail.flags)
        self.assertEqual(detail.familiarity, "competent")
        player.positions = []
        self.assertIs(roles.role_score(player, "Central Defender", "DC").status, ValueStatus.MISSING)

    def test_to_json_round_trips_the_observed_score(self):
        data = roles.explain_role_score(state(1015), "Advanced Forward", "STCR").to_json()
        self.assertEqual(data["score"]["status"], "available")
        self.assertEqual(data["weights_version"], "roles-v1")


if __name__ == "__main__":
    unittest.main()
