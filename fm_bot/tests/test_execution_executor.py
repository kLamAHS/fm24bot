"""Tests for fm_bot.execution.executor (spec 4.2, 12.1-12.3, ACT 01, ACT 03, AUD 01)."""
from __future__ import annotations

import json
import unittest

from ..execution.adapter import FAKE_SCREEN_MODEL, FAKE_WORKFLOWS, FakeAdapter, UIStep, Workflow
from ..execution.executor import NAVIGATION_RETRY_LIMIT, UI_WRITER_LOCK, ExecutorError, WriterLockHeld, unsettled_twin
from ..execution.verification import VerdictKind
from ..rules.authority import AuthorityMode
from ..state.records import ActionState, ExecutionOutcome
from ..state.status import ValueStatus
from .execution_fixtures import harness


def queued_tactic(h, **kw):
    snap = h.snapshot()
    return h.ready(h.tactic_intent(snap, **kw), snap)


class LockTests(unittest.TestCase):
    def test_one_active_ui_writer_per_game(self):
        h = harness()
        first = h.executor("exec-a")
        self.assertEqual(h.store.lock_owner(UI_WRITER_LOCK), "exec-a")
        with self.assertRaises(WriterLockHeld):
            h.executor("exec-b")
        self.assertTrue(first.heartbeat())
        first.close()
        second = h.executor("exec-b")
        self.assertEqual(h.store.lock_owner(UI_WRITER_LOCK), "exec-b")
        second.close()
        self.assertFalse(first.heartbeat())


class HappyPathTests(unittest.TestCase):
    def test_tactic_selection_confirmed_with_evidence_and_journal_order(self):
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertIs(report.verdict.kind, VerdictKind.CONFIRMED)
        self.assertEqual(report.inputs_sent, 2)
        self.assertEqual(h.adapter.selected_tactic_id, "counter-02")
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.CONFIRMED)
        # EXECUTING was persisted before the first input reached the adapter.
        entries = h.store.journal_entries(ref_id=intent.action_id)
        executing = next(e["seq"] for e in entries if e["kind"] == "intent.transition" and e["body"]["to"] == "EXECUTING")
        first_input = next(e["seq"] for e in entries if e["kind"] == "ui.input")
        self.assertLess(executing, first_input)
        # AUD 01: the attempt resolves to before/after evidence that exists in the store.
        attempts = h.store.list_attempts(intent.action_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], ExecutionOutcome.CONFIRMED.value)
        for oid in [*attempts[0]["before_evidence"], *attempts[0]["after_evidence"]]:
            self.assertIsNotNone(h.store.get_observation(oid, with_payload=False), oid)
        self.assertTrue(any(oid for oid in attempts[0]["before_evidence"] if h.store.get_observation(oid, with_payload=False).source.startswith("ui:screen.")))
        self.assertEqual(attempts[0]["confirmed_effect"]["tactic_id"], "counter-02")
        self.assertIsNone(executor.run_next(h.snapshot))

    def test_navigation_step_retries_after_fresh_screen_check(self):
        h = harness()
        h.adapter.inject("timeout_before_effect", on_step=1)
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertEqual(h.adapter.calls, 3, "one navigation retry, then the consequential step once")
        self.assertEqual(report.inputs_sent, 3)

    def test_navigation_retries_are_bounded(self):
        h = harness()
        for n in range(1, NAVIGATION_RETRY_LIMIT + 2):
            h.adapter.inject("timeout_before_effect", on_step=n)
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.FAILED)
        self.assertIn("retries", report.reason)
        self.assertEqual(h.adapter.selected_tactic_id, "balanced-01")


