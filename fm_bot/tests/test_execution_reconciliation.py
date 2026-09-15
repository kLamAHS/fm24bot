"""Tests for fm_bot.execution.reconciliation (spec 12.3, 12.4, ACT 02, REC 01)."""
from __future__ import annotations

import unittest

from ..execution.lifecycle import IntentFactory
from ..execution.reconciliation import COMPENSATIONS, ReconciliationError, compensation_for, propose_compensation, reconcile_on_restart, reconcile_uncertain
from ..state.identity import new_id
from ..state.records import ActionResult, ActionState
from ..state.status import ValueStatus
from ..state.store import StoreError
from .execution_fixtures import harness


def in_flight(h, intent, state=ActionState.EXECUTING, *, with_attempt=True):
    """Simulate a process that died after persisting EXECUTING (and maybe VERIFYING)."""
    snap = h.snapshot()
    h.ready(intent, snap)
    h.store.update_intent_state(intent, ActionState.EXECUTING, "preflight passed")
    if with_attempt:
        h.store.insert_attempt(ActionResult(new_id("res"), intent.action_id, 1, list(snap.observation_ids), [], ActionState.EXECUTING, None, None, None, None, adapter="fake"))
    if state is ActionState.VERIFYING:
        h.store.update_intent_state(intent, ActionState.VERIFYING, "steps done")
    return intent


class RestartTests(unittest.TestCase):
    def test_rec01_in_flight_intent_is_reconciled_never_requeued(self):
        h = harness()
        intent = in_flight(h, h.tactic_intent(h.snapshot()))
        h.adapter.selected_tactic_id = "counter-02"       # the input had landed before the crash
        decisions = reconcile_on_restart(h.store, h.adapter, h.snapshot(), branch_id=h.branch_id)
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertIs(decision.previous_state, ActionState.EXECUTING)
        self.assertIs(decision.new_state, ActionState.CONFIRMED)
        self.assertFalse(decision.requeued)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.CONFIRMED)
        path = [e["body"]["to"] for e in h.store.journal_entries("intent.transition", intent.action_id)]
        self.assertEqual(path, ["VALIDATED", "QUEUED", "EXECUTING", "UNCERTAIN", "RECONCILING", "CONFIRMED"])
        self.assertNotIn("QUEUED", path[3:])
        attempt = h.store.list_attempts(intent.action_id)[0]
        self.assertEqual(attempt["outcome"], "confirmed")
        self.assertTrue(attempt["after_evidence"])
        self.assertEqual(h.adapter.inputs, [], "reconciliation sends no input")
        self.assertEqual(reconcile_on_restart(h.store, h.adapter, h.snapshot(), branch_id=h.branch_id), [])

    def test_absent_effect_with_expired_context_fails(self):
        h = harness()
        intent = in_flight(h, h.tactic_intent(h.snapshot()), ActionState.VERIFYING)
        h.adapter.tactic_catalog["balanced-01"] = "4-4-2 Balanced (edited)"   # the tactic the intent was built on changed
        decision = reconcile_on_restart(h.store, h.adapter, h.snapshot(), branch_id=h.branch_id)[0]
        self.assertIs(decision.previous_state, ActionState.VERIFYING)
        self.assertIs(decision.new_state, ActionState.FAILED)
        self.assertIn("provably absent", decision.reason)
        self.assertIn("expired", decision.reason)

    def test_unreadable_readback_keeps_uncertain_with_evidence(self):
        h = harness()
        intent = in_flight(h, h.tactic_intent(h.snapshot()))
        h.adapter.inject_readback("selected_tactic", ValueStatus.UNSUPPORTED, "tactics screen cannot be read")
        decision = reconcile_on_restart(h.store, h.adapter, h.snapshot(), branch_id=h.branch_id)[0]
        self.assertIs(decision.new_state, ActionState.UNCERTAIN)
        self.assertIn("evidence preserved", decision.reason)
        self.assertTrue(decision.evidence)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.UNCERTAIN)
        # A later restart picks it up again and can now confirm once the readback works.
        h.adapter.readback_faults.clear()
        h.adapter.selected_tactic_id = "counter-02"
        again = reconcile_on_restart(h.store, h.adapter, h.snapshot(), branch_id=h.branch_id)[0]
        self.assertIs(again.previous_state, ActionState.UNCERTAIN)
        self.assertIs(again.new_state, ActionState.CONFIRMED)

    def test_only_in_flight_states_are_reconciled(self):
        h = harness()
        snap = h.snapshot()
        queued = h.ready(h.tactic_intent(snap, decision_id="q"), snap)
        self.assertEqual(reconcile_on_restart(h.store, h.adapter, h.snapshot(), branch_id=h.branch_id), [])
        self.assertIs(h.store.get_intent(queued.action_id).state, ActionState.QUEUED)
        with self.assertRaises(ReconciliationError):
            reconcile_uncertain(h.store, h.adapter, queued, h.snapshot())


