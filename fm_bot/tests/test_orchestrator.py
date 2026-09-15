"""Tests for fm_bot.orchestrator: the four run levels, triggers, polling, the manager lock and Stop (spec 4.2, 12.4; ACT 03, REC 01, MAT 01).

Everything runs against the scripted fixture world; the clock is a
``FakeClock`` so no test waits. Where the shared planner is replaced by a
stub the stub is labelled as such.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from ..bridge_client.client import BridgeClient
from ..bridge_client.transport import FakeTransport
from ..execution.adapter import ANY_SCREEN, FAKE_SCREEN_MODEL, FAKE_WORKFLOWS, RISK_NAVIGATION, FakeAdapter, UIStep, Workflow
from ..execution.lifecycle import IntentFactory, enqueue, validate
from ..interactions.inbox import DeclaredInboxTextProvider, DialogueOption, InboxText, continue_blocked_by_inbox
from ..interactions.language_model import NoLanguageModel, ScriptedLanguageModel
from ..interface.controls import Settings
from ..interface.explain import explain_action, render_action
from ..interface.notify import NotificationKind
from ..interface.status import active_career, set_active_career
from ..orchestrator import (
    CALENDAR_BLOCKED_SUBJECT, JOURNAL_BOUNDARY, JOURNAL_IDENTITY, JOURNAL_INBOX_CHOICE, JOURNAL_LINEAGE_CONFIRMED, JOURNAL_MATCH, JOURNAL_NOT_EXECUTED, JOURNAL_PENDING_ACTIONS,
    JOURNAL_ABANDONED, JOURNAL_PLAN, JOURNAL_RECONCILE, JOURNAL_UNAVAILABLE, MANAGER_LOCK,
    FakeClock, ManagerLockHeld, Orchestrator, PassStatus, RunLevel, Trigger, detect_triggers, lineup_status_of, next_action_of, plan_due, plan_outcome_of,
)
from ..rules.capabilities import CapabilityRegistry
from ..rules.deadlines import CLASSIFICATION_UNRESOLVED_READ, LineupStatus, PendingActionsObservation
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import ActionState, ConsistencyStatus, Decision, DecisionSnapshot
from ..state.status import ValueStatus
from ..state.store import Store
from . import fixtures as fx
from .execution_fixtures import linked_world


def env(data: Any, session: str = fx.SESSION):
    return FakeTransport.envelope(data, session_id=session)


def stub_candidate(kind: str = "select_validated_tactic", status: str = "proposed", **overrides) -> SimpleNamespace:
    base = dict(candidate_id=f"{kind}:1", kind=kind, horizon="next_decision", description=f"stub {kind}", status=status, payload={}, reasons=[], requires=[], unverified=False, submittable=True, targets={"routes": ["/tactics"]}, parameters={"tactic_catalog_id": "counter-02", "catalog_version": 1, "catalog_name": "4-2-3-1 Counter", "previous_tactic_catalog_id": "balanced-01"})
    base.update(overrides)
    return SimpleNamespace(**base)


def stub_planner(candidates: list[SimpleNamespace] | None = None, decisions: list[Decision] | None = None):
    """A stand-in for planning.planner.plan_once with the same call shape (test double, not a planner)."""
    calls: list[dict[str, Any]] = []

    def plan_once(snapshot, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status="planned", candidates=list(candidates or []), decisions=list(decisions or []), summaries=["stub plan"], capability_reports={}, reasons=[])
    plan_once.calls = calls
    return plan_once


CONTINUE_STEP, ANSWER_STEP = "press_continue", "answer_message"
# The in-game moment the fixture world is at; a reading of a message is current evidence only at its own moment.
CURRENT_GAME_TIME = f"{fx.GAME_DATE} {fx.GAME_TIME}"


def continue_workflow() -> Workflow:
    """A validated Continue workflow for the fake UI.

    Navigating to the fake home screen stands in for pressing Continue. Where
    the UI ends up is deliberately not the effect: the intent's verification
    plan reads the in-game clock back from the bridge (spec 12.2).
    """
    return Workflow("fake.continue", "1", "progress.continue", FAKE_SCREEN_MODEL, (UIStep(CONTINUE_STEP, "navigate", ANY_SCREEN, {"target": "home"}, RISK_NAVIGATION, "home"),), None)


def inbox_answer_workflow() -> Workflow:
    """A validated inbox-answer workflow for the fake UI; the effect is read back from ``/inbox`` metadata, not from the screen."""
    return Workflow("fake.respond_inbox", "1", "respond.inbox", FAKE_SCREEN_MODEL, (UIStep(ANSWER_STEP, "navigate", ANY_SCREEN, {"target": "inbox"}, RISK_NAVIGATION, "inbox"),), None)


class ReactingAdapter(FakeAdapter):
    """A fake FM front end whose inputs change what the *bridge* then reports, as the real game does.

    ``reactions`` maps a workflow step id to a callable run once that step has
    succeeded: the in-game clock moves on after Continue, a message stops
    being unread after it is answered. The adapter never reports the effect
    itself, so the verification plans have to establish it from a fresh
    snapshot (test double, not an adapter implementation).
    """

    def __init__(self, *, reactions: dict[str, Any] | None = None, **kw):
        super().__init__(**kw)
        self.reactions = dict(reactions or {})
        self.performed: list[str] = []

    def perform(self, step):
        result = super().perform(step)
        if result.ok:
            self.performed.append(step.step_id)
            reaction = self.reactions.get(step.step_id)
            if reaction is not None:
                reaction()
        return result


class World:
    """A registered career, a scripted bridge, a fake UI and an orchestrator with a fake clock."""

    def __init__(self, *, routes: dict[str, Any] | None = None, linked: bool = False, lineage_confirmed: bool = True, authority: str | None = None, families: list[str] | None = None, allow_continue: bool = False, planner=None, provisions: dict[str, str] | None = None, inbox_text_provider=None, pending_actions_provider=None, language_model=None, workflows: dict[str, Workflow] | None = None, club_id: int = 742, adapter: FakeAdapter | None = None):
        self.store = Store.memory()
        self.career, self.branch, _ = CareerRegistry(self.store).register_career("Wycombe", SaveManifest(fx.BUILD, 90001, club_id, fx.GAME_DATE, fx.GAME_TIME))
        self.adapter = adapter or FakeAdapter()
        world = linked_world(self.adapter) if linked else fx.world()
        for path, value in (routes or {}).items():
            world[path] = value
        self.transport = FakeTransport(world)
        self.client = BridgeClient(self.transport, self.store, context={"career_id": self.career.career_id, "branch_id": self.branch.branch_id})
        self.settings = Settings.load(self.store)
        self.settings.save()
        if authority:
            self.settings.change("authority_mode", authority, reason="test")
        if families is not None:
            self.settings.change("action_families", families, reason="test")
        if allow_continue:
            limits = dict(self.settings.get("spending_limits"))
            limits["allow_continue"] = True
            self.settings.change("spending_limits", limits, reason="test")
        self.clock = FakeClock()
        self.orchestrator = Orchestrator(self.store, self.client, self.adapter, self.settings, career=self.career, branch=self.branch, lineage_confirmed=lineage_confirmed, clock=self.clock, sleep=self.clock.advance, planner=planner, provisions=provisions, inbox_text_provider=inbox_text_provider, pending_actions_provider=pending_actions_provider, language_model=language_model, workflows=workflows)

    def close(self):
        self.orchestrator.close()

    def journal_kinds(self) -> list[str]:
        return [e["kind"] for e in self.store.journal_entries(limit=100_000)]


# ---------------------------------------------------------------------------
# level 1: connection or load
# ---------------------------------------------------------------------------


class ConnectionTests(unittest.TestCase):
    def test_manager_lock_refuses_a_second_instance(self):
        w = World()
        try:
            with self.assertRaises(ManagerLockHeld):
                Orchestrator(w.store, w.client, FakeAdapter(), w.settings, career=w.career, branch=w.branch, clock=w.clock, sleep=w.clock.advance)
            self.assertEqual(w.store.lock_owner(MANAGER_LOCK), w.orchestrator.owner_id)
        finally:
            w.close()
        self.assertIsNone(w.store.lock_owner(MANAGER_LOCK), "close releases the manager lock")

    def test_connect_identifies_career_settles_validates_and_reconciles(self):
        w = World()
        try:
            result = w.orchestrator.connect()
            self.assertTrue(result.ok, result.problems)
            self.assertTrue(result.settle.settled)
            self.assertGreaterEqual(result.settle.reads, 2, "the clock must be read unchanged twice")
            self.assertEqual(w.clock.sleeps, [1.0], "one settle interval slept between reads; no real sleeping")
            self.assertEqual(result.reconciled, [], "nothing was in flight")
            self.assertIn("ui_action_adapter", result.capabilities["supported"])
            self.assertTrue(w.orchestrator.execution_enabled)
            self.assertTrue(w.orchestrator.connected)
            self.assertEqual(result.game_date, fx.GAME_DATE)
        finally:
            w.close()

    def test_settle_waits_for_an_unchanged_clock(self):
        routes = {"/game": [env({"date": fx.GAME_DATE, "time": "10:00"}), env({"date": fx.GAME_DATE, "time": "10:05"}), env({"date": fx.GAME_DATE, "time": "10:05"})]}
        w = World(routes=routes)
        try:
            settled = w.orchestrator.settle()
            self.assertTrue(settled.settled)
            self.assertEqual(settled.reads, 3)
            self.assertEqual(settled.game_time, "10:05")
        finally:
            w.close()

    def test_moving_clock_never_settles_and_keeps_execution_disabled(self):
        ticking = [env({"date": fx.GAME_DATE, "time": f"{10 + i // 60:02d}:{i % 60:02d}"}) for i in range(40)]
        w = World(routes={"/game": ticking})
        try:
            result = w.orchestrator.connect()
            self.assertFalse(result.settle.settled)
            self.assertEqual(result.settle.reads, w.settings.poll_intervals()["settle_max_reads"])
            self.assertIs(result.status, PassStatus.INCONSISTENT)
            self.assertFalse(w.orchestrator.execution_enabled)
            self.assertFalse(w.orchestrator.connected, "an unsettled connection is retried on the next pass")
        finally:
            w.close()

    def test_disconnected_bridge_disables_actions_and_marks_observations_unavailable(self):
        w = World(routes={"/status": (200, fx.status_payload(connected=False))})
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.DISCONNECTED)
            self.assertFalse(w.orchestrator.execution_enabled)
            self.assertIn(JOURNAL_UNAVAILABLE, w.journal_kinds())
            self.assertEqual(w.adapter.inputs, [])
        finally:
            w.close()

    def test_unsupported_build_disables_actions(self):
        w = World(routes={"/status": (200, fx.status_payload(build="25.1.0+1"))})
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.BUILD_UNSUPPORTED)
            self.assertFalse(w.orchestrator.execution_enabled)
            self.assertIn(JOURNAL_UNAVAILABLE, w.journal_kinds())
        finally:
            w.close()

    def test_registered_career_must_match_the_loaded_save(self):
        w = World(club_id=999)
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.IDENTITY_MISMATCH)
            self.assertTrue(any("club 742 differs" in n for n in result.notes), result.notes)
            self.assertFalse(w.orchestrator.execution_enabled)
        finally:
            w.close()

    def test_unconfirmed_lineage_stops_and_names_the_confirmation(self):
        w = World(lineage_confirmed=False)
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.INCONSISTENT)
            self.assertTrue(any("confirm" in n for n in result.notes), result.notes)
            self.assertFalse(w.orchestrator.execution_enabled)
        finally:
            w.close()

    def test_rec01_in_flight_intent_is_reconciled_on_connect_without_input(self):
        w = World(linked=True, authority="scoped", families=["tactics"])
        try:
            factory = IntentFactory(w.store)
            snap = w.orchestrator.collect("setup")
            self.assertTrue(snap.valid, snap.consistency_reasons)
            intent = factory.create("select_validated_tactic", "tactics.select", snap, {"routes": ["/tactics"]}, {"tactic_catalog_id": "counter-02", "catalog_version": 1, "catalog_name": "4-2-3-1 Counter"}, verification="selected_tactic_matches_catalog", decision_id=None)
            registry = CapabilityRegistry.from_status(fx.status_payload(), supported_builds=(fx.BUILD,))
            for name in w.adapter.capabilities():
                registry.provide(name, "ui_adapter:fake")
            self.assertTrue(validate(intent, registry, w.settings.authority_profile(), snap, w.store).ok)
            enqueue(w.store, intent)
            w.store.update_intent_state(intent, ActionState.EXECUTING, "process died after this")
            w.adapter.selected_tactic_id = "counter-02"          # the input had landed before the crash
            result = w.orchestrator.connect()
            self.assertEqual(len(result.reconciled), 1)
            self.assertEqual(result.reconciled[0]["new_state"], "CONFIRMED")
            self.assertIs(w.store.get_intent(intent.action_id).state, ActionState.CONFIRMED)
            self.assertEqual(w.adapter.inputs, [], "reconciliation never sends input")
        finally:
            w.close()


# ---------------------------------------------------------------------------
# levels 2 and 3: decision point and weekly plan
# ---------------------------------------------------------------------------


class DecisionPointAndPlanTests(unittest.TestCase):
    def test_first_pass_handles_mandatory_work_before_optional_planning(self):
        w = World()
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.level, RunLevel.WEEKLY)
            self.assertIs(result.status, PassStatus.BLOCKED)
            point = result.decision_point
            self.assertFalse(point.mandatory_clear, "two unread messages look like required decisions")
            self.assertFalse(result.continue_gate.allowed)
            self.assertTrue(any(r.blocked_action == "progress.continue" and "inbox_text" in r.missing for r in result.blocked))
            entries = w.store.journal_entries(limit=100_000)
            first_notification = next(e["seq"] for e in entries if e["kind"] == "notification" and e["body"]["kind"] == NotificationKind.USER_ACTION_REQUIRED.value)
            plan_seq = next(e["seq"] for e in entries if e["kind"] == JOURNAL_PLAN)
            self.assertLess(first_notification, plan_seq, "the mandatory decision point is processed before the optional plan")
            self.assertEqual(result.plan.status, "planned")
            self.assertTrue(result.plan.decisions)
            for decision in result.plan.decisions:
                stored = w.store.get_decision(decision.decision_id)
                self.assertIsNotNone(stored)
                self.assertEqual(stored.model_versions["settings:authority_profile"], str(w.settings.authority_profile_version))
                self.assertIn("settings:club_objective", stored.model_versions)
            self.assertIsNone(result.next_action, "advise mode proposes nothing for execution")
            self.assertEqual(w.adapter.inputs, [])
            blocked_actions = [r.blocked_action for r in result.blocked]
            self.assertEqual(len(blocked_actions), len(set(blocked_actions)), "one report per blocked action")
        finally:
            w.close()

    def test_unchanged_poll_neither_replans_nor_notifies(self):
        w = World()
        try:
            first = w.orchestrator.run_once()
            decisions_before = len(w.store.list_decisions(limit=1000))
            second = w.orchestrator.run_once()
            self.assertIs(second.status, PassStatus.UNCHANGED)
            self.assertEqual(second.notifications, 0)
            self.assertEqual(second.triggers, [])
            self.assertEqual(len(w.store.list_decisions(limit=1000)), decisions_before)
            self.assertGreater(first.notifications, 0)
            self.assertEqual(w.orchestrator.next_interval(), 5.0, "idle engineering default")
            view = w.orchestrator.status_view()
            self.assertIsNotNone(view.continue_gate, "the operator still sees the last decision point")
        finally:
            w.close()

    def test_new_offer_and_budget_change_trigger_a_replan(self):
        w = World()
        try:
            w.orchestrator.run_once()
            inbox = fx.inbox_payload()
            inbox["messages"].append({"id": 504, "date": fx.GAME_DATE, "time": "10:30", "unread": True, "event_type": "news_item_contract_offer", "sender_id": 95, "sender_name": "DoF", "subject": None, "body": None, "text_status": "not_decoded", "time_status": "current"})
            finances = dict(fx.finances_payload(), balance=10_000_000)
            w.transport.set("/inbox", env(inbox))
            w.transport.set("/finances", env(finances))
            result = w.orchestrator.run_once()
            kinds = {t.kind for t in result.triggers}
            self.assertEqual(kinds, {"new_message", "offer", "budget_change"})
            self.assertIn(result.status, (PassStatus.PLANNED, PassStatus.BLOCKED))
            self.assertTrue(result.changed)
        finally:
            w.close()

    def test_plan_due_on_week_change(self):
        self.assertTrue(plan_due(None, "2024-02-17"))
        self.assertFalse(plan_due("2024-02-12", "2024-02-17"), "same ISO week")
        self.assertTrue(plan_due("2024-02-17", "2024-02-19"), "Monday starts a new week")

    def test_detect_triggers_from_snapshot_diffs(self):
        def snap(**routes):
            return DecisionSnapshot("s", [], ConsistencyStatus.CONSISTENT, [], [], [], {}, "bridge_observed", "c", "b", "sess", "2024-02-17", "10:00", routes=routes)
        before = snap(**{"/squad": [{"id": 1}, {"id": 2}], "/fixtures": {"fixtures": [{"date": "2024-02-20", "time": "19:45", "home": {"club_id": 742}, "away": {"club_id": 804}, "competition_id": 33, "status": "scheduled"}]}})
        after = snap(**{"/squad": [{"id": 1}], "/fixtures": {"fixtures": [{"date": "2024-02-21", "time": "19:45", "home": {"club_id": 742}, "away": {"club_id": 804}, "competition_id": 33, "status": "scheduled"}]}})
        triggers = detect_triggers(before, after, injuries_before={1: False}, injuries_after={1: True})
        self.assertEqual([t.kind for t in triggers], ["fixture_change", "squad_change", "injury"])
        self.assertEqual(detect_triggers(None, after), [], "no previous snapshot means no triggers")
        self.assertEqual(detect_triggers(before, before), [])
        self.assertIsInstance(triggers[0], Trigger)

    def test_inbox_text_read_at_an_earlier_game_time_is_stale_for_the_whole_decision_point(self):
        """spec 5.2 / 12.4: one decision point, one clock. Text declared at an earlier game time is served ``stale`` to the deadline
        rules (pending actions, the Continue gate, the planner) exactly as to the mandatory-inbox check, so the two halves of the
        decision point agree; text declared at the snapshot's own game time is used by both."""
        current = f"{fx.GAME_DATE} {fx.GAME_TIME}"
        provider = DeclaredInboxTextProvider()
        provider.declare(InboxText(501, "informational", "nothing to answer", (), None, False, False), source="test-operator", observed_at="2026-01-01T00:00:00+00:00", game_time="2024-02-10 09:00")
        provider.declare(InboxText(503, "informational", "nothing to answer", (), None, False, False), source="test-operator", observed_at="2026-01-01T00:00:00+00:00", game_time=current)
        w = World(provisions={"pending_actions": "test-operator", "inbox_text": "test-operator"}, inbox_text_provider=provider)
        try:
            snap = w.orchestrator.collect("test")
            self.assertIs(provider.get_text(501, game_time=current).status, ValueStatus.STALE)
            point = w.orchestrator.decision_point(snap)
            stale = next(p for p in point.pending if p.message_id == 501)
            self.assertFalse(stale.resolved, "a stale reading proves nothing; the unread offer still looks like a required decision")
            self.assertTrue(stale.blocks_continue)
            fresh = next(p for p in point.pending if p.message_id == 503)
            self.assertTrue(fresh.resolved, "text read at this game time settles the message")
            self.assertEqual({b.item.message_id for b in point.blockers}, {501}, "the mandatory-inbox check reaches the same verdict")
            self.assertFalse(point.mandatory_clear)
            reading = w.orchestrator._deadline_text_provider(snap)
            self.assertIsNone(reading({"id": 501}), "the planner's reader ignores the stale text too")
            self.assertEqual(reading({"id": 503})["requires_decision"], False)
        finally:
            w.close()

    def test_material_events_backoff_on_errors_is_bounded(self):
        w = World()
        try:
            w.orchestrator.run_once()
            w.transport.set("/status", (503, {"error": "unavailable"}))
            intervals = [w.orchestrator.run_once().next_poll_seconds for _ in range(6)]
            self.assertEqual(intervals[:3], [10.0, 20.0, 40.0])
            self.assertEqual(intervals[-1], 60.0, "capped at max_backoff_seconds")
            self.assertTrue(all(i <= 60.0 for i in intervals))
        finally:
            w.close()

    def test_planner_adapter_reads_the_shared_planner_report(self):
        from ..planning import planner as module
        w = World()
        try:
            snap = w.orchestrator.collect("test")
            report = module.plan_once(snap, store=None)
            outcome = plan_outcome_of(report)
            self.assertEqual(outcome.status, "planned")
            self.assertEqual(outcome.lineup_status.status, "unverified")
            self.assertIsNone(outcome.next_action)
            self.assertTrue(outcome.summary_lines)
            self.assertTrue(all(r.blocked for r in outcome.missing))
        finally:
            w.close()

    def test_next_action_only_from_proposed_executable_candidates(self):
        proposed = stub_candidate()
        blocked = stub_candidate(kind="submit.lineup", status="blocked")
        cont = stub_candidate(kind="progress.continue")
        report = SimpleNamespace(candidates=[cont, blocked, proposed], decisions=[])
        action = next_action_of(report, [])
        self.assertEqual(action["kind"], "select_validated_tactic")
        self.assertEqual(action["authority_scope"], "tactics.select")
        self.assertEqual(action["verification"], "selected_tactic_matches_catalog")
        self.assertIsNone(next_action_of(SimpleNamespace(candidates=[cont, blocked], decisions=[]), []), "Continue goes through the gate, never as a plain action")
        unverified = stub_candidate(kind="submit.lineup", unverified=True)
        self.assertIsNone(next_action_of(SimpleNamespace(candidates=[unverified]), []), "an unverified eleven is never submitted")

    def test_lineup_status_of_candidates(self):
        self.assertEqual(lineup_status_of(SimpleNamespace(candidates=[])).status, "missing")
        self.assertEqual(lineup_status_of(SimpleNamespace(candidates=[stub_candidate(kind="submit.lineup")])).status, "verified")
        self.assertEqual(lineup_status_of(SimpleNamespace(candidates=[stub_candidate(kind="advise.lineup", status="infeasible")])).status, "infeasible")
        self.assertEqual(lineup_status_of(SimpleNamespace(candidates=[stub_candidate(kind="advise.lineup", status="advisory", unverified=True)])).status, "unverified")
        self.assertEqual(lineup_status_of(SimpleNamespace(candidates=[stub_candidate(kind="advise.lineup", status="blocked", reasons=["no upcoming fixture observed"])])).status, "not_required")