class PreflightTests(unittest.TestCase):
    def test_act01_changed_tactic_expires_queued_action_without_input(self):
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        h.adapter.selected_tactic_id = "counter-02"       # the tactic changed under us; same game date
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.EXPIRED)
        self.assertEqual([p.check for p in report.problems], ["freshness"])
        self.assertEqual(h.adapter.inputs, [])
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.EXPIRED)
        self.assertEqual(h.store.journal_entries("ui.input", intent.action_id), [])

    def test_missing_capability_blocks_only_that_action(self):
        h = harness(provide=())
        executor = h.executor()
        snap = h.snapshot()
        executor.enqueue(h.ready(h.tactic_intent(snap), snap))   # tactics need nothing the operator must provide
        contract = h.contract_intent(snap)                 # queued without validate() so the executor's own gate is exercised
        h.store.update_intent_state(contract, ActionState.VALIDATED, "test bypass")
        h.store.update_intent_state(contract, ActionState.QUEUED, "test bypass")
        executor.enqueue(contract)
        first = executor.run_next(h.snapshot)
        self.assertIs(first.state, ActionState.CONFIRMED, first.reason)
        second = executor.run_next(h.snapshot)
        self.assertIs(second.state, ActionState.EXPIRED)
        problem = next(p for p in second.problems if p.check == "capabilities")
        self.assertIn("contract_cash_flows", problem.report.missing)
        self.assertEqual(h.adapter.accepted, {})

    def test_authority_rechecked_before_input(self):
        h = harness()
        executor = h.executor()
        queued_tactic(h)
        h.profile.mode = AuthorityMode.ADVISE               # operator downgraded the profile after queueing
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.EXPIRED)
        self.assertEqual([p.check for p in report.problems], ["authority"])
        self.assertEqual(h.adapter.inputs, [])

    def test_environment_and_screen_checks(self):
        h = harness(FakeAdapter(scaling=1.25))
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.EXPIRED)
        self.assertEqual([p.check for p in report.problems], ["environment"])
        self.assertIn("scaling 1.25", report.reason)
        executor.close()
        # A workflow that starts on the tactics screen while the game shows home.
        h2 = harness()
        strict = Workflow("strict", "1", "select_validated_tactic", FAKE_SCREEN_MODEL, (UIStep("select", "select_tactic", "tactics", {"tactic_catalog_id": None}, "consequential", "tactics"),), "selected_tactic", FAKE_WORKFLOWS["select_validated_tactic"].required_capabilities)
        executor2 = h2.executor(workflows={**FAKE_WORKFLOWS, "select_validated_tactic": strict})
        queued_tactic(h2)
        report = executor2.run_next(h2.snapshot)
        self.assertIs(report.state, ActionState.EXPIRED)
        self.assertEqual([p.check for p in report.problems], ["screen"])
        self.assertEqual(h2.adapter.inputs, [])

    def test_no_workflow_cancels_without_input(self):
        h = harness()
        executor = h.executor(workflows={})
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CANCELLED)
        self.assertEqual(report.problems[0].check, "workflow")
        self.assertEqual(h.adapter.inputs, [])

    def test_branch_mismatch_expires(self):
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        intent.branch_id = "branch-elsewhere"
        h.store.connection.execute("UPDATE action_intents SET body = ? WHERE action_id = ?", (json.dumps(intent.to_json()), intent.action_id))
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.EXPIRED)
        self.assertEqual(report.problems[0].check, "career_branch")


class HumanControlTests(unittest.TestCase):
    def test_act03_stop_cancels_queue_and_blocks_input(self):
        h = harness()
        executor = h.executor()
        a = queued_tactic(h, decision_id="d1")
        b = queued_tactic(h, decision_id="d2")
        cancelled = executor.stop("operator pressed stop")
        self.assertEqual(cancelled, [a.action_id, b.action_id])
        self.assertIs(h.store.get_intent(a.action_id).state, ActionState.CANCELLED)
        self.assertTrue(h.adapter.stop_flag.is_set())
        self.assertIsNone(executor.run_next(h.snapshot))
        with self.assertRaises(ExecutorError):
            executor.enqueue(h.tactic_intent(h.snapshot(), decision_id="d3"))
        self.assertEqual(h.adapter.inputs, [])

    def test_stop_flag_raised_elsewhere_cancels_before_input(self):
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        h.adapter.stop("stop from the operator interface")
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CANCELLED)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.CANCELLED)
        self.assertEqual(h.adapter.inputs, [])

    def test_focus_loss_before_execution_pauses_and_keeps_intent_queued(self):
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        h.adapter.focused = False
        report = executor.run_next(h.snapshot)
        self.assertTrue(report.paused)
        self.assertIs(report.state, ActionState.QUEUED)
        self.assertEqual(executor.queue_ids(), [intent.action_id])
        self.assertEqual(h.adapter.inputs, [])
        h.adapter.focused = True
        self.assertIs(executor.run_next(h.snapshot).state, ActionState.CONFIRMED)

    def test_focus_loss_mid_workflow_stops_before_the_consequential_step(self):
        h = harness()
        h.adapter.inject("focus_loss", on_step=2)
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.FAILED)
        self.assertIn("focus", report.reason)
        self.assertEqual(len(h.adapter.inputs), 1, "only the navigation input went out")
        self.assertEqual(h.adapter.selected_tactic_id, "balanced-01")