class Act02Tests(unittest.TestCase):
    def test_timeout_after_acceptance_reconciles_without_second_dispatch(self):
        h = harness()
        h.adapter.inject("timeout_after_success", on_step=3)     # the accept click landed, the confirmation did not
        executor = h.executor()
        snap = h.snapshot()
        intent = h.ready(h.contract_intent(snap), snap)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN)
        inputs_before = len(h.adapter.inputs)
        decision = reconcile_uncertain(h.store, h.adapter, h.store.get_intent(intent.action_id), h.snapshot())
        self.assertIs(decision.new_state, ActionState.CONFIRMED)
        self.assertEqual(decision.verdict.details["effect"]["agreement_id"], "agr-1")
        self.assertEqual(len(h.adapter.inputs), inputs_before, "no second acceptance was sent")
        self.assertEqual(list(h.adapter.accepted), ["offer-1"])
        self.assertIsNone(decision.duplicate_of)
        # The same decision cannot be dispatched again: the idempotency key is taken.
        with self.assertRaises(StoreError):
            h.contract_intent(h.snapshot())
        self.assertFalse(decision.compensation["permitted"])
        self.assertIsNone(executor.run_next(h.snapshot))

    def test_duplicate_detected_by_idempotency_key(self):
        h = harness()
        intent = in_flight(h, h.contract_intent(h.snapshot()))
        h.adapter.accepted["offer-1"] = {"agreement_id": "agr-1", "offer_id": "offer-1", "commitments": list(h.adapter.offers["offer-1"]["commitments"])}
        twin = IntentFactory(None).create("commit.contract", "contracts.accept", h.snapshot(), intent.targets, intent.parameters, verification=intent.verification, decision_id="dec-contract-1")
        self.assertEqual(twin.idempotency_key, intent.idempotency_key)
        with self.assertRaises(StoreError):
            h.store.insert_intent(twin)
        decision = reconcile_uncertain(h.store, h.adapter, intent, h.snapshot())
        self.assertIs(decision.new_state, ActionState.CONFIRMED)


class RequeueTests(unittest.TestCase):
    def test_absent_and_fresh_is_failed_unless_explicitly_requeued(self):
        h = harness()
        intent = in_flight(h, h.tactic_intent(h.snapshot(), decision_id="a"))
        decision = reconcile_uncertain(h.store, h.adapter, intent, h.snapshot())
        self.assertIs(decision.new_state, ActionState.FAILED)
        self.assertIn("not re-queued", decision.reason)
        other = in_flight(h, h.tactic_intent(h.snapshot(), decision_id="b"))
        decision = reconcile_uncertain(h.store, h.adapter, other, h.snapshot(), requeue_if_absent=True, requested_by="operator liam")
        self.assertIs(decision.new_state, ActionState.QUEUED)
        self.assertTrue(decision.requeued)
        self.assertIn("operator liam", decision.reason)
        self.assertEqual(h.store.journal_entries("reconcile.decision", other.action_id)[-1]["body"]["requeued"], True)

    def test_requeue_is_refused_when_context_expired(self):
        h = harness()
        intent = in_flight(h, h.tactic_intent(h.snapshot()))
        h.adapter.tactic_catalog["balanced-01"] = "changed"
        decision = reconcile_uncertain(h.store, h.adapter, intent, h.snapshot(), requeue_if_absent=True)
        self.assertIs(decision.new_state, ActionState.FAILED)


class CompensationTests(unittest.TestCase):
    def test_table_only_permits_understood_reversals(self):
        self.assertTrue(COMPENSATIONS["select_validated_tactic"].permitted)
        for kind in ("commit.contract", "progress.continue", "respond.inbox", "match.substitute"):
            self.assertFalse(COMPENSATIONS[kind].permitted)
            self.assertIsNone(COMPENSATIONS[kind].reversal_kind)

    def test_compensation_for_reports_availability(self):
        h = harness()
        snap = h.snapshot()
        with_prev = h.tactic_intent(snap, decision_id="p", previous="balanced-01")
        self.assertTrue(compensation_for(with_prev)["available"])
        without = h.tactic_intent(snap, decision_id="np", previous=None)
        info = compensation_for(without)
        self.assertFalse(info["available"])
        self.assertEqual(info["missing_parameters"], ["previous_tactic_catalog_id"])
        unknown = h.factory.create("navigate", "tactics.view", snap, {}, {"target": "x"}, verification="navigation_only", risk_class="navigation")
        self.assertFalse(compensation_for(unknown)["known"])

    def test_propose_compensation_creates_reversal_intent_only_when_permitted(self):
        h = harness()
        snap = h.snapshot()
        original = h.tactic_intent(snap)
        reversal = propose_compensation(h.store, h.factory, original, h.snapshot())
        self.assertIsNotNone(reversal)
        self.assertIs(reversal.state, ActionState.PROPOSED)
        self.assertEqual(reversal.kind, "select_validated_tactic")
        self.assertEqual(reversal.parameters, {"tactic_catalog_id": "balanced-01"})
        self.assertEqual(reversal.decision_id, f"compensate:{original.action_id}")
        self.assertTrue(h.store.journal_entries("reconcile.compensation_proposed", original.action_id))
        contract = h.contract_intent(snap)
        self.assertIsNone(propose_compensation(h.store, h.factory, contract, h.snapshot()))
        self.assertTrue(h.store.journal_entries("reconcile.compensation_refused", contract.action_id))

    def test_act01_a_reversal_carrying_too_few_parameters_sends_no_input(self):
        """ACT 01: the compensation table only recovers ``previous_tactic_catalog_id``, so the reversal is refused rather than dispatched with a fabricated catalog version (spec 12.2-12.3)."""
        h = harness()
        snap = h.snapshot()
        reversal = propose_compensation(h.store, h.factory, h.tactic_intent(snap), h.snapshot())
        self.assertEqual(reversal.parameters, {"tactic_catalog_id": "balanced-01"}, "no catalog_version is recovered by the table")
        executor = h.executor()
        executor.enqueue(h.ready(reversal, h.snapshot()))
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CANCELLED)
        self.assertEqual([p.check for p in report.problems], ["parameters"])
        self.assertIn("catalog_version", report.reason)
        self.assertEqual(h.adapter.inputs, [])
        self.assertEqual(h.adapter.selected_tactic_id, "balanced-01")
        executor.close()


if __name__ == "__main__":
    unittest.main()