# ---------------------------------------------------------------------------
# execution, Stop and Continue
# ---------------------------------------------------------------------------


class ExecutionTests(unittest.TestCase):
    def test_proposed_action_is_executed_verified_and_explained(self):
        planner = stub_planner([stub_candidate()])
        w = World(linked=True, authority="scoped", families=["tactics"], planner=planner)
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.ACTED)
            self.assertIs(result.executed.state, ActionState.CONFIRMED)
            self.assertEqual(w.adapter.selected_tactic_id, "counter-02")
            self.assertEqual(len(w.adapter.inputs), 2)
            kinds = [n.kind for n in w.orchestrator.notifier.history]
            self.assertIn(NotificationKind.COMPLETED, kinds)
            explanation = explain_action(result.executed.action_id, w.store)
            self.assertEqual(explanation.limits["status"], "available")
            self.assertEqual(explanation.limits["authority_profile_version"], w.settings.authority_profile_version)
            self.assertEqual(explanation.decision.settings_versions["settings:authority_profile"], str(w.settings.authority_profile_version))
            self.assertTrue(explanation.before_evidence and explanation.after_evidence)
            self.assertEqual(planner.calls[0]["authority"].mode.value, "scoped_execution")
            self.assertIsNone(planner.calls[0]["store"], "the planner does not persist; the orchestrator stamps and stores decisions")
            second = w.orchestrator.run_once()
            self.assertIs(second.status, PassStatus.UNCHANGED)
            self.assertEqual(second.notifications, 0)
        finally:
            w.close()

    def test_advise_mode_records_advice_and_sends_nothing(self):
        w = World(linked=True, planner=stub_planner([stub_candidate()]))
        try:
            result = w.orchestrator.run_once()
            self.assertIsNot(result.status, PassStatus.ACTED)
            self.assertTrue(any("advice only" in n for n in result.notes), result.notes)
            self.assertEqual(w.adapter.inputs, [])
            self.assertEqual(w.store.list_intents(), [])
        finally:
            w.close()

    def test_family_outside_scope_is_refused_before_any_input(self):
        w = World(linked=True, authority="scoped", families=["training"], planner=stub_planner([stub_candidate()]))
        try:
            result = w.orchestrator.run_once()
            self.assertTrue(any("OUTSIDE_SCOPE" in n for n in result.notes), result.notes)
            self.assertEqual(w.adapter.inputs, [])
        finally:
            w.close()

    def test_act03_stop_cancels_queued_work_and_prevents_input(self):
        planner = stub_planner([stub_candidate()])
        w = World(linked=True, authority="scoped", families=["tactics"], planner=planner)
        try:
            snap = w.orchestrator.collect("setup")
            intent = IntentFactory(w.store).create("select_validated_tactic", "tactics.select", snap, {"routes": ["/tactics"]}, {"tactic_catalog_id": "counter-02", "catalog_version": 1}, verification="selected_tactic_matches_catalog")
            w.store.update_intent_state(intent, ActionState.VALIDATED, "test")
            enqueue(w.store, intent)
            cancelled = w.orchestrator.stop("operator pressed Stop")
            self.assertEqual(cancelled, [intent.action_id])
            self.assertIs(w.store.get_intent(intent.action_id).state, ActionState.CANCELLED)
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.STOPPED)
            self.assertEqual(w.adapter.inputs, [])
            self.assertEqual(planner.calls, [], "nothing is planned while Stop is engaged")
            self.assertTrue(w.orchestrator.status_view().stop["engaged"])
            passes = w.orchestrator.run(max_iterations=3)
            self.assertEqual(len(passes), 1, "the loop stops on Stop")
            w.orchestrator.resume()
            resumed = w.orchestrator.run_once()
            self.assertIs(resumed.status, PassStatus.ACTED)
            self.assertEqual(w.adapter.selected_tactic_id, "counter-02")
        finally:
            w.close()

    def test_a_kind_without_a_validated_workflow_is_an_unsupported_workflow_not_a_failed_action(self):
        """spec 14, 15.1: the adapter has no validated workflow for the proposed kind, so the work is reported as an unsupported
        mandatory workflow naming that workflow. No decision is recorded, no intent is minted (and therefore none is cancelled at
        preflight and reported as a failure), and nothing is sent."""
        planner = stub_planner([stub_candidate()])
        w = World(linked=True, authority="scoped", families=["tactics"], planner=planner, workflows={})
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.BLOCKED, result.notes)
            report = next(r for r in result.blocked if r.blocked_action == "select_validated_tactic")
            self.assertEqual(report.missing, ["ui_workflow:select_validated_tactic"])
            self.assertIn("no validated workflow", report.reasons["ui_workflow:select_validated_tactic"])
            self.assertIn("'fake'", report.reasons["ui_workflow:select_validated_tactic"])
            self.assertIsNone(result.executed)
            self.assertEqual(w.store.list_intents(branch_id=w.branch.branch_id), [], "nothing is minted for a workflow that does not exist")
            self.assertEqual([d.kind for d in w.store.list_decisions(limit=1000) if d.kind.startswith("execution.")], [])
            self.assertEqual(w.adapter.inputs, [])
            kinds = [n.kind for n in w.orchestrator.notifier.history]
            self.assertIn(NotificationKind.UNSUPPORTED_MANDATORY_WORKFLOW, kinds)
            self.assertNotIn(NotificationKind.FAILED, kinds, "an unsupported workflow is not an action that failed")
            journaled = [e["body"] for e in w.store.journal_entries(kind=JOURNAL_NOT_EXECUTED)]
            self.assertTrue(any(b.get("missing", {}).get("missing") == ["ui_workflow:select_validated_tactic"] for b in journaled), journaled)
        finally:
            w.close()

    def test_act02_timeout_after_successful_accept_reconciles_in_process_and_never_dispatches_twice(self):
        """ACT 02 / spec 12.3, within one process: the consequential input lands but its confirmation times out (UNCERTAIN). Every
        later pass reconciles that intent from readback before replanning; while the readback cannot establish the effect (the UI
        says the change landed, the bridge still shows the old tactic: sources contradict) the replanned duplicate is refused by
        the duplicate-effect guard, so the acceptance is never dispatched a second time; once the bridge corroborates it the
        intent is CONFIRMED and nothing further is proposed or sent."""
        def bridge_aware_planner():
            """Proposes the counter tactic while the bridge still reports the balanced one (test double, not a planner)."""
            calls: list[dict[str, Any]] = []

            def plan_once(snapshot, **kwargs):
                calls.append(kwargs)
                stored = (snapshot.routes.get("/tactics") or {}).get("stored_name")
                candidates = [stub_candidate()] if stored != "4-2-3-1 Counter" else []
                return SimpleNamespace(status="planned", candidates=candidates, decisions=[], summaries=["stub plan"], capability_reports={}, reasons=[])
            plan_once.calls = calls
            return plan_once

        planner = bridge_aware_planner()
        w = World(authority="scoped", families=["tactics"], planner=planner)      # not linked: the bridge does not yet reflect the UI change
        try:
            w.adapter.inject("timeout_after_success", on_step=2)
            first = w.orchestrator.run_once()
            self.assertIs(first.status, PassStatus.ACTED, first.notes)
            self.assertIs(first.executed.state, ActionState.UNCERTAIN, first.executed.reason)
            self.assertEqual(w.adapter.selected_tactic_id, "counter-02", "the effect landed; only reconciliation may say so")
            self.assertEqual(w.adapter.calls, 2)
            action_id = first.executed.action_id
            # a material event replans: the uncertain intent is reconciled first, and the replanned duplicate is refused
            w.transport.set("/finances", env(dict(fx.finances_payload(), balance=10_000_000)))
            second = w.orchestrator.run_once()
            self.assertEqual([d["action_id"] for d in second.reconciled], [action_id])
            self.assertEqual(second.reconciled[0]["new_state"], "UNCERTAIN")
            self.assertIn("contradict", second.reconciled[0]["reason"])
            self.assertIsNotNone(second.next_action, "the bridge still shows the old tactic, so the same change is proposed again")
            self.assertIsNone(second.executed)
            self.assertTrue(any("duplicate-effect guard" in n and action_id in n for n in second.notes), second.notes)
            self.assertEqual(w.adapter.calls, 2, "no second dispatch")
            self.assertEqual([i["action"] for i in w.adapter.inputs], ["navigate", "select_tactic"])
            self.assertEqual([(i.action_id, i.state) for i in w.store.list_intents()], [(action_id, ActionState.UNCERTAIN)], "no second intent was minted")
            refused = w.store.journal_entries(kind=JOURNAL_NOT_EXECUTED)[-1]["body"]
            self.assertEqual(refused["unsettled_action_id"], action_id)
            self.assertIn(JOURNAL_RECONCILE, w.journal_kinds())
            # the bridge now corroborates the UI readback: reconciliation confirms, and the plan proposes nothing
            tactics = fx.tactics_payload()
            tactics["stored_name"] = "4-2-3-1 Counter"
            w.transport.set("/tactics", env(tactics))
            third = w.orchestrator.run_once()
            self.assertEqual(third.reconciled[0]["new_state"], "CONFIRMED", third.reconciled)
            self.assertIs(w.store.get_intent(action_id).state, ActionState.CONFIRMED)
            self.assertIsNone(third.executed)
            self.assertEqual(w.adapter.calls, 2, "reconciliation sent nothing")
            transitions = [e["body"]["to"] for e in w.store.journal_entries("intent.transition", action_id)]
            self.assertEqual(transitions, ["VALIDATED", "QUEUED", "EXECUTING", "UNCERTAIN", "RECONCILING", "UNCERTAIN", "RECONCILING", "CONFIRMED"])
            self.assertNotIn("QUEUED", transitions[3:], "never silently re-queued")
            self.assertEqual(len(w.store.list_intents()), 1)
        finally:
            w.close()

    def test_id01_a_restarted_orchestrator_judges_continuity_against_the_witnessed_anchor(self):
        """ID 01 / spec 5.1: the anchor is remembered per career branch, so a *new* orchestrator over the same database starts from
        the history the bot actually witnessed. A save reloaded while no bot was running is therefore a stop for identity
        resolution, not a first observation merged onto the production branch, and the local sequence keeps moving forward."""
        w = World(linked=True, authority="scoped", families=["tactics"])
        try:
            first = w.orchestrator.run_once()
            self.assertIn(first.status, (PassStatus.PLANNED, PassStatus.BLOCKED), first.notes)
            witnessed = w.store.get_anchor(w.career.career_id, w.branch.branch_id)
            self.assertEqual((witnessed.game_date, witnessed.game_time), (fx.GAME_DATE, fx.GAME_TIME))
            decisions_before = len(w.store.list_decisions(limit=1000))
        finally:
            w.orchestrator.close()
        # a new process over the same database: the operator loaded the 10 February save while the bot was down
        w.transport.set("/game", env({"date": "2024-02-10", "time": "10:00"}))
        restarted = Orchestrator(w.store, w.client, w.adapter, w.settings, career=w.career, branch=w.branch, lineage_confirmed=True, clock=w.clock, sleep=w.clock.advance)
        try:
            self.assertEqual(restarted._anchor, witnessed, "the restarted process starts from the witnessed anchor, not from nothing")
            self.assertEqual(restarted._sequence, witnessed.sequence, "and continues its local sequence")
            result = restarted.run_once()
            self.assertIs(result.status, PassStatus.IDENTITY_RESOLUTION_REQUIRED, result.notes)
            self.assertTrue(any("date_reversed" in note for note in result.notes), result.notes)
            self.assertTrue(restarted.identity_resolution_required)
            self.assertFalse(restarted.execution_enabled)
            self.assertEqual(len(w.store.list_decisions(limit=1000)), decisions_before, "nothing is decided on the branch while the lineage is unresolved")
            self.assertEqual(w.store.list_intents(), [])
            self.assertEqual(w.adapter.inputs, [])
            self.assertEqual(w.store.get_anchor(w.career.career_id, w.branch.branch_id), witnessed, "the anchor stays where the witnessed history ended")
            restarted.confirm_lineage("I loaded the 10 February save on purpose")
            self.assertIsNone(w.store.get_anchor(w.career.career_id, w.branch.branch_id), "the overruled history is not compared against again")
        finally:
            restarted.close()

    def test_id01_reload_of_an_earlier_checkpoint_stops_for_identity_resolution(self):
        """ID 01 / spec 5.1: mid-session the operator loads an earlier save (``/game`` goes backwards). The next pass is a stop for
        identity resolution: execution is disabled, queued work is cancelled without input, and no decision or intent is recorded on
        the branch while observations keep being journaled, until the operator confirms the lineage; then the bot reconnects from
        the loaded save."""
        w = World(linked=True, authority="scoped", families=["tactics"])          # the shared planner: it records decisions per horizon
        try:
            first = w.orchestrator.run_once()
            self.assertIn(first.status, (PassStatus.PLANNED, PassStatus.BLOCKED), first.notes)
            self.assertEqual(first.game_date, fx.GAME_DATE)
            decisions_before = len(w.store.list_decisions(limit=1000))
            self.assertGreater(decisions_before, 0)
            snap = w.orchestrator.collect("setup")
            intent = IntentFactory(w.store).create("select_validated_tactic", "tactics.select", snap, {"routes": ["/tactics"]}, {"tactic_catalog_id": "counter-02", "catalog_version": 1}, verification="selected_tactic_matches_catalog")
            self.assertTrue(validate(intent, w.orchestrator.capabilities, w.settings.authority_profile(), snap, w.store).ok)
            enqueue(w.store, intent)
            action = next_action_of(SimpleNamespace(candidates=[stub_candidate()], decisions=[]), [])
            w.transport.set("/game", env({"date": "2024-02-10", "time": "10:00"}))          # the operator loaded the 10 February save
            second = w.orchestrator.run_once()
            self.assertIs(second.status, PassStatus.IDENTITY_RESOLUTION_REQUIRED, second.notes)
            self.assertTrue(any("date_reversed" in n for n in second.notes), second.notes)
            self.assertTrue(any("confirm" in n for n in second.notes), second.notes)
            self.assertTrue(w.orchestrator.identity_resolution_required)
            self.assertFalse(w.orchestrator.execution_enabled)
            self.assertFalse(w.orchestrator.lineage_confirmed)
            self.assertIsNone(second.plan)
            self.assertIsNone(second.executed)
            self.assertIs(w.store.get_intent(intent.action_id).state, ActionState.CANCELLED, "queued work is cancelled")
            self.assertEqual(w.adapter.inputs, [])
            stop = w.store.journal_entries(kind=JOURNAL_IDENTITY)[-1]["body"]
            self.assertEqual((stop["status"], stop["cancelled"], stop["execution"]), ("date_reversed", [intent.action_id], "disabled"))
            self.assertIn(NotificationKind.USER_ACTION_REQUIRED, [n.kind for n in w.orchestrator.notifier.history])
            # later passes keep journaling what they see, but decide, mint and send nothing
            observations_before = len(w.store.list_observations(branch_id=w.branch.branch_id, limit=100_000))
            third = w.orchestrator.run_once()
            self.assertIs(third.status, PassStatus.IDENTITY_RESOLUTION_REQUIRED)
            self.assertGreater(len(w.store.list_observations(branch_id=w.branch.branch_id, limit=100_000)), observations_before, "observations are still journaled")
            self.assertEqual(len(w.store.list_decisions(limit=1000)), decisions_before, "no decision recorded on the branch")
            self.assertEqual(len(w.store.list_intents()), 1, "no intent minted")
            self.assertEqual(len(w.store.journal_entries(kind=JOURNAL_IDENTITY)), 1, "the stop is recorded once, not every poll")
            refusal = w.orchestrator.execute(action, snap)
            self.assertIsInstance(refusal, str)
            self.assertIn("identity resolution required", refusal)
            self.assertEqual(w.orchestrator.plan(snap).status, "unavailable")
            self.assertEqual(len(w.store.list_decisions(limit=1000)), decisions_before)
            self.assertFalse(w.orchestrator.status_view().career["lineage_confirmed"])
            # the operator confirms the lineage: continuity restarts from the loaded save and the bot decides again
            w.orchestrator.confirm_lineage("the 10 February save was loaded on purpose")
            self.assertIn(JOURNAL_LINEAGE_CONFIRMED, w.journal_kinds())
            fourth = w.orchestrator.run_once()
            self.assertIn(fourth.status, (PassStatus.PLANNED, PassStatus.BLOCKED), fourth.notes)
            self.assertEqual(fourth.game_date, "2024-02-10")
            self.assertTrue(w.orchestrator.execution_enabled)
            self.assertFalse(w.orchestrator.identity_resolution_required)
            self.assertGreater(len(w.store.list_decisions(limit=1000)), decisions_before, "decisions resume on the confirmed lineage")
        finally:
            w.close()

    def test_id01_confirm_lineage_persists_against_the_registered_career_without_forking_it(self):
        """ID 01 / spec 5.1: the operator's confirmation is recorded against the career and branch already registered, so a restarted
        process does not stop again; no second career, branch or checkpoint is created, and the active career pointer is not moved."""
        w = World(linked=True)
        try:
            set_active_career(w.store, w.career.career_id, w.branch.branch_id, lineage_confirmed=False)
            w.orchestrator.run_once()
            w.transport.set("/game", env({"date": "2024-02-10", "time": "10:00"}))
            self.assertIs(w.orchestrator.run_once().status, PassStatus.IDENTITY_RESOLUTION_REQUIRED)
            careers, branches = len(w.store.list_careers()), len(w.store.list_branches(w.career.career_id))
            checkpoints = len(w.store.list_checkpoints(w.branch.branch_id))
            w.orchestrator.confirm_lineage("the 10 February save was loaded on purpose")
            self.assertFalse(w.orchestrator.identity_resolution_required)
            stored, _ = w.store.get_setting("registry:active_career")
            self.assertEqual(stored, {"career_id": w.career.career_id, "branch_id": w.branch.branch_id, "lineage_confirmed": True})
            self.assertEqual((len(w.store.list_careers()), len(w.store.list_branches(w.career.career_id)), len(w.store.list_checkpoints(w.branch.branch_id))), (careers, branches, checkpoints))
            entry = w.store.journal_entries(kind=JOURNAL_LINEAGE_CONFIRMED)[-1]["body"]
            self.assertEqual((entry["career_id"], entry["branch_id"], entry["persisted"]), (w.career.career_id, w.branch.branch_id, True))
            self.assertTrue(active_career(w.store)[2], "a restarted process starts from a confirmed lineage")
        finally:
            w.close()

    def test_lock_lost_stops_the_pass(self):
        w = World()
        try:
            w.store.release_lock(MANAGER_LOCK, w.orchestrator.owner_id)
            w.store.acquire_lock(MANAGER_LOCK, "intruder")
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.LOCK_LOST)
        finally:
            w.orchestrator.lock_held = False
            w.close()

    def test_continue_is_gated_by_blocking_inbox_messages(self):
        w = World(linked=True, authority="scoped", families=["progression"], allow_continue=True, provisions={"pending_actions": "test-operator"})
        try:
            result = w.orchestrator.run_once()
            self.assertFalse(result.continue_gate.allowed)
            self.assertTrue(any("calendar blocked" in n for n in result.notes), result.notes)
            self.assertNotIn(JOURNAL_BOUNDARY, w.journal_kinds())
            self.assertEqual(w.adapter.inputs, [])
        finally:
            w.close()

    def _continue_world(self, adapter: ReactingAdapter, provider: DeclaredInboxTextProvider | None = None) -> World:
        """A world whose calendar is clear, with a validated Continue workflow and the real Continue the orchestrator mints."""
        return World(linked=True, adapter=adapter, authority="scoped", families=["progression"], allow_continue=True,
                     provisions={"pending_actions": "test-operator", "inbox_text": "test-operator"}, inbox_text_provider=provider or answered_inbox(501, 503),
                     workflows={**FAKE_WORKFLOWS, "progress.continue": continue_workflow()})

    @staticmethod
    def _game_moves_on(world: World, provider: DeclaredInboxTextProvider, moments: list[tuple[str, str]]):
        """What the game does when Continue is pressed: the clock moves to the next moment, and the inbox reader looks again there.

        The second reading matters because in-game time is the only clock: the
        reading taken at the previous decision point is not evidence about
        this one (spec 5.2).
        """
        def pressed() -> None:
            date, time = moments.pop(0)
            world.transport.set("/game", env({"date": date, "time": time}))
            declare_nothing_to_answer(provider, f"{date} {time}", 501, 503)
        return pressed

    def test_cal01_continue_is_confirmed_by_the_game_advancing_then_settles_and_recollects(self):
        """CAL 01 / spec 12.2, 12.4: the Continue the orchestrator itself mints names no target screen - a changed screen is not
        evidence that the calendar moved - and is confirmed only because the in-game clock of a fresh snapshot has moved on from the
        moment the intent recorded. Afterwards the bot settles and collects again."""
        adapter, provider = ReactingAdapter(), answered_inbox(501, 503)
        w = self._continue_world(adapter, provider)
        adapter.reactions[CONTINUE_STEP] = self._game_moves_on(w, provider, [("2024-02-18", "09:00")])
        try:
            result = w.orchestrator.run_once()
            self.assertTrue(result.continue_gate.allowed, result.continue_gate.to_json())
            self.assertIn(JOURNAL_BOUNDARY, w.journal_kinds(), "the expected boundary is recorded before Continue")
            self.assertIs(result.status, PassStatus.ACTED, result.notes)
            self.assertIs(result.executed.state, ActionState.CONFIRMED, result.executed.reason)
            intent = w.store.get_intent(result.executed.action_id)
            self.assertEqual(intent.verification, "game_advanced_past_boundary", "Continue is verified by its effect, not by a screen")
            self.assertNotIn("target", intent.parameters)
            self.assertNotIn("expected_screen", intent.parameters)
            self.assertEqual((intent.parameters["from_game_date"], intent.parameters["from_game_time"]), (fx.GAME_DATE, fx.GAME_TIME))
            effect = result.executed.verdict.details["effect"]
            self.assertEqual(effect["from"], {"game_date": fx.GAME_DATE, "game_time": fx.GAME_TIME})
            self.assertEqual((effect["game_date"], effect["game_time"]), ("2024-02-18", "09:00"))
            self.assertTrue(all(r["source"].startswith("bridge:/game@") for r in result.executed.verdict.readbacks), result.executed.verdict.readbacks)
            labels = [e["body"]["requirements"]["label"] for e in w.store.journal_entries(kind="snapshot", limit=1000)]
            self.assertIn("post-progression", labels, "after progression the bot settles and collects again")
            self.assertGreaterEqual(w.clock.sleeps.count(1.0), 2, "settled twice: on connect and after progression")
        finally:
            w.close()

    def test_cal01_a_second_continue_follows_the_confirmed_first_one(self):
        """CAL 01 / spec 12.3, 12.4 (ACT 02): because the first Continue's effect is established from the in-game clock, its intent
        settles CONFIRMED and the next decision point may press Continue again. No unsettled twin refuses the second one, nothing is
        re-dispatched, and each intent records the moment it moved the calendar on from."""
        adapter, provider = ReactingAdapter(), answered_inbox(501, 503)
        w = self._continue_world(adapter, provider)
        adapter.reactions[CONTINUE_STEP] = self._game_moves_on(w, provider, [("2024-02-18", "09:00"), ("2024-02-19", "09:00")])
        try:
            first = w.orchestrator.run_once()
            second = w.orchestrator.run_once()
            self.assertIs(first.status, PassStatus.ACTED, first.notes)
            self.assertIs(second.status, PassStatus.ACTED, second.notes)
            self.assertIs(second.executed.state, ActionState.CONFIRMED, second.executed.reason)
            self.assertTrue(any(t.kind == "date_advanced" for t in second.triggers), [t.to_json() for t in second.triggers])
            intents = [i for i in w.store.list_intents() if i.kind == "progress.continue"]
            self.assertEqual(len(intents), 2, "the second Continue was minted, not refused by the duplicate-effect guard")
            self.assertEqual([i.state for i in intents], [ActionState.CONFIRMED, ActionState.CONFIRMED])
            self.assertEqual([i.parameters["from_game_date"] for i in intents], [fx.GAME_DATE, "2024-02-18"])
            self.assertEqual(adapter.performed, [CONTINUE_STEP, CONTINUE_STEP], "one input per Continue; nothing retried")
            self.assertEqual([e["body"]["reason"] for e in w.store.journal_entries(kind=JOURNAL_NOT_EXECUTED) if "duplicate-effect" in e["body"].get("reason", "")], [])
            self.assertEqual(second.reconciled, [], "nothing was left unsettled to reconcile")
        finally:
            w.close()

    def test_cal01_an_unverifiable_continue_is_closed_explicitly_instead_of_wedging_the_calendar(self):
        """CAL 01 / spec 12.3, 12.4 (ACT 02): a Continue whose effect no readback can establish - here an intent an earlier process
        left in flight without recording the in-game moment it was to move on from - keeps every later Continue out while
        reconciliation tries, is never re-sent and never re-queued, and is finally closed explicitly, with its evidence, an
        operator-visible reason and a notification, so the calendar is not wedged for good."""
        adapter, provider = ReactingAdapter(), answered_inbox(501, 503)
        w = self._continue_world(adapter, provider)
        adapter.reactions[CONTINUE_STEP] = self._game_moves_on(w, provider, [("2024-02-19", "09:00")])
        try:
            w.orchestrator.validate_capabilities(fx.status_payload())     # as the connection step would, before the earlier process died
            snap = w.orchestrator.collect("setup")
            stale = IntentFactory(w.store).create("progress.continue", "progression.continue", snap, {"routes": ["/inbox", "/fixtures"]}, {"expected_boundary": None}, verification="game_advanced_past_boundary")
            self.assertTrue(validate(stale, w.orchestrator.capabilities, w.settings.authority_profile(), snap, w.store).ok)
            enqueue(w.store, stale)
            w.store.update_intent_state(stale, ActionState.EXECUTING, "left in flight by an earlier process")
            passes = [w.orchestrator.run_once() for _ in range(3)]
            self.assertTrue(any("duplicate-effect guard" in n for n in passes[0].notes), passes[0].notes)
            self.assertEqual(adapter.performed, [], "no input was ever sent for the unsettled intent")
            abandoned = w.store.journal_entries(kind=JOURNAL_ABANDONED)
            self.assertEqual(len(abandoned), 1, [d for p in passes for d in p.reconciled])
            body = abandoned[0]["body"]
            self.assertEqual((body["action_id"], body["kind"], body["rounds"], body["effect"]), (stale.action_id, "progress.continue", 3, "unestablished"))
            self.assertEqual(body["verdict"]["kind"], "uncertain", "the evidence keeps saying the effect was never established")
            self.assertTrue(body["evidence"], "the observations it was judged on are preserved")
            closed = w.store.get_intent(stale.action_id)
            self.assertIs(closed.state, ActionState.FAILED)
            self.assertIn("abandoned as unverifiable", closed.state_reason)
            self.assertIn("a person must check in the game", closed.state_reason)
            transitions = [e["body"]["to"] for e in w.store.journal_entries("intent.transition", stale.action_id)]
            self.assertNotIn("QUEUED", transitions[transitions.index("EXECUTING"):], "an in-flight intent is never silently re-queued")
            self.assertIn(NotificationKind.USER_ACTION_REQUIRED, [n.kind for n in w.orchestrator.notifier.history])
            # with the unverifiable intent closed, the next decision point may press Continue again
            w.transport.set("/game", env({"date": "2024-02-18", "time": "09:00"}))
            declare_nothing_to_answer(provider, "2024-02-18 09:00", 501, 503)      # the reader looks at the inbox at the new moment
            after = w.orchestrator.run_once()
            self.assertIs(after.status, PassStatus.ACTED, after.notes)
            self.assertIs(after.executed.state, ActionState.CONFIRMED, after.executed.reason)
            self.assertEqual(adapter.performed, [CONTINUE_STEP])
        finally:
            w.close()


