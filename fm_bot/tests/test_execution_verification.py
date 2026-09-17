"""Tests for fm_bot.execution.verification (spec 12.2: a click is never proof)."""
from __future__ import annotations

import unittest

from ..execution.adapter import ANY_SCREEN, FakeAdapter, UIStep
from ..execution.verification import CONTINUE_BOUNDARY, CONTINUE_FROM_DATE, CONTINUE_FROM_TIME, DISPLAY_TOLERANCES, PLANS, Evidence, VerdictKind, values_match, verify
from ..state.status import ValueStatus
from .execution_fixtures import OFFER_COMMITMENTS, ROUTES, harness


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


class GameAdvancedPlanTests(unittest.TestCase):
    """CAL 01 / spec 12.2, 12.4: Continue is judged by the calendar moving, read from the game, never by the screen it left behind."""

    BOUNDARY = {"date": "2024-02-20", "time": "19:45", "description": "Bristol Street Motors Trophy: Wycombe v Bolton"}

    def _intent(self, h, snap, *, from_date="2024-02-17", from_time="10:00", boundary=BOUNDARY):
        parameters = {CONTINUE_BOUNDARY: boundary}
        if from_date is not None:
            parameters[CONTINUE_FROM_DATE] = from_date
        if from_time is not None:
            parameters[CONTINUE_FROM_TIME] = from_time
        return h.factory.create("progress.continue", "progression.continue", snap, {"routes": ["/inbox"]}, parameters, verification="game_advanced_past_boundary", required_capabilities=[], decision_id="dec-continue-1")

    @staticmethod
    def _moved(snap, date, time="09:00"):
        snap.game_date, snap.game_time = date, time
        return snap

    def test_cal01_a_moved_clock_is_confirmed_and_records_whether_the_boundary_was_reached(self):
        """CAL 01: the game stopping earlier than the expected boundary is still a Continue that worked; the plan records
        which it was rather than requiring the boundary."""
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap)
        early = verify(intent, Evidence([], snap), Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), "2024-02-18")), h.adapter)
        self.assertIs(early.kind, VerdictKind.CONFIRMED)
        self.assertFalse(early.details["effect"]["reached_expected_boundary"])
        self.assertTrue(any("stopped before the expected boundary" in reason for reason in early.reasons), early.reasons)
        at = verify(intent, Evidence([], snap), Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), "2024-02-20", "19:45")), h.adapter)
        self.assertIs(at.kind, VerdictKind.CONFIRMED)
        self.assertTrue(at.details["effect"]["reached_expected_boundary"])
        self.assertEqual(at.details["effect"]["from"], {"game_date": "2024-02-17", "game_time": "10:00"})

    def test_cal01_an_unmoved_or_reversed_clock_is_failed_never_confirmed(self):
        """CAL 01: the same moment means the calendar did not move; an earlier one means another save may be loaded. Neither
        is an uncertain outcome, so neither is left to reconciliation."""
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap)
        same = verify(intent, Evidence([], snap), Evidence([], h.snapshot(ROUTES + ["/inbox"])), h.adapter)
        self.assertIs(same.kind, VerdictKind.FAILED)
        self.assertIn("did not move on", same.reasons[0])
        back = verify(intent, Evidence([], snap), Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), "2024-02-10", "10:00")), h.adapter)
        self.assertIs(back.kind, VerdictKind.FAILED)
        self.assertIn("went backwards", back.reasons[0])

    def test_obs02_an_unreadable_clock_is_uncertain_and_the_reading_stays_unavailable(self):
        """OBS 02: no fresh reading is not evidence that nothing happened. The verdict is uncertain and the readback keeps
        its missing status instead of standing in for a time."""
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap)
        for after, why in ((Evidence(), "no fresh snapshot"), (Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), None, None)), "read no in-game clock")):
            verdict = verify(intent, Evidence([], snap), after, h.adapter)
            self.assertIs(verdict.kind, VerdictKind.UNCERTAIN, why)
            self.assertEqual(verdict.readbacks[0]["status"], ValueStatus.MISSING.value)
            self.assertIn("unknown", verdict.reasons[0])

    def test_obs02_a_same_day_reading_without_a_time_of_day_cannot_confirm_or_deny(self):
        """OBS 02 / spec 5.2: an unreported time of day is not midnight, so movement inside one day is simply not observable."""
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap, from_time=None)
        verdict = verify(intent, Evidence([], snap), Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), "2024-02-17", None)), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.UNCERTAIN)
        self.assertIn("within the day cannot be established", verdict.reasons[0])

    def test_an_intent_with_no_starting_moment_falls_back_to_the_before_snapshot_then_gives_up(self):
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap, from_date=None, from_time=None)
        fell_back = verify(intent, Evidence([], snap), Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), "2024-02-18")), h.adapter)
        self.assertIs(fell_back.kind, VerdictKind.CONFIRMED, "the before snapshot records the moment the intent omitted")
        nothing = verify(intent, Evidence(), Evidence([], self._moved(h.snapshot(ROUTES + ["/inbox"]), "2024-02-18")), h.adapter)
        self.assertIs(nothing.kind, VerdictKind.UNCERTAIN)
        self.assertIn("records no in-game moment", nothing.reasons[0])


