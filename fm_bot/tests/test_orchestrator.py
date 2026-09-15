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
from ..interactions.inbox import DeclaredInboxTextProvider, InboxText
from ..interface.controls import Settings
from ..interface.explain import explain_action
from ..interface.notify import NotificationKind
from ..orchestrator import (
    JOURNAL_BOUNDARY, JOURNAL_MATCH, JOURNAL_PLAN, JOURNAL_UNAVAILABLE, MANAGER_LOCK, FakeClock, ManagerLockHeld, Orchestrator, PassStatus, RunLevel, Trigger,
    detect_triggers, lineup_status_of, next_action_of, plan_due, plan_outcome_of,
)
from ..rules.capabilities import CapabilityRegistry
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import ActionState, ConsistencyStatus, Decision, DecisionSnapshot
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


class World:
    """A registered career, a scripted bridge, a fake UI and an orchestrator with a fake clock."""

    def __init__(self, *, routes: dict[str, Any] | None = None, linked: bool = False, lineage_confirmed: bool = True, authority: str | None = None, families: list[str] | None = None, allow_continue: bool = False, planner=None, provisions: dict[str, str] | None = None, inbox_text_provider=None, workflows: dict[str, Workflow] | None = None, club_id: int = 742, adapter: FakeAdapter | None = None):
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
        self.orchestrator = Orchestrator(self.store, self.client, self.adapter, self.settings, career=self.career, branch=self.branch, lineage_confirmed=lineage_confirmed, clock=self.clock, sleep=self.clock.advance, planner=planner, provisions=provisions, inbox_text_provider=inbox_text_provider, workflows=workflows)

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

    def test_continue_runs_after_the_gate_clears_then_settles_and_recollects(self):
        provider = DeclaredInboxTextProvider()
        for message_id in (501, 503):
            provider.declare(InboxText(message_id, "informational", "nothing to answer", (), None, False, False), source="test-operator", observed_at="2026-01-01T00:00:00+00:00", game_time=None)
        continue_workflow = Workflow("fake.continue", "1", "progress.continue", FAKE_SCREEN_MODEL, (UIStep("go", "navigate", ANY_SCREEN, {"target": "home"}, RISK_NAVIGATION, "home"),), None)

        class WithTarget(Orchestrator):
            def continue_action(self, gate):
                action = super().continue_action(gate)
                action["parameters"]["target"] = "home"       # the fake UI's home screen stands in for the next stable point
                return action

        w = World(linked=True, authority="scoped", families=["progression"], allow_continue=True, provisions={"pending_actions": "test-operator", "inbox_text": "test-operator"}, inbox_text_provider=provider, workflows={**FAKE_WORKFLOWS, "progress.continue": continue_workflow})
        w.orchestrator.close()
        w.orchestrator = WithTarget(w.store, w.client, w.adapter, w.settings, career=w.career, branch=w.branch, lineage_confirmed=True, clock=w.clock, sleep=w.clock.advance, provisions={"pending_actions": "test-operator", "inbox_text": "test-operator"}, inbox_text_provider=provider, workflows={**FAKE_WORKFLOWS, "progress.continue": continue_workflow})
        try:
            result = w.orchestrator.run_once()
            self.assertTrue(result.continue_gate.allowed, result.continue_gate.to_json())
            self.assertIn(JOURNAL_BOUNDARY, w.journal_kinds(), "the expected boundary is recorded before Continue")
            self.assertIs(result.status, PassStatus.ACTED, result.notes)
            self.assertIs(result.executed.state, ActionState.CONFIRMED)
            labels = [e["body"]["requirements"]["label"] for e in w.store.journal_entries(kind="snapshot", limit=1000)]
            self.assertIn("post-progression", labels, "after progression the bot settles and collects again")
            self.assertGreaterEqual(w.clock.sleeps.count(1.0), 2, "settled twice: on connect and after progression")
        finally:
            w.close()


# ---------------------------------------------------------------------------
# level 4: supported match
# ---------------------------------------------------------------------------


class MatchLevelTests(unittest.TestCase):
    def test_mat01_live_match_only_records_observations(self):
        match = {"available": True, "reason": None, "match": {"home": {"club_id": 742}, "away": {"club_id": 804}, "clock": "45:00", "score": {"home": 0, "away": 0}, "timeline": "unclassified"}}
        planner = stub_planner([stub_candidate()])
        w = World(linked=True, authority="scoped", families=["tactics"], planner=planner, routes={"/match": env(match)})
        try:
            result = w.orchestrator.run_once()
            self.assertIs(result.level, RunLevel.MATCH)
            self.assertIs(result.status, PassStatus.MATCH_OBSERVED)
            self.assertIsNone(result.executed)
            self.assertEqual(planner.calls, [], "no optimisation during a match")
            self.assertEqual(w.adapter.inputs, [])
            self.assertIn(JOURNAL_MATCH, w.journal_kinds())
            entry = w.store.journal_entries(kind=JOURNAL_MATCH)[-1]["body"]
            self.assertFalse(entry["live_actions_allowed"])
            self.assertEqual(entry["timeline"], "unclassified")
            self.assertEqual(result.next_poll_seconds, 2.0, "paused-match engineering default")
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