# ---------------------------------------------------------------------------
# the one Continue gate, inbox answers and the workflow catalogue
# ---------------------------------------------------------------------------


def fixture_tomorrow() -> dict[str, Any]:
    """The fixture world with its next scheduled fixture the day after the snapshot, so the lineup gate is in horizon."""
    payload = fx.fixtures_payload()
    for entry in payload["fixtures"]:
        if entry["status"] == "scheduled":
            entry["date"] = "2024-02-18"
            break
    return payload


def declare_nothing_to_answer(provider: DeclaredInboxTextProvider, game_time: str, *message_ids: int) -> DeclaredInboxTextProvider:
    """Record that a reader looked at each message *at this in-game moment* and found nothing to answer.

    In-game time is the only clock, so a reading is only current evidence at
    the moment it was taken (spec 5.2): a decision point at a later moment
    needs the reader to look again, which is what a real provider does.
    """
    for message_id in message_ids:
        provider.declare(InboxText(message_id, "informational", "nothing to answer", (), None, False, False), source="test-operator", observed_at="2026-01-01T00:00:00+00:00", game_time=game_time)
    return provider


def answered_inbox(*message_ids: int, game_time: str = CURRENT_GAME_TIME) -> DeclaredInboxTextProvider:
    """A text provider that read every given message at ``game_time`` (the fixture's own moment) and found nothing to answer."""
    return declare_nothing_to_answer(DeclaredInboxTextProvider(), game_time, *message_ids)


