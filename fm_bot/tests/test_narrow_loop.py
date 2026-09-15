"""The narrow loop the specification asks to prove first (16.3), end to end and offline.

Identify the career, collect stable state, propose one supported change,
authorise it, apply it through the single UI writer, verify it by independent
readback, and recover from an injected interruption (REC 01, ACT 01, ACT 02,
ACT 03, AUD 01). Everything is a fake: the scripted fixture bridge, the
in-memory fake FM front end (:class:`FakeAdapter`) and a fake clock. The
store is a real SQLite file in a temporary directory so that a "second
process" opens its own connection over the same file exactly as a restarted
bot would.

The tactic catalog here is a *test fixture*. A real catalog entry must map to
observed settings and a tested UI workflow (spec 17.2); nothing in the bridge
exposes one yet. The proposal step is therefore a small catalog-driven helper
standing in for the planner, which has no tactic catalog to draw from.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from os import path
from types import SimpleNamespace
from typing import Any

from ..bridge_client.client import BridgeClient
from ..bridge_client.transport import FakeTransport
from ..execution.adapter import FAKE_WORKFLOWS, FakeAdapter
from ..execution.executor import ExecutionReport, SingleWriterExecutor, WriterLockHeld
from ..execution.lifecycle import IntentFactory, enqueue, validate
from ..execution.reconciliation import reconcile_uncertain
from ..interface.controls import Settings
from ..interface.explain import explain_action
from ..interface.notify import NotificationKind
from ..orchestrator import FakeClock, Orchestrator, PassStatus
from ..rules.authority import AuthorityMode
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import ActionState, DecisionSnapshot
from ..state.status import ValueStatus
from ..state.store import Store, StoreError
from ..state.views import tactic_view
from . import fixtures as fx
from .execution_fixtures import linked_world

# Test-only catalog: id -> the name the game stores. Mirrors the FakeAdapter default so the
# bridge's ``/tactics`` stored_name (linked world) corroborates the UI readback.
TACTIC_CATALOG: dict[str, str] = {"balanced-01": "4-4-2 Balanced", "counter-02": "4-2-3-1 Counter"}
CATALOG_VERSION = 1
TRAINING_CHANGE = {"intensity": "Double", "rest_percent": 20}


class ProcessDied(Exception):
    """Stands in for the executor process being killed mid-step (kill -9, power loss)."""


class DyingAdapter(FakeAdapter):
    """A fake FM front end whose *bot process* dies right after the n-th input reached the game.

    The input lands (the game state changes) but the process never records
    the step result, never transitions to VERIFYING and never releases its
    locks. That is the interruption REC 01 must survive.
    """

    def __init__(self, *, die_on_call: int, **kw):
        super().__init__(**kw)
        self.die_on_call = die_on_call
        self.died = False

    def perform(self, step):
        result = super().perform(step)
        if self.calls == self.die_on_call and not self.died:
            self.died = True
            raise ProcessDied(f"bot process killed after input {self.calls} ({step.step_id}) reached the game")
        return result


def propose_tactic_change(snapshot: DecisionSnapshot) -> dict[str, Any]:
    """Propose switching to the catalog entry that is not currently selected (spec 17.2 shape).

    The current tactic is read from the snapshot's ``/tactics`` stored name
    and mapped back to a catalog entry; the proposal names the other entry.
    A stored tactic outside the catalog is refused rather than guessed.
    """
    view = tactic_view(snapshot)
    if view["status"] != "available":
        raise LookupError(f"tactic not observed: {view['status']}")
    by_name = {name: tactic_id for tactic_id, name in TACTIC_CATALOG.items()}
    current = by_name.get(view["name"])
    if current is None:
        raise LookupError(f"stored tactic {view['name']!r} is not a validated catalog entry")
    target = next(tactic_id for tactic_id in TACTIC_CATALOG if tactic_id != current)
    return {
        "kind": "select_validated_tactic",
        "authority_scope": "tactics.select",
        "targets": {"routes": ["/tactics"]},
        "parameters": {"tactic_catalog_id": target, "catalog_version": CATALOG_VERSION, "catalog_name": TACTIC_CATALOG[target], "previous_tactic_catalog_id": current},
        "required_capabilities": ["tactic_catalog_readback", "tactic_selection_ui"],
        "verification": "selected_tactic_matches_catalog",
        "description": f"switch from {TACTIC_CATALOG[current]} to {TACTIC_CATALOG[target]} (catalog v{CATALOG_VERSION})",
    }


def propose_training_change(adapter: FakeAdapter) -> dict[str, Any]:
    """Propose a training intensity change; the previous settings come from the UI readback, not a guess."""
    previous = adapter.readback("training_settings").require()
    return {
        "kind": "set.training",
        "authority_scope": "training.set",
        "targets": {"routes": ["/training"]},
        "parameters": {"settings": dict(TRAINING_CHANGE), "previous_settings": dict(previous)},
        "required_capabilities": [],
        "verification": "training_settings_reread",
        "description": f"training intensity {previous.get('intensity')} -> {TRAINING_CHANGE['intensity']} with {TRAINING_CHANGE['rest_percent']}% rest",
    }


def catalog_planner(adapter: FakeAdapter):
    """A stand-in for ``planning.planner.plan_once`` that proposes the catalog tactic change (test double, not a planner)."""

    def plan_once(snapshot, **kwargs):
        action = propose_tactic_change(snapshot)
        candidate = SimpleNamespace(candidate_id="tactic:catalog", kind=action["kind"], horizon="next_decision", description=action["description"], status="proposed", payload={}, reasons=[], requires=action["required_capabilities"], unverified=False, submittable=True, targets=action["targets"], parameters=action["parameters"])
        return SimpleNamespace(status="planned", candidates=[candidate], decisions=[], summaries=[action["description"]], capability_reports={}, reasons=[])
    return plan_once


class Process:
    """One bot process over an on-disk store: registry, settings, bridge client, orchestrator.

    ``register=True`` registers the career (first run); a later process
    identifies the career from the registry instead. ``lock_stale_seconds``
    is the takeover policy for locks left by a dead process.
    """

    def __init__(self, db_path: str, adapter: FakeAdapter, *, register: bool = False, families: tuple[str, ...] = ("tactics",), lock_stale_seconds: float = 300.0, planner=None, provisions: dict[str, str] | None = None):
        self.store = Store(db_path)
        registry = CareerRegistry(self.store)
        if register:
            self.career, self.branch, self.checkpoint = registry.register_career("Wycombe main", SaveManifest(fx.BUILD, fx.MANAGER["id"], fx.CLUB["id"], fx.GAME_DATE, fx.GAME_TIME))
        else:
            careers = self.store.list_careers()
            assert len(careers) == 1, "exactly one registered career expected on restart"
            self.career = careers[0]
            self.branch = self.store.list_branches(self.career.career_id)[0]
            self.checkpoint = self.store.list_checkpoints(self.branch.branch_id)[0]
        self.adapter = adapter
        self.client = BridgeClient(FakeTransport(linked_world(adapter)), self.store, context={"career_id": self.career.career_id, "branch_id": self.branch.branch_id})
        self.settings = Settings.load(self.store)
        if register:
            self.settings.save()
            self.settings.change("authority_mode", "scoped", reason="narrow loop")
            self.settings.change("action_families", list(families), reason="narrow loop")
        self.clock = FakeClock()
        self.orchestrator = Orchestrator(self.store, self.client, adapter, self.settings, career=self.career, branch=self.branch, lineage_confirmed=True, clock=self.clock, sleep=self.clock.advance, planner=planner, provisions=provisions, lock_stale_seconds=lock_stale_seconds)
        self.dead = False

    def die(self) -> None:
        """The process is gone: nothing is closed, no lock is released, the connection simply ends."""
        self.dead = True
        self.store.close()

    def close(self) -> None:
        if not self.dead:
            self.orchestrator.close()
            self.store.close()
            self.dead = True

    def transitions(self, action_id: str) -> list[str]:
        return [e["body"]["to"] for e in self.store.journal_entries("intent.transition", action_id)]

    def observation_exists(self, observation_id: str) -> bool:
        return self.store.get_observation(observation_id, with_payload=False) is not None


class NarrowLoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fm_bot_narrow_loop_")
        self.db_path = path.join(self.tmp, "fm_bot.sqlite3")
        self.processes: list[Process] = []

    def tearDown(self):
        for proc in self.processes:
            proc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, adapter: FakeAdapter, **kw) -> Process:
        proc = Process(self.db_path, adapter, **kw)
        self.processes.append(proc)
        return proc

    def connected(self, adapter: FakeAdapter, **kw) -> tuple[Process, DecisionSnapshot]:
        """A registered, connected process at a stable decision point."""
        proc = self.start(adapter, register=True, **kw)
        connection = proc.orchestrator.connect()
        self.assertTrue(connection.ok, connection.problems)
        snapshot = proc.orchestrator.collect("decision-point")
        self.assertTrue(snapshot.valid, snapshot.consistency_reasons)
        return proc, snapshot


class HappyPathTests(NarrowLoopCase):
    def test_identify_collect_propose_authorise_apply_verify(self):
        """The narrow loop in one pass: registered career identified and continuous, stable snapshot, a catalog tactic proposed,
        authorised under a scoped profile with the tactics family, applied by the single writer and confirmed by independent
        readback that the bridge corroborates (AUD 01: the action explains itself down to the evidence)."""
        adapter = FakeAdapter()
        proc = self.start(adapter, register=True)
        connection = proc.orchestrator.connect()
        # identify: manifest match against the registered career, clock settled, nothing left in flight
        self.assertTrue(connection.ok, connection.problems)
        self.assertIs(connection.career_matched, True)
        self.assertTrue(connection.settle.settled)
        self.assertEqual(connection.reconciled, [])
        self.assertEqual(proc.checkpoint.manifest.manager_id, fx.MANAGER["id"])
        # collect: a consistent snapshot on a continuous lineage
        snapshot = proc.orchestrator.collect("decision-point")
        self.assertTrue(snapshot.valid, snapshot.consistency_reasons)
        self.assertEqual(snapshot.continuity["status"], "continuous")
        self.assertEqual((snapshot.career_id, snapshot.branch_id), (proc.career.career_id, proc.branch.branch_id))
        self.assertEqual(snapshot.game_date, fx.GAME_DATE)
        # propose: the entry that is not currently selected
        action = propose_tactic_change(snapshot)
        self.assertEqual(action["parameters"], {"tactic_catalog_id": "counter-02", "catalog_version": CATALOG_VERSION, "catalog_name": "4-2-3-1 Counter", "previous_tactic_catalog_id": "balanced-01"})
        self.assertIs(proc.settings.authority_profile().mode, AuthorityMode.SCOPED_EXECUTION)
        self.assertIn("tactics", proc.settings.authority_profile().enabled_families)
        # authorise + apply + verify
        report = proc.orchestrator.execute(action, snapshot)
        self.assertIsInstance(report, ExecutionReport, report)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertEqual(proc.transitions(report.action_id), ["VALIDATED", "QUEUED", "EXECUTING", "VERIFYING", "CONFIRMED"])
        self.assertEqual(adapter.selected_tactic_id, "counter-02")
        self.assertEqual([i["action"] for i in adapter.inputs], ["navigate", "select_tactic"])
        effect = report.verdict.details["effect"]
        self.assertEqual(effect["tactic_id"], "counter-02")
        self.assertIs(effect["bridge_corroborated"], True, "the bridge's stored tactic name agrees with the UI readback")
        # the game now reports the new tactic and a fresh proposal would go the other way
        after = proc.orchestrator.collect("after")
        self.assertEqual(tactic_view(after)["name"], "4-2-3-1 Counter")
        self.assertEqual(propose_tactic_change(after)["parameters"]["tactic_catalog_id"], "balanced-01")
        # AUD 01: inputs, limits, decision version and evidence all resolve
        explanation = explain_action(report.action_id, proc.store)
        self.assertEqual(explanation.state, "CONFIRMED")
        self.assertEqual(explanation.limits["authority_profile_version"], proc.settings.authority_profile_version)
        self.assertEqual(len(explanation.inputs_sent), 2)
        self.assertTrue(explanation.before_evidence and explanation.after_evidence)
        self.assertEqual(explanation.decision.snapshot_id, snapshot.snapshot_id)
        self.assertIn(NotificationKind.COMPLETED, [n.kind for n in proc.orchestrator.notifier.history])

    def test_training_intensity_change_is_applied_and_reread(self):
        """A second supported change: training intensity, verified by rereading the committed settings, under the training family."""
        adapter = FakeAdapter()
        proc, snapshot = self.connected(adapter, families=("tactics", "training"))
        action = propose_training_change(adapter)
        self.assertEqual(action["parameters"]["previous_settings"], {"intensity": "Normal"})
        report = proc.orchestrator.execute(action, snapshot)
        self.assertIsInstance(report, ExecutionReport, report)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        self.assertEqual(report.verdict.plan, "training_settings_reread")
        self.assertEqual(adapter.training_settings, TRAINING_CHANGE)
        self.assertEqual(adapter.readback("training_settings").require(), TRAINING_CHANGE)

    def test_whole_loop_through_run_once_with_a_catalog_proposer(self):
        """One orchestrator pass drives the same loop: connect, decision point, proposal (catalog stand-in), execution, verification."""
        adapter = FakeAdapter()
        proc = self.start(adapter, register=True, planner=catalog_planner(adapter))
        result = proc.orchestrator.run_once()
        self.assertIs(result.status, PassStatus.ACTED, result.notes)
        self.assertIs(result.executed.state, ActionState.CONFIRMED, result.executed.reason)
        self.assertEqual(result.next_action["kind"], "select_validated_tactic")
        self.assertEqual(adapter.selected_tactic_id, "counter-02")
        self.assertIsNotNone(result.decision_point, "mandatory work was assessed before the optional change")
        self.assertTrue(result.decision_point.pending, "the fixture inbox carries mandatory-looking messages; they are reported, and only Continue is gated by them")
        self.assertFalse(result.continue_gate.allowed, "the optional tactic change ran, but the calendar stays blocked until the inbox is resolved")
        explanation = explain_action(result.executed.action_id, proc.store)
        self.assertEqual(explanation.decision.snapshot_id, result.snapshot_id)

    def test_family_not_enabled_is_outside_scope_before_any_input(self):
        """The same proposal under a profile without the tactics family never reaches the UI."""
        adapter = FakeAdapter()
        proc, snapshot = self.connected(adapter, families=("training",))
        outcome = proc.orchestrator.execute(propose_tactic_change(snapshot), snapshot)
        self.assertIsInstance(outcome, str)
        self.assertIn("OUTSIDE_SCOPE", outcome)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(adapter.selected_tactic_id, "balanced-01")


class InterruptionTests(NarrowLoopCase):
    def test_rec01_act02_process_dies_after_executing_and_restart_reconciles_without_a_second_dispatch(self):
        """REC 01 / ACT 02: the bot process dies after EXECUTING is persisted and the select input has landed, before VERIFYING.
        A new process over the same store identifies the career again and, on connect, reconciles the in-flight intent to
        CONFIRMED from independent readback. No input is sent again, the intent is never reset to QUEUED, and the same decision
        cannot be dispatched twice."""
        adapter = DyingAdapter(die_on_call=2)          # call 1 navigates, call 2 selects the tactic and then the process dies
        first, snapshot = self.connected(adapter)
        action = propose_tactic_change(snapshot)
        with self.assertRaises(ProcessDied):
            first.orchestrator.execute(action, snapshot)
        intents = first.store.list_intents([ActionState.EXECUTING], branch_id=first.branch.branch_id)
        self.assertEqual(len(intents), 1)
        intent = intents[0]
        self.assertEqual(first.transitions(intent.action_id), ["VALIDATED", "QUEUED", "EXECUTING"])
        attempts = first.store.list_attempts(intent.action_id)
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(attempts[0]["outcome"], "the process died before any outcome was recorded")
        self.assertTrue(attempts[0]["before_evidence"])
        self.assertEqual(adapter.selected_tactic_id, "counter-02", "the input reached the game")
        self.assertEqual(len(adapter.inputs), 2)
        first.die()

        second = self.start(adapter, lock_stale_seconds=0)   # the dead process's manager-lock heartbeat is treated as stale
        self.assertEqual(second.career.career_id, first.career.career_id, "the career is identified from the registry, not re-registered")
        self.assertIs(second.settings.authority_mode(), AuthorityMode.SCOPED_EXECUTION, "the authority profile survived the restart")
        connection = second.orchestrator.connect()
        self.assertTrue(connection.ok, connection.problems)
        self.assertEqual(len(connection.reconciled), 1)
        decision = connection.reconciled[0]
        self.assertEqual((decision["action_id"], decision["previous_state"], decision["new_state"]), (intent.action_id, "EXECUTING", "CONFIRMED"))
        self.assertFalse(decision["requeued"])
        self.assertEqual(decision["verdict"]["kind"], "confirmed")
        self.assertIs(decision["verdict"]["details"]["effect"]["bridge_corroborated"], True)
        self.assertIs(second.store.get_intent(intent.action_id).state, ActionState.CONFIRMED)
        transitions = second.transitions(intent.action_id)
        self.assertEqual(transitions, ["VALIDATED", "QUEUED", "EXECUTING", "UNCERTAIN", "RECONCILING", "CONFIRMED"])
        self.assertNotIn("QUEUED", transitions[3:], "never silently reset to QUEUED")
        self.assertEqual(len(adapter.inputs), 2, "reconciliation sent no input; no second dispatch")
        self.assertEqual(adapter.calls, 2)
        attempt = second.store.list_attempts(intent.action_id)[0]
        self.assertEqual(attempt["outcome"], "confirmed")
        for observation_id in [*attempt["before_evidence"], *attempt["after_evidence"]]:
            self.assertTrue(second.observation_exists(observation_id), observation_id)
        # the same decision cannot be dispatched again: its idempotency key is taken
        with self.assertRaises(StoreError):
            IntentFactory(second.store).create(action["kind"], action["authority_scope"], second.orchestrator.collect("again"), action["targets"], action["parameters"], verification=action["verification"], decision_id=intent.decision_id)
        # a new UI writer refuses to start while the dead writer's lock is fresh, then finds nothing queued once it is stale
        with self.assertRaises(WriterLockHeld):
            SingleWriterExecutor(second.store, adapter, owner_id="exec-restart", registry=second.orchestrator.capabilities, profile=second.settings.authority_profile(), career_id=second.career.career_id, branch_id=second.branch.branch_id, workflows=FAKE_WORKFLOWS)
        executor = SingleWriterExecutor(second.store, adapter, owner_id="exec-restart", registry=second.orchestrator.capabilities, profile=second.settings.authority_profile(), career_id=second.career.career_id, branch_id=second.branch.branch_id, workflows=FAKE_WORKFLOWS, lock_stale_seconds=0)
        try:
            self.assertEqual(executor.load_queue(), [])
            self.assertIsNone(executor.run_next(lambda: second.orchestrator.collect("pre-execution")))
        finally:
            executor.close()
        self.assertEqual(adapter.calls, 2)
        explanation = explain_action(intent.action_id, second.store)
        self.assertEqual(explanation.state, "CONFIRMED")
        self.assertEqual([t["to"] for t in explanation.transitions], transitions)

    def test_act02_absent_effect_after_timeout_is_failed_with_evidence_and_never_retried(self):
        """ACT 02: the consequential select times out before any effect; the executor reports UNCERTAIN and never retries, and
        reconciliation proves the effect absent (FAILED) from readback without re-queueing. Before/after evidence is preserved."""
        adapter = FakeAdapter()
        adapter.inject("timeout_before_effect", on_step=2)
        proc, snapshot = self.connected(adapter)
        report = proc.orchestrator.execute(propose_tactic_change(snapshot), snapshot)
        self.assertIsInstance(report, ExecutionReport, report)
        self.assertIs(report.state, ActionState.UNCERTAIN, report.reason)
        self.assertIn("never retried", report.reason)
        self.assertEqual(adapter.calls, 2, "one navigation, one select; no retry of the consequential step")
        self.assertEqual([i["action"] for i in adapter.inputs], ["navigate"], "the select never went out")
        self.assertEqual(adapter.selected_tactic_id, "balanced-01")
        self.assertEqual(report.result.recovery_instruction.split(";")[0], "do not retry")
        self.assertIn(NotificationKind.FAILED, [n.kind for n in proc.orchestrator.notifier.history])
        intent = proc.store.get_intent(report.action_id)
        decision = reconcile_uncertain(proc.store, adapter, intent, proc.orchestrator.collect("reconcile"))
        self.assertIs(decision.new_state, ActionState.FAILED)
        self.assertIn("provably absent", decision.reason)
        self.assertIn("not re-queued", decision.reason)
        self.assertFalse(decision.requeued)
        self.assertEqual(adapter.calls, 2, "reconciliation sent nothing")
        self.assertEqual(proc.store.list_intents([ActionState.QUEUED], branch_id=proc.branch.branch_id), [])
        self.assertEqual(proc.transitions(intent.action_id), ["VALIDATED", "QUEUED", "EXECUTING", "UNCERTAIN", "RECONCILING", "FAILED"])
        attempt = proc.store.list_attempts(intent.action_id)[0]
        self.assertEqual(attempt["outcome"], "failed")
        self.assertTrue(attempt["before_evidence"] and attempt["after_evidence"])
        for observation_id in [*attempt["before_evidence"], *attempt["after_evidence"], *decision.evidence]:
            self.assertTrue(proc.observation_exists(observation_id), observation_id)
        self.assertTrue(proc.store.journal_entries("reconcile.decision", intent.action_id))

    def test_act02_unreadable_readback_keeps_the_intent_uncertain_across_a_restart(self):
        """ACT 02 / REC 01: the input went out but the screen changed unexpectedly and the tactic screen cannot be read back.
        The intent stays UNCERTAIN through a restart with its evidence kept and no input sent; once the readback works again a
        later reconciliation confirms the effect that had actually landed."""
        adapter = FakeAdapter()
        adapter.inject("unexpected_screen", on_step=2)
        adapter.inject_readback("selected_tactic", ValueStatus.UNSUPPORTED, "tactics screen cannot be read")
        first, snapshot = self.connected(adapter)
        report = first.orchestrator.execute(propose_tactic_change(snapshot), snapshot)
        self.assertIs(report.state, ActionState.UNCERTAIN, report.reason)
        self.assertEqual(adapter.selected_tactic_id, "counter-02", "the effect landed although the UI could not confirm it")
        first.die()
        second = self.start(adapter, lock_stale_seconds=0)
        connection = second.orchestrator.connect()
        self.assertTrue(connection.ok, connection.problems)
        self.assertEqual(connection.reconciled[0]["new_state"], "UNCERTAIN")
        self.assertIn("evidence preserved", connection.reconciled[0]["reason"])
        self.assertTrue(connection.reconciled[0]["evidence"])
        self.assertIs(second.store.get_intent(report.action_id).state, ActionState.UNCERTAIN)
        self.assertEqual(adapter.calls, 2, "nothing was re-sent")
        adapter.readback_faults.clear()
        decision = reconcile_uncertain(second.store, adapter, second.store.get_intent(report.action_id), second.orchestrator.collect("reconcile"))
        self.assertIs(decision.new_state, ActionState.CONFIRMED)
        self.assertEqual(adapter.calls, 2)


class HumanControlTests(NarrowLoopCase):
    def test_act03_stop_pressed_before_execution_sends_no_input(self):
        """ACT 03: Stop pressed after the intent was queued and before the writer ran cancels it; the orchestrator refuses the
        action while Stop is engaged, the adapter's Stop flag refuses input, and nothing reaches the game."""
        adapter = FakeAdapter()
        proc, snapshot = self.connected(adapter)
        action = propose_tactic_change(snapshot)
        intent = proc.orchestrator.factory.create(action["kind"], action["authority_scope"], snapshot, action["targets"], action["parameters"], required_capabilities=action["required_capabilities"], verification=action["verification"], decision_id=None)
        self.assertTrue(validate(intent, proc.orchestrator.capabilities, proc.settings.authority_profile(), snapshot, proc.store).ok)
        enqueue(proc.store, intent)
        cancelled = proc.orchestrator.stop("operator pressed Stop")
        self.assertEqual(cancelled, [intent.action_id])
        self.assertIs(proc.store.get_intent(intent.action_id).state, ActionState.CANCELLED)
        self.assertTrue(adapter.stop_flag.is_set())
        refusal = proc.orchestrator.execute(action, snapshot)
        self.assertIsInstance(refusal, str)
        self.assertIn("Stop is engaged", refusal)
        self.assertIs(proc.orchestrator.run_once().status, PassStatus.STOPPED)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(adapter.inputs, [])
        self.assertEqual(adapter.selected_tactic_id, "balanced-01")
        self.assertEqual(proc.store.journal_entries("ui.input", intent.action_id), [])
        # Stop is a pause, not a verdict: after the operator resumes, a new decision runs normally
        proc.orchestrator.resume()
        report = proc.orchestrator.execute(action, snapshot)
        self.assertIsInstance(report, ExecutionReport, report)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)

    def test_act03_focus_loss_pauses_before_the_first_input(self):
        """ACT 03: when another application has focus the intent stays QUEUED, paused at a safe boundary, and no input is sent."""
        adapter = FakeAdapter(focused=False)
        proc, snapshot = self.connected(adapter)
        report = proc.orchestrator.execute(propose_tactic_change(snapshot), snapshot)
        self.assertIsInstance(report, ExecutionReport, report)
        self.assertTrue(report.paused)
        self.assertIs(report.state, ActionState.QUEUED)
        self.assertEqual([p.check for p in report.problems], ["focus"])
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(adapter.inputs, [])
        self.assertTrue(proc.store.journal_entries("executor.paused", report.action_id))