class InboxAnsweredPlanTests(unittest.TestCase):
    """AUD 01 / spec 11.3, 12.2: an inbox answer is judged by the message no longer standing pending, and the option sent is never claimed as read back."""

    def _intent(self, h, snap, *, message_id=501, option_id="reject"):
        return h.factory.create("respond.inbox", "inbox.respond", snap, {"routes": ["/inbox"], "message_id": message_id}, {"message_id": message_id, "option_id": option_id, "legal_option_ids": ["accept", "reject"]}, verification="inbox_message_answered", required_capabilities=[], decision_id="dec-inbox-1")

    @staticmethod
    def _inbox(snap, *, drop=None, unread=None, message_id=501, strip=False):
        payload = snap.routes["/inbox"]
        if strip:
            snap.routes["/inbox"] = {"unread_count": 0}
            return snap
        messages = [m for m in payload["messages"] if not (drop and m["id"] == message_id)]
        for message in messages:
            if message["id"] == message_id and unread is not None:
                message["unread"] = unread
            if message["id"] == message_id and unread is None and drop is None:
                message.pop("unread", None)
        payload["messages"] = messages
        return snap

    def test_aud01_a_message_that_is_gone_or_read_is_confirmed_without_claiming_an_option_readback(self):
        """AUD 01: the observable effect is the metadata one. Which option the game recorded is not observable, so the effect
        carries the option that was sent and says plainly that no readback exists."""
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap)
        gone = verify(intent, Evidence([], snap), Evidence([], self._inbox(h.snapshot(ROUTES + ["/inbox"]), drop=True)), h.adapter)
        self.assertIs(gone.kind, VerdictKind.CONFIRMED)
        self.assertFalse(gone.details["effect"]["pending"])
        self.assertEqual(gone.details["effect"]["option_id_sent"], "reject")
        self.assertIn("unsupported", gone.details["effect"]["option_readback"])
        read = verify(intent, Evidence([], snap), Evidence([], self._inbox(h.snapshot(ROUTES + ["/inbox"]), unread=False)), h.adapter)
        self.assertIs(read.kind, VerdictKind.CONFIRMED)
        self.assertFalse(read.details["effect"]["unread"])

    def test_a_still_unread_message_is_failed(self):
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        verdict = verify(self._intent(h, snap), Evidence([], snap), Evidence([], self._inbox(h.snapshot(ROUTES + ["/inbox"]), unread=True)), h.adapter)
        self.assertIs(verdict.kind, VerdictKind.FAILED)
        self.assertIn("still unread", verdict.reasons[0])

    def test_obs02_missing_metadata_or_a_missing_unread_flag_is_uncertain(self):
        """OBS 02: an absent inbox reading, and a record with no unread flag, both leave the answer unestablished rather than
        counting as landed or as lost."""
        h = harness()
        snap = h.snapshot(ROUTES + ["/inbox"])
        intent = self._intent(h, snap)
        stripped = verify(intent, Evidence([], snap), Evidence([], self._inbox(h.snapshot(ROUTES + ["/inbox"]), strip=True)), h.adapter)
        self.assertIs(stripped.kind, VerdictKind.UNCERTAIN)
        self.assertEqual(stripped.readbacks[0]["status"], ValueStatus.MISSING.value)
        flagless = verify(intent, Evidence([], snap), Evidence([], self._inbox(h.snapshot(ROUTES + ["/inbox"]))), h.adapter)
        self.assertIs(flagless.kind, VerdictKind.UNCERTAIN)
        self.assertIn("no unread flag", flagless.reasons[0])
        nothing = verify(intent, Evidence([], snap), Evidence(), h.adapter)
        self.assertIs(nothing.kind, VerdictKind.UNCERTAIN)

    def test_both_effect_plans_are_registered_under_the_names_the_planners_use(self):
        self.assertIn("game_advanced_past_boundary", PLANS)
        self.assertIn("inbox_message_answered", PLANS)