class ContinueGateTests(unittest.TestCase):
    """CAL 01 / spec 12.4, 15.1: one Continue gate decides a decision point, and it is the one the operator hears about."""

    def _lineup_world(self, candidate: SimpleNamespace) -> World:
        return World(linked=True, routes={"/fixtures": env(fixture_tomorrow())}, planner=stub_planner([candidate]), authority="scoped", families=["progression", "selection"], allow_continue=True,
                     provisions={"pending_actions": "test-operator", "inbox_text": "test-operator"}, inbox_text_provider=answered_inbox(501, 503))

    def test_cal01_the_gate_that_decides_is_the_only_one_and_the_only_one_notified(self):
        """CAL 01 / spec 12.4, 15.1: the lineup verdict only exists after the plan, so the gate is evaluated once, with it. A plan
        whose eleven is verified leaves the calendar clear, and the operator is not told it cannot move on: nothing is notified
        that the deciding gate does not name."""
        submit = stub_candidate(kind="submit.lineup", status="proposed", unverified=False, submittable=True, payload={"fixture": {"identity": None}}, targets={"routes": ["/squad"]}, parameters={"player_ids": [1001], "roles": {}})
        w = self._lineup_world(submit)
        try:
            result = w.orchestrator.run_once()
            self.assertTrue(result.continue_gate.allowed, result.continue_gate.to_json())
            self.assertIs(result.decision_point.gate, result.continue_gate, "one gate per decision point: the recorded decision point carries the gate that decided")
            required = [n for n in w.orchestrator.notifier.history if n.kind is NotificationKind.USER_ACTION_REQUIRED]
            self.assertEqual(required, [], "the calendar is clear, so nothing asks the operator to unblock it")
            for notification in w.orchestrator.notifier.history:
                self.assertNotIn("no lineup status supplied", notification.detail)
        finally:
            w.close()

    def test_cal01_an_unverified_eleven_blocks_the_gate_and_the_notification_uses_its_words(self):
        """CAL 01 / spec 15.1: when the plan really cannot verify the eleven, the deciding gate blocks and the single notification
        carries that gate's own reason, not a placeholder from an evaluation made before the plan ran."""
        advisory = stub_candidate(kind="advise.lineup", status="advisory", unverified=True, submittable=False, payload={"fixture": {"identity": None}})
        w = self._lineup_world(advisory)
        try:
            result = w.orchestrator.run_once()
            self.assertFalse(result.continue_gate.allowed)
            self.assertTrue(any("is unverified" in b for b in result.continue_gate.blockers), result.continue_gate.blockers)
            required = [n for n in w.orchestrator.notifier.history if n.kind is NotificationKind.USER_ACTION_REQUIRED]
            self.assertEqual(len(required), 1, [n.detail for n in required])
            self.assertEqual(required[0].title, f"The club needs you: {CALENDAR_BLOCKED_SUBJECT}")
            self.assertEqual(required[0].detail, "; ".join(result.continue_gate.blockers), "the operator is told exactly what the deciding gate says")
        finally:
            w.close()

    def test_cal01_mandatory_inbox_work_is_reported_before_the_optional_plan_and_only_once(self):
        """CAL 01 / spec 12.4: the mandatory items level 2 establishes on its own are notified before the optional plan runs, and the
        gate that decides afterwards repeats none of it (the notifier suppresses the identical event for the same subject)."""
        w = World()
        try:
            result = w.orchestrator.run_once()
            entries = w.store.journal_entries(limit=100_000)
            required = [n for n in w.orchestrator.notifier.history if n.kind is NotificationKind.USER_ACTION_REQUIRED]
            self.assertEqual(len(required), 1, [n.detail for n in required])
            first = next(e["seq"] for e in entries if e["kind"] == "notification" and e["body"]["kind"] == NotificationKind.USER_ACTION_REQUIRED.value)
            self.assertLess(first, next(e["seq"] for e in entries if e["kind"] == JOURNAL_PLAN), "mandatory work is reported before the optional plan")
            self.assertEqual(required[0].detail, "; ".join(result.continue_gate.blockers), "worded as the deciding gate words it")
        finally:
            w.close()