class UncertaintyTests(unittest.TestCase):
    def test_timeout_after_consequential_input_is_uncertain_and_never_retried(self):
        h = harness()
        h.adapter.inject("timeout_after_success", on_step=2)
        executor = h.executor()
        intent = queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN)
        self.assertEqual(h.adapter.calls, 2, "the consequential step was sent exactly once")
        attempt = h.store.list_attempts(intent.action_id)[0]
        self.assertEqual(attempt["outcome"], "uncertain")
        self.assertIn("reconcil", attempt["recovery_instruction"])
        self.assertTrue(attempt["before_evidence"] and attempt["after_evidence"])
        self.assertEqual(h.adapter.selected_tactic_id, "counter-02", "the effect actually landed; only reconciliation may say so")

    def test_adapter_crash_is_uncertain_with_evidence(self):
        h = harness()
        h.adapter.inject("crash", on_step=2)
        executor = h.executor()
        intent = queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN)
        self.assertIn("crashed", report.reason)
        self.assertEqual(h.store.list_attempts(intent.action_id)[0]["execution_state"], "UNCERTAIN")

    def test_unexpected_screen_after_selection_is_uncertain(self):
        h = harness()
        h.adapter.inject("unexpected_screen", on_step=2)
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN)
        self.assertEqual(h.adapter.calls, 2)

    def test_act02_any_adapter_exception_after_input_is_uncertain_with_after_evidence_and_never_retried(self):
        """ACT 02 / spec 12.3: an adapter error that is not an ``AdapterCrash`` (here the UI layer raising ``ValueError`` once the
        consequential input has gone out) never propagates out of ``run_next``: the intent becomes UNCERTAIN with the attempt's
        after-evidence recorded, it is never left EXECUTING, and the step is never retried."""
        class RaisingUI(FakeAdapter):
            def _apply(self, step):
                if step.action == "select_tactic":
                    raise ValueError("dictionary update sequence element #0 has length 10; 2 is required")
                return super()._apply(step)

        h = harness(RaisingUI())
        executor = h.executor()
        intent = queued_tactic(h)
        report = executor.run_next(h.snapshot)                       # must not raise
        self.assertIs(report.state, ActionState.UNCERTAIN, report.reason)
        self.assertIn("ValueError", report.reason)
        self.assertIn("never retried", report.reason)
        self.assertEqual(h.adapter.calls, 2, "navigation once, the consequential step exactly once")
        self.assertEqual(len(h.adapter.inputs), 2, "the input had gone out before the adapter raised")
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.UNCERTAIN)
        self.assertEqual(h.store.list_intents([ActionState.EXECUTING]), [], "never stuck in EXECUTING")
        attempt = h.store.list_attempts(intent.action_id)[0]
        self.assertEqual(attempt["execution_state"], "UNCERTAIN")
        self.assertEqual(attempt["outcome"], ExecutionOutcome.UNCERTAIN.value)
        self.assertTrue(attempt["before_evidence"] and attempt["after_evidence"], "before and after evidence are both recorded")
        for oid in [*attempt["before_evidence"], *attempt["after_evidence"]]:
            self.assertIsNotNone(h.store.get_observation(oid, with_payload=False), oid)
        self.assertIn("reconcil", attempt["recovery_instruction"])
        steps = h.store.journal_entries("executor.result", intent.action_id)[0]["body"]["steps"]
        self.assertEqual((steps[-1]["step_id"], steps[-1]["status"], steps[-1]["error_type"]), ("select", "exception", "ValueError"))
        self.assertIsNone(executor.run_next(h.snapshot), "nothing is re-queued")


