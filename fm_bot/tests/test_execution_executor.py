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
from .execution_fixtures import TracingAdapter, harness, same_tick_change


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
        """Spec 12.3: a navigation step may be retried, but only after the screen has been identified again - the trace must show
        identify/perform interleaved, never two inputs in a row off one stale screen read."""
        h = harness(TracingAdapter())
        h.adapter.inject("timeout_before_effect", on_step=1)
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertEqual(h.adapter.calls, 3, "one navigation retry, then the consequential step once")
        self.assertEqual(report.inputs_sent, 3)
        # The retry is preceded by its own screen identification, and the consequential step by another one.
        self.assertEqual(h.adapter.trace[:7], [
            ("identify", "home"),                    # the pre-execution screen check
            ("identify", "home"), ("perform", "go_tactics", 1),    # attempt 1: timed out before any effect
            ("identify", "home"), ("perform", "go_tactics", 2),    # the fresh screen check, then the retry
            ("identify", "tactics"), ("perform", "select", 3),     # the consequential step on the screen just identified
        ])
        performs = [entry for entry in h.adapter.trace if entry[0] == "perform"]
        for previous, current in zip(performs, performs[1:]):
            between = h.adapter.trace[h.adapter.trace.index(previous) + 1:h.adapter.trace.index(current)]
            self.assertIn("identify", [entry[0] for entry in between], f"no fresh screen check between {previous} and {current}")

    def test_a_consequential_step_is_never_retried_even_after_a_fresh_screen_check(self):
        """Spec 12.3: the retry allowance is for navigation only. A consequential step that times out is performed exactly once
        and the executor stops there; no second screen check and no second dispatch of the same input (ACT 02)."""
        h = harness(TracingAdapter())
        h.adapter.inject("timeout_before_effect", on_step=2)     # the select times out before any effect
        executor = h.executor()
        queued_tactic(h)
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN, report.reason)
        self.assertIn("never retried", report.reason)
        self.assertEqual(h.adapter.performs_of("select"), [("perform", "select", 2)], "the consequential step ran exactly once")
        self.assertEqual(h.adapter.performs_of("go_tactics"), [("perform", "go_tactics", 1)])
        self.assertEqual(h.adapter.trace.index(("perform", "select", 2)), len(h.adapter.trace) - 2, "nothing was attempted after it but the after-evidence screen read")
        self.assertEqual(h.adapter.selected_tactic_id, "balanced-01")

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