class PendingActionsObservationTests(unittest.TestCase):
    """CAL 01 / spec 12.4: a read message that looks like a decision keeps blocking until a pending-actions observation proves it answered."""

    READ_DEADLINE = {"id": 504, "date": "2024-02-16", "time": "11:00", "unread": False, "event_type": "news_item_registration_deadline", "sender_id": 95, "sender_name": "Director of Football", "subject": None, "body": None, "text_status": "not_decoded", "time_status": "current"}

    def _world(self, *, observation: PendingActionsObservation | None = None, supported: bool = True, declared: bool = False) -> World:
        inbox = fx.inbox_payload()
        inbox["messages"].append(dict(self.READ_DEADLINE))
        provisions = {"inbox_text": "test-operator"}
        if supported:
            provisions["pending_actions"] = "test-operator"
        provider = None if declared or observation is None else (lambda: observation)
        w = World(routes={"/inbox": env(inbox)}, provisions=provisions, inbox_text_provider=answered_inbox(501, 503), pending_actions_provider=provider)
        w.orchestrator.validate_capabilities(w.client.connection_state()["status"])      # as a pass does on connect
        if declared and observation is not None:
            w.orchestrator.declare_pending_actions(observation)
        return w

    def _gate(self, w: World):
        snapshot = w.orchestrator.collect("test")
        point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
        return snapshot, point, point.gate

    def test_cal01_a_current_observation_stops_a_read_decision_blocking_the_calendar(self):
        """CAL 01: with the ``pending_actions`` capability supported and an observation at the snapshot's own game date and time
        naming the message, the read deadline message is resolved and the calendar may move on; the resolution is journaled with
        the source that reported it."""
        observation = PendingActionsObservation(frozenset({"inbox:504"}), "test-operator", fx.GAME_DATE, fx.GAME_TIME)
        w = self._world(observation=observation, declared=True)
        try:
            snapshot, point, gate = self._gate(w)
            resolved = next(p for p in point.pending if p.action_id == "inbox:504")
            self.assertTrue(resolved.resolved)
            self.assertFalse(resolved.blocks_continue)
            self.assertIn("reported resolved by test-operator", resolved.description)
            self.assertNotIn("inbox:504", "; ".join(gate.blockers))
            self.assertTrue(gate.allowed, gate.to_json())
            self.assertEqual(w.store.journal_entries(kind=JOURNAL_PENDING_ACTIONS)[-1]["body"]["resolved_action_ids"], ["inbox:504"])
        finally:
            w.close()

    def test_cal01_both_halves_of_the_decision_point_agree_about_a_resolved_message(self):
        """CAL 01 / spec 12.4: the inbox blockers and the Continue gate judge a read message by the same evidence, so a resolved
        message does not clear the gate while still standing as an inbox blocker that refuses progression."""
        observation = PendingActionsObservation(frozenset({"inbox:504"}), "test-operator", fx.GAME_DATE, fx.GAME_TIME)
        w = self._world(observation=observation, declared=True)
        try:
            snapshot, point, gate = self._gate(w)
            self.assertTrue(gate.allowed, gate.to_json())
            self.assertEqual([b.item.message_id for b in point.blockers], [])
            self.assertTrue(point.mandatory_clear)
            self.assertFalse(continue_blocked_by_inbox(point.blockers).blocked)
        finally:
            w.close()
        # Without the capability the same observation proves nothing: the blocker stands and both halves refuse.
        w = self._world(observation=observation, declared=True, supported=False)
        try:
            _, point, gate = self._gate(w)
            self.assertFalse(gate.allowed)
            self.assertIn(504, [b.item.message_id for b in point.blockers])
            self.assertFalse(point.mandatory_clear)
        finally:
            w.close()

    def test_cal01_a_stale_observation_or_a_missing_capability_keeps_it_blocking(self):
        """CAL 01 / spec 5.2: in-game time is the only clock, so an observation from another game time proves nothing about this
        decision point; and without the ``pending_actions`` capability no observation counts at all. Both keep the read message
        blocking, and the gate says why."""
        stale = PendingActionsObservation(frozenset({"inbox:504"}), "test-operator", "2024-02-16", "09:00")
        w = self._world(observation=stale)
        try:
            _, point, gate = self._gate(w)
            action = next(p for p in point.pending if p.action_id == "inbox:504")
            self.assertEqual(action.classification, CLASSIFICATION_UNRESOLVED_READ)
            self.assertFalse(action.resolved)
            self.assertIn("inbox:504", "; ".join(gate.blockers))
            self.assertFalse(gate.allowed)
            self.assertTrue(any("is not the snapshot game time" in note for note in gate.notes), gate.notes)
            self.assertTrue(any("is not the snapshot game time" in note for note in point.notes), point.notes)
        finally:
            w.close()
        current = PendingActionsObservation(frozenset({"inbox:504"}), "test-operator", fx.GAME_DATE, fx.GAME_TIME)
        w = self._world(observation=current, supported=False)
        try:
            _, point, gate = self._gate(w)
            self.assertFalse(next(p for p in point.pending if p.action_id == "inbox:504").resolved)
            self.assertIn("inbox:504", "; ".join(gate.blockers))
            self.assertFalse(gate.allowed)
            self.assertTrue(any("pending_actions capability" in note for note in gate.notes), gate.notes)
        finally:
            w.close()