class DuplicateEffectGuardTests(unittest.TestCase):
    def test_act02_unsettled_twin_matches_kind_and_targets_only_while_the_effect_is_unsettled(self):
        """ACT 02 / spec 12.3: the duplicate-effect lookup finds an EXECUTING/VERIFYING/UNCERTAIN/RECONCILING intent with the same
        kind and targets on the same branch, and nothing once that intent is settled (or for other targets, kinds, branches)."""
        h = harness()
        snap = h.snapshot()
        kind, targets = "select_validated_tactic", {"routes": ["/tactics"]}
        intent = h.ready(h.tactic_intent(snap, decision_id="d1"), snap)
        self.assertIsNone(unsettled_twin(h.store, kind, targets, branch_id=h.branch_id), "QUEUED has sent nothing; it is not unsettled")
        h.store.update_intent_state(intent, ActionState.EXECUTING, "test")
        self.assertEqual(unsettled_twin(h.store, kind, targets, branch_id=h.branch_id).action_id, intent.action_id)
        h.store.update_intent_state(intent, ActionState.UNCERTAIN, "test")
        self.assertEqual(unsettled_twin(h.store, kind, targets, branch_id=h.branch_id).action_id, intent.action_id)
        self.assertIsNone(unsettled_twin(h.store, kind, {"routes": ["/squad"]}, branch_id=h.branch_id), "different targets")
        self.assertIsNone(unsettled_twin(h.store, "set.training", targets, branch_id=h.branch_id), "different kind")
        self.assertIsNone(unsettled_twin(h.store, kind, targets, branch_id="branch-elsewhere"), "different branch")
        h.store.update_intent_state(intent, ActionState.RECONCILING, "test")
        self.assertEqual(unsettled_twin(h.store, kind, targets, branch_id=h.branch_id).action_id, intent.action_id)
        h.store.update_intent_state(intent, ActionState.CONFIRMED, "test")
        self.assertIsNone(unsettled_twin(h.store, kind, targets, branch_id=h.branch_id), "a settled effect no longer blocks a new decision")


class VerificationGateTests(unittest.TestCase):
    def test_successful_click_without_effect_fails_verification(self):
        class IgnoringUI(FakeAdapter):
            def _apply(self, step):
                if step.action == "select_tactic":
                    return None      # the click "worked" but nothing changed
                return super()._apply(step)

        h = harness(IgnoringUI())
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.FAILED)
        self.assertIs(report.verdict.kind, VerdictKind.FAILED)
        self.assertEqual(report.inputs_sent, 2)

    def test_unreadable_readback_is_uncertain(self):
        h = harness()
        h.adapter.inject_readback("selected_tactic", ValueStatus.MISSING, "readback screen unavailable")
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN)
        self.assertEqual(report.verdict.readbacks[0]["status"], "missing")

    def test_contract_acceptance_end_to_end(self):
        h = harness()
        executor = h.executor()
        snap = h.snapshot()
        intent = h.ready(h.contract_intent(snap), snap)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertEqual(report.result.confirmed_effect["agreement_id"], "agr-1")
        self.assertEqual(h.adapter.accepted["offer-1"]["offer_id"], "offer-1")
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.CONFIRMED)

    def test_load_queue_picks_up_persisted_queued_intents(self):
        h = harness()
        snap = h.snapshot()
        intent = h.ready(h.tactic_intent(snap), snap)
        executor = h.executor()
        self.assertEqual(executor.load_queue(), [intent.action_id])
        self.assertIs(executor.run_next(h.snapshot).state, ActionState.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