class FreshContextTests(NarrowLoopCase):
    def test_act01_tactic_changed_between_snapshot_and_execution_expires_the_intent(self):
        """ACT 01: a human changes the tactic in the game after the decision snapshot, on the same game date. The queued intent
        expires at preflight because ``/tactics`` no longer matches the version it was built on; no input is sent."""
        adapter = FakeAdapter()
        proc, snapshot = self.connected(adapter)
        action = propose_tactic_change(snapshot)
        adapter.tactic_catalog["press-03"] = "4-3-3 Press"
        adapter.selected_tactic_id = "press-03"                 # the manager clicked something in the meantime
        report = proc.orchestrator.execute(action, snapshot)
        self.assertIsInstance(report, ExecutionReport, report)
        self.assertIs(report.state, ActionState.EXPIRED, report.reason)
        self.assertIn("freshness", [p.check for p in report.problems])
        self.assertIn("/tactics changed", report.reason)
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(adapter.inputs, [])
        self.assertEqual(adapter.selected_tactic_id, "press-03", "the human's choice stands")
        fresh = proc.orchestrator.collect("after")
        self.assertEqual((fresh.game_date, fresh.game_time), (snapshot.game_date, snapshot.game_time), "same game date and time: the date alone proves nothing")
        self.assertNotEqual(fresh.entity_versions["/tactics"], snapshot.entity_versions["/tactics"])
        self.assertIs(proc.store.get_intent(report.action_id).state, ActionState.EXPIRED)
        self.assertEqual(proc.store.journal_entries("ui.input", report.action_id), [])
        # the stored tactic is now outside the catalog, so a fresh proposal is refused rather than guessed
        with self.assertRaises(LookupError):
            propose_tactic_change(fresh)


if __name__ == "__main__":
    unittest.main()