class InboxAnswerTests(unittest.TestCase):
    """AUD 01 / spec 11.3, 4.3: an inbox answer is one of the options the game showed, ranked from observed evidence and recorded."""

    OFFER_OPTIONS = (
        DialogueOption("keep", "Reject the offer and keep him", "He stays at the club."),
        DialogueOption("delegate", "Let my assistant handle it", "The assistant answers."),
        DialogueOption("write", "Write your own reply", kind="free_text"),
    )

    def _world(self, *, language_model=None, options=OFFER_OPTIONS, adapter=None, workflows=None) -> World:
        provider = answered_inbox(503)
        provider.declare(InboxText(501, "Derby offer for Sam Wing", "Derby have offered GBP 450,000.", options, None, True, True), source="test-operator", observed_at="2026-01-01T00:00:00+00:00", game_time=CURRENT_GAME_TIME)
        return World(linked=True, adapter=adapter, authority="scoped", families=["inbox"], provisions={"pending_actions": "test-operator", "inbox_text": "test-operator"}, inbox_text_provider=provider, language_model=language_model, workflows=workflows)

    def _inbox_observation_id(self, w: World) -> str:
        return w.store.list_observations(branch_id=w.branch.branch_id, source="bridge:/inbox", limit=1000)[-1].observation_id

    def test_aud01_the_proposal_carries_the_chosen_legal_option_its_evidence_and_the_policy_version(self):
        """AUD 01 / spec 11.3: the club policy ranks the options the game showed and the proposal carries the chosen legal option id,
        the cited observation ids and the policy version; the ``dialogue.inbox`` decision records the same, so an answer resolves
        back to its inputs. Free-text boxes are excluded: the bot never composes prose."""
        w = self._world()
        try:
            snapshot = w.orchestrator.collect("test")
            point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
            self.assertEqual(len(point.proposals), 1, point.proposals)
            parameters = point.proposals[0]["parameters"]
            self.assertEqual(parameters["legal_option_ids"], ["keep", "delegate"], "only the fixed options the game offered are legal")
            self.assertEqual(parameters["option_id"], "keep", "the policy disfavours delegating; both are legal")
            self.assertIn(parameters["option_id"], parameters["legal_option_ids"])
            self.assertEqual(parameters["cited_observation_ids"], [self._inbox_observation_id(w)])
            self.assertTrue(parameters["choice_policy_version"].startswith("choice-policy/"))
            self.assertIn(f"settings:authority_profile/{w.settings.authority_profile_version}", parameters["choice_policy_version"])
            decision = w.store.get_decision(parameters["dialogue_decision_id"])
            self.assertIsNotNone(decision, "the dialogue decision is recorded before anything is minted")
            self.assertEqual(decision.kind, "dialogue.inbox")
            self.assertEqual(decision.selected["option_id"], "keep")
            self.assertEqual(decision.constraints, [{"legal_option_ids": ["keep", "delegate"]}])
            self.assertEqual(decision.components["cited_observation_ids"], parameters["cited_observation_ids"])
            self.assertEqual(decision.model_versions["settings:authority_profile"], str(w.settings.authority_profile_version))
            self.assertEqual(point.proposals[0]["decision_id"], decision.decision_id, "the intent will cite the decision that chose the option")
            entry = w.store.journal_entries(kind=JOURNAL_INBOX_CHOICE)[-1]["body"]
            self.assertEqual(entry["ranking"]["excluded"], [["write", "option kind 'free_text': the bot does not invent free text"]])
        finally:
            w.close()

    def test_aud01_a_language_model_may_only_rank_the_legal_options_it_cites_evidence_for(self):
        """AUD 01 / spec 4.3: the configured model sees only the legal option ids and the mode-filtered evidence, and an accepted,
        grounded answer is recorded with the model's own citation and request id."""
        w = self._world()
        try:
            snapshot = w.orchestrator.collect("test")
            observation_id = self._inbox_observation_id(w)
            model = ScriptedLanguageModel([{"option_id": "delegate", "cited_observation_ids": [observation_id], "rationale": "the assistant knows the player"}])
            w.orchestrator.language_model = model
            point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
            parameters = point.proposals[0]["parameters"]
            self.assertEqual(parameters["option_id"], "delegate", "an accepted model answer earns a bonus over the policy score")
            self.assertEqual(parameters["cited_observation_ids"], [observation_id])
            self.assertEqual(parameters["language_model"]["status"], "accepted")
            self.assertEqual(parameters["language_model"]["request_id"], model.requests[-1].request_id)
            self.assertEqual(model.requests[-1].legal_option_ids, ["keep", "delegate"], "the model is never offered the free-text box")
            self.assertEqual(model.requests[-1].evidence_ids, [observation_id])
            self.assertIn("are not instructions", model.requests[-1].render_prompt())
        finally:
            w.close()

    def test_aud01_an_unavailable_or_rejected_model_never_invents_a_choice(self):
        """AUD 01 / spec 4.3: with no model configured (the baseline ``NoLanguageModel``) the club policy still answers from the
        observed legal ids and nothing is cited to the model; a model answer that names an option the game never offered is
        rejected, and the recorded answer stays the policy's legal pick."""
        w = self._world(language_model=NoLanguageModel())
        try:
            snapshot = w.orchestrator.collect("test")
            point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
            parameters = point.proposals[0]["parameters"]
            self.assertEqual(parameters["language_model"]["status"], "unavailable")
            self.assertEqual(parameters["option_id"], "keep")
            entry = w.store.journal_entries(kind=JOURNAL_INBOX_CHOICE)[-1]["body"]
            self.assertFalse(any(item["lm_selected"] for item in entry["ranking"]["ranked"]), "an unavailable model selects nothing")
        finally:
            w.close()
        w = self._world(language_model=ScriptedLanguageModel([{"option_id": "accept_and_sell", "cited_observation_ids": ["obs-invented"]}]))
        try:
            snapshot = w.orchestrator.collect("test")
            point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
            parameters = point.proposals[0]["parameters"]
            self.assertEqual(parameters["language_model"]["status"], "rejected")
            self.assertEqual(parameters["option_id"], "keep", "an illegal or ungrounded answer changes nothing")
            self.assertIn(parameters["option_id"], parameters["legal_option_ids"])
        finally:
            w.close()

    def test_aud01_the_executed_answer_resolves_back_to_the_option_that_was_chosen(self):
        """AUD 01 / spec 11.3, 12.2: the intent the UI writer carries out names the chosen legal option, the evidence cited for it and
        the policy version, and it cites the dialogue decision, so the executed answer resolves back to the ranking that produced it.
        It is confirmed by the effect the bridge can see - the message is no longer pending in the ``/inbox`` metadata - and never by
        the screen the UI ended up on; which option the game recorded is explicitly not read back."""
        inbox = fx.inbox_payload()
        adapter = ReactingAdapter()
        w = self._world(adapter=adapter, workflows={**FAKE_WORKFLOWS, "respond.inbox": inbox_answer_workflow()})
        w.transport.set("/inbox", lambda: env(inbox))

        def answered() -> None:
            for message in inbox["messages"]:
                if message["id"] == 501:
                    message["unread"] = False
            inbox["unread_count"] = sum(1 for m in inbox["messages"] if m["unread"])
        adapter.reactions[ANSWER_STEP] = answered
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.status, PassStatus.ACTED, result.notes)
            self.assertIs(result.executed.state, ActionState.CONFIRMED, result.executed.reason)
            self.assertEqual(adapter.performed, [ANSWER_STEP], "one input; nothing retried")
            intent = w.store.get_intent(result.executed.action_id)
            self.assertEqual(intent.verification, "inbox_message_answered")
            self.assertNotIn("target", intent.parameters)
            self.assertNotIn("expected_screen", intent.parameters)
            effect = result.executed.verdict.details["effect"]
            self.assertEqual((effect["message_id"], effect["option_id_sent"], effect["pending"], effect["unread"]), (501, "keep", False, False))
            self.assertIn("cannot be read back", effect["option_readback"])
            self.assertTrue(all(r["source"].startswith("bridge:/inbox@") for r in result.executed.verdict.readbacks), result.executed.verdict.readbacks)
            self.assertEqual(intent.kind, "respond.inbox")
            self.assertEqual(intent.parameters["option_id"], "keep")
            self.assertEqual(intent.parameters["legal_option_ids"], ["keep", "delegate"])
            cited = intent.parameters["cited_observation_ids"]
            self.assertEqual([w.store.get_observation(o, with_payload=False).source for o in cited], ["bridge:/inbox"])
            self.assertTrue(set(cited) <= set(w.store.get_snapshot(intent.decision_snapshot_id).observation_ids), "the cited evidence belongs to the snapshot the answer was chosen on")
            explanation = explain_action(intent.action_id, w.store)
            self.assertEqual(explanation.decision.kind, "dialogue.inbox")
            self.assertEqual(explanation.decision.decision_id, intent.parameters["dialogue_decision_id"])
            self.assertIn("Reject the offer and keep him", render_action(explanation))
            self.assertTrue(explanation.before_evidence, "the answer resolves to the observations it was chosen from")
        finally:
            w.close()

    def test_aud01_no_answer_is_proposed_when_the_options_cannot_be_read_or_understood(self):
        """AUD 01 / spec 11.3: without observed text there are no option ids to choose from, so nothing is proposed and the blocker
        keeps naming ``inbox_text``; when the visible words of every option mean nothing to the policy the bot says so (the refusal
        is journaled with the legal ids) instead of guessing one."""
        w = World(provisions={"pending_actions": "test-operator"})        # no inbox text provider at all
        try:
            snapshot = w.orchestrator.collect("test")
            point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
            self.assertEqual(point.proposals, [])
            self.assertEqual(w.store.journal_entries(kind=JOURNAL_INBOX_CHOICE), [], "no choice is even ranked without text")
            self.assertTrue(any("inbox_text" in b.report.missing for b in point.blockers))
        finally:
            w.close()
        opaque = (DialogueOption("option_a", "Mmm"), DialogueOption("option_b", "Hmm"))
        w = self._world(options=opaque)
        try:
            snapshot = w.orchestrator.collect("test")
            point = w.orchestrator.decision_point(snapshot, LineupStatus("not_required"))
            self.assertEqual(point.proposals, [], "an option whose meaning is unknown is never sent")
            refusal = w.store.journal_entries(kind=JOURNAL_NOT_EXECUTED)[-1]["body"]
            self.assertEqual(refusal["kind"], "respond.inbox")
            self.assertEqual(refusal["legal_option_ids"], ["option_a", "option_b"])
            self.assertIn("what it would do is unknown", refusal["reason"])
            self.assertIsNotNone(w.store.get_decision(refusal["decision_id"]), "the refusal to answer is recorded too")
            self.assertEqual(w.store.list_intents(branch_id=w.branch.branch_id), [])
        finally:
            w.close()