class ActionCriticalFreshnessTests(unittest.TestCase):
    def test_obs03_the_pre_execution_read_takes_a_second_stable_read_of_the_target_routes(self):
        """OBS 03 / spec 5.2, 12.2: the snapshot taken immediately before the input names the intent's target routes as
        action-critical, so they are read twice and compared; the after-evidence read needs no second read."""
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        self.assertEqual(intent.targets, {"routes": ["/tactics"]})
        asked: list[dict] = []
        collected = []

        def provider(**kw):
            asked.append(dict(kw))
            snap = h.collect(**kw)
            collected.append(snap)
            return snap

        report = executor.run_next(provider)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertEqual(asked, [{"action_critical": ["/tactics"]}, {}], "pre-execution asks for the targets; the after read does not")
        self.assertEqual(collected[0].requirements["action_critical"], ["/tactics"])
        self.assertIn("/tactics", collected[0].entity_versions)

    def test_obs03_a_provider_that_cannot_reread_the_targets_expires_the_intent_without_input(self):
        """OBS 03: an unestablished second stable read is never treated as an established one. A pre-execution provider that
        cannot reread the target routes ends the intent EXPIRED with no input sent (spec 5.2: no fabricated freshness)."""
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        report = executor.run_next(lambda: h.collect())        # no action-critical reread possible
        self.assertIs(report.state, ActionState.EXPIRED)
        self.assertEqual([p.check for p in report.problems], ["action_critical"])
        self.assertIn("/tactics", report.reason)
        self.assertIn("unestablished, not assumed", report.reason)
        self.assertEqual(h.adapter.inputs, [])
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.EXPIRED)
        self.assertEqual(h.store.journal_entries("ui.input", intent.action_id), [])

    def test_obs03_a_same_tick_change_to_a_target_route_stops_the_input(self):
        """OBS 03 / spec 5.2, 12.2: ``/tactics`` still agrees with the intent on the first read of the pre-execution snapshot and
        changes before the second, with the game clock standing still. The second stable read catches it: no input is sent, the
        intent expires and the state the input would have acted on is recorded."""
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        counter = same_tick_change(h, "/tactics", stable_reads=1)   # the pre-execution snapshot's first read still agrees; the next one does not
        report = executor.run_next(h.collect)
        self.assertIs(report.state, ActionState.EXPIRED, report.reason)
        self.assertGreaterEqual(counter["reads"], 2, "the target route really was read twice in the pre-execution snapshot")
        self.assertIn("/tactics changed since the decision", report.reason)
        self.assertEqual(h.adapter.inputs, [], "no input went out")
        self.assertEqual(h.adapter.selected_tactic_id, "balanced-01")
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.EXPIRED)
        self.assertEqual(h.store.journal_entries("ui.input", intent.action_id), [])
        # Evidence: the pre-execution read is journaled, asked for the second read, and disagrees with the intent it was checking.
        pre_execution = [e["body"] for e in h.store.journal_entries("snapshot")][-1]
        self.assertEqual(pre_execution["requirements"]["action_critical"], ["/tactics"])
        self.assertNotEqual(pre_execution["entity_versions"]["/tactics"], intent.entity_versions["/tactics"])
        for oid in pre_execution["observation_ids"]:
            self.assertIsNotNone(h.store.get_observation(oid, with_payload=False), oid)

    def test_obs03_a_target_route_still_moving_within_the_tick_invalidates_the_pre_execution_read(self):
        """OBS 03 / spec 5.2: a target route being written to throughout the tick never reads back stable, so no snapshot is
        accepted and no input is sent. The exhausted read is journaled - naming the action-critical route - with its
        observations, so what was seen is preserved instead of replaced by an optimistic assumption."""
        h = harness()
        executor = h.executor()
        intent = queued_tactic(h)
        same_tick_change(h, "/tactics", stable_reads=1, keep_changing=True)
        report = executor.run_next(h.collect)
        self.assertIs(report.state, ActionState.EXPIRED, report.reason)
        self.assertEqual(report.problems[0].check, "snapshot_valid")
        self.assertIn("action-critical route /tactics changed between reads", report.reason)
        self.assertEqual(h.adapter.inputs, [])
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.EXPIRED)
        self.assertEqual(h.store.journal_entries("ui.input", intent.action_id), [])
        exhausted = [e["body"] for e in h.store.journal_entries("snapshot")][-1]
        self.assertEqual(exhausted["consistency"], "attempts_exhausted")
        self.assertTrue(any("action-critical route /tactics changed between reads" in reason for reason in exhausted["consistency_reasons"]), exhausted["consistency_reasons"])
        self.assertTrue(exhausted["observation_ids"])
        for oid in exhausted["observation_ids"]:
            self.assertIsNotNone(h.store.get_observation(oid, with_payload=False), oid)


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

    def test_act01_intent_missing_a_workflow_parameter_is_cancelled_without_input(self):
        """ACT 01: an intent that does not fill a workflow parameter is refused; the template's own None is never sent (spec 12.2-12.3)."""
        h = harness()
        executor = h.executor()
        snap = h.snapshot()
        incomplete = h.factory.create("set.training", "training.set", snap, {"routes": ["/squad"]}, {"previous_settings": {"intensity": "Normal"}}, verification="training_settings_reread", decision_id="dec-no-settings")
        executor.enqueue(h.ready(incomplete, snap))
        report = executor.run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CANCELLED)
        self.assertEqual([p.check for p in report.problems], ["parameters"])
        self.assertIn("settings", report.reason)
        self.assertEqual(h.adapter.inputs, [])
        self.assertEqual(h.adapter.training_settings, {"intensity": "Normal"}, "an unfilled parameter must not wipe the committed program to {}")
        self.assertIs(h.store.get_intent(incomplete.action_id).state, ActionState.CANCELLED)
        self.assertEqual(h.store.journal_entries("ui.input", incomplete.action_id), [])
        # The same intent with the parameter filled goes through, so the refusal is about the gap alone.
        complete = h.training_intent(h.snapshot(), decision_id="dec-with-settings", settings={"intensity": "Double"})
        executor.enqueue(h.ready(complete, h.snapshot()))
        self.assertIs(executor.run_next(h.snapshot).state, ActionState.CONFIRMED)
        self.assertEqual(h.adapter.training_settings, {"intensity": "Double"})

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
