"""Tests for fm_bot.execution.verification (spec 12.2: a click is never proof)."""
from __future__ import annotations

import unittest

from ..execution.adapter import ANY_SCREEN, FakeAdapter, UIStep
from ..execution.verification import DISPLAY_TOLERANCES, PLANS, Evidence, VerdictKind, values_match, verify
from ..state.status import ValueStatus
from .execution_fixtures import OFFER_COMMITMENTS, harness


class ValuesMatchTests(unittest.TestCase):
    def test_exact_by_default_with_documented_percent_tolerance(self):
        self.assertTrue(values_match({"intensity": "Double", "rest_percent": 21}, {"intensity": "Double", "rest_percent": 20}))
        self.assertFalse(values_match({"intensity": "Double", "rest_percent": 22}, {"intensity": "Double", "rest_percent": 20}))
        self.assertFalse(values_match({"weekly_wage": 4001}, {"weekly_wage": 4000}), "money is never rounded")
        self.assertFalse(values_match({"a": 1}, {"a": 1, "b": 2}))
        self.assertFalse(values_match([1001, 1002], [1002, 1001]), "slot order matters")
        self.assertEqual(DISPLAY_TOLERANCES, {"_percent": 1})


class PlanTests(unittest.TestCase):
    def test_tactic_plan_confirms_only_from_readback_and_bridge_agreement(self):
        h = harness()
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        before = Evidence(snap.observation_ids, snap)
        # Nothing selected yet: readback shows the old tactic -> FAILED even though no step "failed".
        verdict = verify(intent, before, Evidence(snap.observation_ids, h.snapshot()), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertIn("counter-02", verdict.reasons[0])
        h.adapter.selected_tactic_id = "counter-02"
        verdict = verify(intent, before, Evidence([], h.snapshot()), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.CONFIRMED)
        self.assertTrue(verdict.details["effect"]["bridge_corroborated"])
        self.assertEqual(verdict.details["before"]["snapshot_id"], snap.snapshot_id)

    def test_tactic_plan_is_uncertain_when_bridge_contradicts_ui(self):
        h = harness()
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        h.adapter.selected_tactic_id = "counter-02"
        after = h.snapshot()
        after.routes["/tactics"]["stored_name"] = "4-4-2 Balanced"      # bridge disagrees with the UI readback
        verdict = verify(intent, Evidence(), Evidence([], after), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.UNCERTAIN)
        self.assertIn("contradict", verdict.reasons[0])

    def test_unavailable_readback_is_uncertain_never_false(self):
        h = harness()
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        h.adapter.selected_tactic_id = "counter-02"
        h.adapter.inject_readback("selected_tactic", ValueStatus.STALE, "tactics screen not refreshed")
        verdict = verify(intent, Evidence(), Evidence([], snap), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.UNCERTAIN)
        self.assertEqual(verdict.readbacks[0]["status"], "stale")

    def test_lineup_plan_checks_ids_roles_and_bridge(self):
        h = harness()
        snap = h.snapshot()
        ids = [1001, 1003, 1004, 1005, 1008, 1013, 1009, 1011, 1012, 1015, 1014]
        roles = {"0": "Goalkeeper"}
        intent = h.factory.create("submit.lineup", "selection.submit", snap, {"routes": ["/tactics"]}, {"player_ids": ids, "roles": roles}, verification="lineup_matches_selection")
        h.adapter.lineup_ids, h.adapter.lineup_roles = list(ids), dict(roles)
        self.assertIs(verify(intent, Evidence(), Evidence([], h.snapshot()), h.adapter).kind, VerdictKind.CONFIRMED)
        h.adapter.lineup_roles = {"0": "Sweeper Keeper"}
        verdict = verify(intent, Evidence(), Evidence([], h.snapshot()), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertIn("roles", verdict.reasons[0])
        h.adapter.lineup_roles = dict(roles)
        after = h.snapshot()
        after.routes["/tactics"]["positions"][0]["player_id"] = 1002
        self.assertIs(verify(intent, Evidence(), Evidence([], after), h.adapter).kind, VerdictKind.UNCERTAIN)

    def test_training_plan_rereads_settings(self):
        h = harness()
        snap = h.snapshot()
        intent = h.training_intent(snap)
        self.assertIs(verify(intent, Evidence(), Evidence([], snap), h.adapter).kind, VerdictKind.FAILED)
        h.adapter.training_settings = {"intensity": "Double", "rest_percent": 21}
        self.assertIs(verify(intent, Evidence(), Evidence([], snap), h.adapter).kind, VerdictKind.CONFIRMED)

    def test_contract_plan_needs_agreement_and_exact_obligations(self):
        h = harness()
        snap = h.snapshot()
        intent = h.contract_intent(snap)
        verdict = verify(intent, Evidence(), Evidence([], snap), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.FAILED, "explicit null agreement is provably absent")
        h.adapter.accepted["offer-1"] = {"agreement_id": "agr-1", "offer_id": "offer-1", "commitments": list(OFFER_COMMITMENTS)}
        self.assertIs(verify(intent, Evidence(), Evidence([], snap), h.adapter).kind, VerdictKind.CONFIRMED)
        wrong = dict(OFFER_COMMITMENTS[0]); wrong["amount"] = {**wrong["amount"], "minor": wrong["amount"]["minor"] + 100}
        h.adapter.accepted["offer-1"]["commitments"] = [wrong]
        verdict = verify(intent, Evidence(), Evidence([], snap), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertIn("obligations", verdict.reasons[0])
        intent.parameters["offer_id"] = "offer-unknown"
        self.assertIs(verify(intent, Evidence(), Evidence([], snap), h.adapter).kind, VerdictKind.UNCERTAIN)

    def test_navigation_plan_and_unknown_plan(self):
        h = harness()
        snap = h.snapshot()
        intent = h.factory.create("navigate", "tactics.view", snap, {}, {"target": "tactics"}, verification="navigation_only", risk_class="navigation")
        self.assertIs(verify(intent, Evidence(), Evidence(), h.adapter).kind, VerdictKind.FAILED)
        h.adapter.perform(UIStep("go", "navigate", ANY_SCREEN, {"target": "tactics"}, "navigation"))
        self.assertIs(verify(intent, Evidence(), Evidence(), h.adapter).kind, VerdictKind.CONFIRMED)
        h.adapter.screen = "mystery"
        self.assertIs(verify(intent, Evidence(), Evidence(), h.adapter).kind, VerdictKind.UNCERTAIN)
        intent.verification = "wishful_thinking"
        verdict = verify(intent, Evidence(), Evidence(), FakeAdapter())
        self.assertIs(verdict.kind, VerdictKind.UNCERTAIN)
        self.assertNotIn("wishful_thinking", PLANS)


if __name__ == "__main__":
    unittest.main()