# ---------------------------------------------------------------------------
# level 4: supported match
# ---------------------------------------------------------------------------


def match_payload(timeline: str | None = "unclassified", *, with_timeline: bool = True) -> dict[str, Any]:
    match = {"home": {"club_id": 742}, "away": {"club_id": 804}, "clock": "45:00", "score": {"home": 0, "away": 0}}
    if with_timeline:
        match["timeline"] = timeline
    return {"available": True, "reason": None, "match": match}


class MatchLevelTests(unittest.TestCase):
    """MAT 01: live decisions are gated on the ``/match`` payload's own timeline classification and the event-order capability."""

    def _observe(self, match: dict[str, Any], **world_kw):
        planner = stub_planner([stub_candidate()])
        w = World(linked=True, authority="scoped", families=["tactics"], planner=planner, routes={"/match": env(match)}, **world_kw)
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.level, RunLevel.MATCH)
            self.assertIs(result.status, PassStatus.MATCH_OBSERVED)
            self.assertIsNone(result.executed)
            self.assertEqual(planner.calls, [], "no optimisation during a match")
            self.assertEqual(w.adapter.inputs, [], "nothing is ever sent from the match level in this version")
            self.assertEqual(result.next_poll_seconds, 2.0, "paused-match engineering default")
            self.assertIn(JOURNAL_MATCH, w.journal_kinds())
            return result, w.store.journal_entries(kind=JOURNAL_MATCH)[-1]["body"]
        finally:
            w.close()

    def test_mat01_live_match_only_records_observations(self):
        """MAT 01: an ``unclassified`` timeline refuses every live action; the journal carries the timeline value read from the
        payload and the refusal names it, so retained statistics cannot trigger anything."""
        result, entry = self._observe(match_payload("unclassified"))
        self.assertFalse(entry["live_actions_allowed"])
        self.assertEqual(entry["timeline"], "unclassified")
        self.assertEqual(entry["timeline_status"], "reported")
        self.assertIn("'unclassified'", entry["reason"])
        self.assertIn("'unclassified'", result.notes[0])
        self.assertIn("MAT 01", entry["reason"])

    def test_mat01_gate_reads_the_payload_timeline_value_not_a_constant(self):
        """MAT 01: whatever the payload says is what is journaled and refused on; a payload with no timeline field is journaled as
        missing (never as a classification) and refused."""
        _, entry = self._observe(match_payload("partially_classified"))
        self.assertFalse(entry["live_actions_allowed"])
        self.assertEqual(entry["timeline"], "partially_classified")
        self.assertIn("'partially_classified'", entry["reason"])
        result, entry = self._observe(match_payload(with_timeline=False))
        self.assertFalse(entry["live_actions_allowed"])
        self.assertIsNone(entry["timeline"])
        self.assertEqual(entry["timeline_status"], "missing")
        self.assertIn("timeline missing", entry["reason"])
        self.assertIn("timeline missing", result.notes[0])

    def test_mat01_classified_timeline_without_the_event_order_capability_still_refuses(self):
        """MAT 01: ``classified`` alone is not enough; with ``match_event_order`` unresolved there is no verified intervention point,
        so live actions are refused and the refusal names the capability status."""
        _, entry = self._observe(match_payload("classified"))
        self.assertFalse(entry["live_actions_allowed"])
        self.assertEqual(entry["timeline"], "classified")
        self.assertIn(entry["match_event_order"], ("missing", "unsupported"))
        self.assertIn("match_event_order", entry["reason"])
        self.assertIn(entry["match_event_order"], entry["reason"])

    def test_mat01_classified_timeline_with_supported_event_order_is_the_only_permitting_combination(self):
        """MAT 01: only a ``classified`` timeline together with a supported ``match_event_order`` could ever permit a live action; even
        then nothing is executed here because no prevalidated match action set exists in this version."""
        result, entry = self._observe(match_payload("classified"), provisions={"match_event_order": "test-operator"})
        self.assertTrue(entry["live_actions_allowed"])
        self.assertEqual((entry["timeline"], entry["match_event_order"]), ("classified", "supported"))
        self.assertIn("nothing is executed", entry["reason"])
        self.assertIn("nothing is executed", result.notes[0])


if __name__ == "__main__":
    unittest.main()
