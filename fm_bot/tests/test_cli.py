"""Tests for ``python -m fm_bot`` against the scripted fixture world (spec 15.1).

The CLI's transport factory hook replaces the loopback HTTP transport with a
``FakeTransport``; the clock hook makes ``run`` sleep on a fake clock. A
temporary SQLite file carries state between invocations, as it would for an
operator.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from typing import Any

from .. import __main__ as cli
from ..bridge_client.client import BridgeClient
from ..bridge_client.transport import FakeTransport
from ..execution.lifecycle import IntentFactory, transition
from ..interface.status import build_view
from ..orchestrator import FakeClock, PassResult, PassStatus, RunLevel
from ..state.records import ActionState
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from ..state.identity import utc_now
from ..state.store import SCHEMA_VERSION, Store
from ..state.visibility import InformationMode
from . import fixtures as fx


class StubOrchestrator:
    """Stands in for the orchestrator so ``run``'s exit code can be checked per pass outcome (test double, not an orchestrator)."""

    statuses: list[PassStatus] = []

    def __init__(self, store, client, adapter, settings, **kwargs):
        self.store, self.settings = store, settings

    def run(self, max_iterations: int = 1) -> list[PassResult]:
        return [PassResult(RunLevel.CONNECT, status) for status in self.statuses]

    def status_view(self):
        return build_view(self.store, settings=self.settings)

    def close(self) -> None:
        return None


class CliCase(unittest.TestCase):
    """Runs the CLI in-process with the fixture world and captures its output."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "bot.sqlite3")
        self.world: dict[str, Any] = fx.world()
        self.clock = FakeClock()
        self._saved = (cli.TRANSPORT_FACTORY, cli.ADAPTER_FACTORY, cli.CLOCK, cli.SLEEP)
        cli.TRANSPORT_FACTORY = lambda url: FakeTransport(dict(self.world))
        cli.ADAPTER_FACTORY = None
        cli.CLOCK, cli.SLEEP = self.clock, self.clock.advance

    def tearDown(self):
        cli.TRANSPORT_FACTORY, cli.ADAPTER_FACTORY, cli.CLOCK, cli.SLEEP = self._saved
        self.tmp.cleanup()

    def run_cli(self, *argv: str, before: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = ["--db", self.db, *argv] if before else list(argv)
        code = cli.main(args, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def register(self, *extra: str) -> str:
        code, out, err = self.run_cli("register", "--label", "Wycombe", "--save-path", os.path.join(self.tmp.name, "missing.fm"), *extra)
        self.assertEqual(code, cli.EXIT_OK, err)
        return out


class StatusAndRegisterTests(CliCase):
    def test_status_before_registration_explains_what_to_do(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("no career registered", out)
        self.assertIn("Bridge: not checked", out)

    def test_register_builds_the_manifest_from_the_bridge_and_persists_settings(self):
        out = self.register("--confirm-lineage")
        self.assertIn("Registered career", out)
        self.assertIn("club Wycombe, manager Test Manager", out)
        self.assertIn("Lineage: confirmed", out)
        self.assertIn("checksum not recorded", out)
        self.assertIn("Settings saved with defaults", out)
        store = Store(self.db)
        try:
            careers = store.list_careers()
            self.assertEqual(len(careers), 1)
            self.assertEqual((careers[0].club_id, careers[0].manager_id, careers[0].build), (742, 90001, fx.BUILD))
            checkpoint = store.list_checkpoints(store.list_branches(careers[0].career_id)[0].branch_id)[0]
            self.assertEqual(checkpoint.manifest.game_date, fx.GAME_DATE)
            self.assertEqual(checkpoint.manifest.path, os.path.join(self.tmp.name, "missing.fm"))
        finally:
            store.close()
        code, out, _ = self.run_cli("status", "--evidence")
        self.assertIn("Managing: Wycombe (club 742, manager 90001)", out)
        self.assertIn("settings: ", out)
        code, out, _ = self.run_cli("status", "--json")
        view = json.loads(out)
        self.assertTrue(view["career"]["lineage_confirmed"])

    def test_register_records_a_checksum_when_the_save_exists(self):
        path = os.path.join(self.tmp.name, "career.fm")
        with open(path, "wb") as handle:
            handle.write(b"not a real save")
        code, out, err = self.run_cli("register", "--label", "Wycombe", "--save-path", path)
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertNotIn("checksum not recorded", out)
        self.assertIn("Lineage: not confirmed", out)

    def test_register_refuses_a_disconnected_bridge(self):
        self.world["/status"] = (200, fx.status_payload(connected=False))
        code, out, err = self.run_cli("register", "--label", "x")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("not connected", err)

    def test_non_loopback_bridge_url_is_refused(self):
        cli.TRANSPORT_FACTORY = None
        code, _, err = self.run_cli("--bridge-url", "http://example.com:8765", "status")
        self.assertEqual(code, cli.EXIT_OK, "status needs no bridge")
        cli.TRANSPORT_FACTORY = lambda url: FakeTransport(dict(self.world))
        self.register()
        cli.TRANSPORT_FACTORY = None
        code, _, err = self.run_cli("--bridge-url", "http://example.com:8765", "snapshot")
        self.assertEqual(code, cli.EXIT_SETUP)
        self.assertIn("loopback", err)


class ConfirmLineageTests(CliCase):
    """ID 01 / spec 5.1: the operator confirms the lineage of the REGISTERED career; nothing forks the history."""

    def test_id01_confirm_lineage_clears_the_stop_without_a_second_career_or_branch(self):
        """ID 01: an unconfirmed lineage makes every snapshot unusable, and the advice the bot gives is `confirm-lineage`. That
        command confirms the registered career itself: the stop clears, the next snapshot is usable, and no second career, branch
        or checkpoint is created (registering again would have forked the production history)."""
        out = self.register()
        self.assertIn("run `confirm-lineage`", out)
        self.assertIn("would start a second career", out)
        code, out, _ = self.run_cli("status")
        self.assertIn("run `python -m fm_bot confirm-lineage`", out)
        code, out, _ = self.run_cli("snapshot")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("lineage not confirmed", out)
        code, out, err = self.run_cli("confirm-lineage", "--reason", "the 17 February save is the one I registered")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("Lineage confirmed for career", out)
        self.assertIn("the 17 February save is the one I registered", out)
        self.assertIn("No new career, branch or checkpoint was created", out)
        code, out, err = self.run_cli("snapshot")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("consistent - usable for decisions", out)
        store = Store(self.db)
        try:
            careers = store.list_careers()
            self.assertEqual(len(careers), 1, "the registered career is confirmed, not replaced")
            branches = store.list_branches(careers[0].career_id)
            self.assertEqual(len(branches), 1)
            self.assertEqual(len(store.list_checkpoints(branches[0].branch_id)), 1)
            entry = store.journal_entries(kind="orchestrator.lineage_confirmed")[-1]
            self.assertEqual((entry["body"]["career_id"], entry["body"]["branch_id"], entry["body"]["by"]), (careers[0].career_id, branches[0].branch_id, "cli"))
            self.assertEqual(entry["body"]["game_date"], fx.GAME_DATE)
        finally:
            store.close()
        code, out, _ = self.run_cli("status")
        self.assertNotIn("lineage not yet confirmed", out)
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_OK, err)

    def test_id01_a_restarted_process_detects_a_save_reloaded_from_an_earlier_point(self):
        """ID 01 / spec 5.1: the documented operator sequence - run the bot, quit it, load an earlier save in FM, run it again -
        is a stop for identity resolution, not a merge. Each invocation is a fresh process, so continuity is judged against the last
        anchor this database witnessed: the second run records no decision, mints no intent and creates no second branch, and it
        points at `confirm-lineage`. Only after the operator vouches for the loaded save does work resume, on the one production
        branch."""
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_OK, err)
        store = Store(self.db)
        try:
            career, branch = store.list_careers()[0], store.list_branches(store.list_careers()[0].career_id)[0]
            witnessed = store.get_anchor(career.career_id, branch.branch_id)
            self.assertIsNotNone(witnessed, "the process remembers the history it witnessed")
            self.assertEqual((witnessed.game_date, witnessed.game_time), (fx.GAME_DATE, fx.GAME_TIME))
            decisions, intents = len(store.list_decisions(limit=1000)), len(store.list_intents())
            self.assertGreater(decisions, 0, "the first run planned on the 17 February save")
        finally:
            store.close()
        # the operator quits the bot, loads the 10 February save in FM and runs the bot again
        self.world["/game"] = FakeTransport.envelope({"date": "2024-02-10", "time": "10:00"}, session_id=fx.SESSION)
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_GAME_STATE, out)
        self.assertIn("identity resolution required", out)
        self.assertIn("continuity date_reversed: game time 2024-02-17 10:00 -> 2024-02-10 10:00", out)
        self.assertIn("python -m fm_bot confirm-lineage", out)
        code, snapshot_out, _ = self.run_cli("snapshot")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("Continuity: date_reversed", snapshot_out)
        self.assertIn("confirm-lineage", snapshot_out)
        store = Store(self.db)
        try:
            self.assertEqual(len(store.list_careers()), 1)
            self.assertEqual(len(store.list_branches(career.career_id)), 1, "no second branch: the reload is resolved, never merged")
            self.assertEqual(len(store.list_decisions(limit=1000)), decisions, "nothing is decided on the branch until the lineage is confirmed")
            self.assertEqual(len(store.list_intents()), intents, "and no intent is minted")
            still = store.get_anchor(career.career_id, branch.branch_id)
            self.assertEqual((still.game_date, still.game_time), (fx.GAME_DATE, fx.GAME_TIME), "the witnessed anchor stays where the history ended")
            self.assertEqual([e["body"]["status"] for e in store.journal_entries(kind="orchestrator.identity_resolution_required")], ["date_reversed"])
        finally:
            store.close()
        code, out, err = self.run_cli("confirm-lineage", "--reason", "I really did load the 10 February save")
        self.assertEqual(code, cli.EXIT_OK, err)
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertNotIn("identity resolution required", out)
        store = Store(self.db)
        try:
            self.assertEqual(len(store.list_branches(career.career_id)), 1)
            self.assertGreater(len(store.list_decisions(limit=1000)), decisions, "work resumes on the confirmed save")
            resumed = store.get_anchor(career.career_id, branch.branch_id)
            self.assertEqual((resumed.game_date, resumed.game_time), ("2024-02-10", "10:00"), "continuity now runs from the save the operator vouched for")
        finally:
            store.close()

    def test_id01_confirm_lineage_refuses_a_save_that_is_not_the_registered_career(self):
        """ID 01 / spec 5.1: confirming vouches for *this* registered career, so a save whose club, manager or build differs is
        refused with the difference named; the lineage stays unconfirmed and nothing is created."""
        self.register()
        self.world["/club"] = FakeTransport.envelope({"id": 999, "name": "Somebody Else"}, session_id=fx.SESSION)
        code, _, err = self.run_cli("confirm-lineage")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("not the registered career", err)
        self.assertIn("club 999 differs from registered 742", err)
        self.assertIn("register it as its own career", err)
        store = Store(self.db)
        try:
            self.assertEqual(len(store.list_careers()), 1)
            self.assertFalse(store.get_setting("registry:active_career")[0]["lineage_confirmed"])
        finally:
            store.close()

    def test_confirm_lineage_needs_a_career_and_a_connected_bridge(self):
        code, _, err = self.run_cli("confirm-lineage")
        self.assertEqual(code, cli.EXIT_SETUP)
        self.assertIn("no career registered", err)
        self.register()
        self.world["/status"] = (200, fx.status_payload(connected=False))
        code, _, err = self.run_cli("confirm-lineage")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("not connected", err)


class SnapshotPlanExplainTests(CliCase):
    def test_commands_needing_a_career_say_so(self):
        for command in (["snapshot"], ["plan"], ["run", "--once"], ["reconcile"]):
            with self.subTest(command=command):
                code, _, err = self.run_cli(*command)
                self.assertEqual(code, cli.EXIT_SETUP)
                self.assertIn("no career registered", err)

    def test_snapshot_reports_consistency(self):
        self.register("--confirm-lineage")
        code, out, _ = self.run_cli("snapshot")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("consistent - usable for decisions", out)
        self.assertIn("Game day: 2024-02-17 10:00", out)
        code, out, _ = self.run_cli("snapshot", "--json")
        body = json.loads(out)
        self.assertEqual(body["consistency"], "consistent")
        self.assertNotIn("routes", body, "payloads stay in the store, not on the terminal")

    def test_unconfirmed_lineage_makes_the_snapshot_unusable(self):
        self.register()
        code, out, _ = self.run_cli("snapshot")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("NOT usable", out)
        self.assertIn("lineage not confirmed", out)

    def test_plan_prints_football_language_and_records_decisions(self):
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("plan")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("Next match: Advisory eleven v Bolton", out)
        self.assertIn("Team sheet: unverified", out)
        self.assertIn("Next: nothing proposed for execution", out)
        self.assertIn("Cannot submit.lineup yet", out)
        self.assertIn("Decisions recorded: plan.next_decision", out)
        for jargon in ("sqlite", "hungarian", "traceback"):
            self.assertNotIn(jargon, out.lower())
        code, out, err = self.run_cli("plan", "--json")
        self.assertEqual(code, cli.EXIT_OK, err)
        body = json.loads(out)
        self.assertEqual(body["status"], "planned")
        self.assertTrue(body["decision_ids"])
        self.assertEqual(body["decisions"][0]["model_versions"]["settings:authority_profile"], "1")
        decision_id = body["decision_ids"][0]
        code, out, err = self.run_cli("explain", decision_id)
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("Decision: plan.next_decision", out)
        self.assertIn("Chosen:", out)
        code, out, err = self.run_cli("explain", decision_id, "--evidence")
        self.assertIn("bridge:/squad", out)
        code, out, err = self.run_cli("explain", decision_id, "--json")
        self.assertEqual(json.loads(out)["decision_id"], decision_id)

    def test_explain_unknown_id_fails_loudly(self):
        self.register("--confirm-lineage")
        code, _, err = self.run_cli("explain", "dec-nothing")
        self.assertEqual(code, cli.EXIT_SETUP)
        self.assertIn("neither a decision nor an action", err)

    def test_plan_with_windows_adapter_fails_closed(self):
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("--adapter", "windows", "plan")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("UI adapter windows: cannot act on the game", out)
        self.assertIn("Cannot submit.lineup yet: needs ui_action_adapter", out)


class ConfigTests(CliCase):
    def test_get_set_and_invalid_values(self):
        code, out, _ = self.run_cli("config", "get")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("authority_mode (v0, default not yet saved): \"advise\"", out)
        code, out, err = self.run_cli("config", "set", "authority_mode", "scoped", "--reason", "trial")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn('authority_mode is now v1: "scoped_execution"', out)
        code, out, _ = self.run_cli("config", "set", "spending_limits", '{"max_weekly_wage_commitment": 4000, "allow_continue": true}')
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn('"period": "weekly"', out)
        code, out, _ = self.run_cli("config", "get", "authority_mode", "--json")
        self.assertEqual(json.loads(out)["authority_mode"]["value"], "scoped_execution")
        code, _, err = self.run_cli("config", "set", "development_emphasis", "7")
        self.assertEqual(code, cli.EXIT_SETUP)
        self.assertIn("setting not changed", err)
        code, _, err = self.run_cli("config", "get", "nonsense")
        self.assertEqual(code, cli.EXIT_SETUP)
        store = Store(self.db)
        try:
            self.assertEqual(len(store.journal_entries(kind="settings.changed")), 2)
            self.assertEqual(store.journal_entries(kind="settings.changed")[0]["body"]["by"], "cli")
        finally:
            store.close()


class RunAndReconcileTests(CliCase):
    def test_run_once_prints_passes_and_the_operator_view(self):
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("UI adapter fake: workflows reported for ui_action_adapter", out)
        self.assertIn("Pass 1 (weekly): blocked - 2024-02-17 10:00", out)
        self.assertIn("Calendar: waiting", out)
        self.assertIn("Managing: Wycombe", out)
        self.assertIn("Next: nothing proposed", out)
        self.assertEqual(self.clock.sleeps, [1.0], "only the settle interval, on the fake clock")
        store = Store(self.db)
        try:
            self.assertIsNone(store.lock_owner("manager"), "the manager lock is released when the run ends")
            self.assertTrue(store.journal_entries(kind="orchestrator.pass"))
        finally:
            store.close()

    def test_run_iterations_sleeps_on_the_fake_clock_and_changes_authority(self):
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("run", "--iterations", "3", "--authority", "observe")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("Authority mode set to observe", out)
        self.assertIn("Pass 3", out)
        self.assertIn("nothing has changed since the last decision", out)
        self.assertEqual(self.clock.sleeps.count(5.0), 2, "idle interval between passes; no real sleeping")
        code, out, _ = self.run_cli("config", "get", "authority_mode")
        self.assertIn('"observe"', out)

    def test_run_authority_is_a_configuration_change_and_survives_a_refused_lock(self):
        """Spec 1.2: ``--authority`` is a configuration change the operator typed, so it is saved, versioned, journaled and
        reported even when the run is then refused with exit 3 - the manager lock (4.2) guards the writer, not the settings,
        and ``config set authority_mode`` is equally available while a rival instance runs. Nothing is silently rolled back."""
        self.register("--confirm-lineage")
        rival = Store(self.db)
        try:
            self.assertTrue(rival.acquire_lock("manager", "rival-instance", now=self.clock.utc_now()))
            code, out, err = self.run_cli("run", "--once", "--authority", "autonomy")
            self.assertEqual(code, cli.EXIT_LOCK, err)
            self.assertIn("manager lock is held by 'rival-instance'", err)
            self.assertIn("Authority mode set to club_autonomy", out, "the operator is told what was saved")
            self.assertEqual(rival.lock_owner("manager"), "rival-instance", "the refused run never took the lock")
        finally:
            rival.close()
        code, out, _ = self.run_cli("config", "get", "authority_mode")
        self.assertIn('"club_autonomy"', out)
        self.assertIn("authority_mode (v2)", out, "one change, one version - no rollback churn")
        store = Store(self.db)
        try:
            entries = store.journal_entries(kind="settings.changed")
        finally:
            store.close()
        self.assertTrue(any(e["body"].get("reason") == "run --authority" for e in entries), "the change is auditable")

    def test_run_with_windows_adapter_reports_fail_closed(self):
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("--adapter", "windows", "run", "--once")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("UI adapter windows: cannot act on the game", out)
        self.assertIn("fail-closed", out)

    def test_run_against_a_disconnected_bridge_exits_with_game_state(self):
        self.register("--confirm-lineage")
        self.world["/status"] = (200, fx.status_payload(connected=False))
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_GAME_STATE)
        self.assertIn("disconnected", out)

    def test_second_instance_is_refused_by_the_manager_lock(self):
        self.register("--confirm-lineage")
        holder = Store(self.db)
        try:
            self.assertTrue(holder.acquire_lock("manager", "another-bot"))
            code, _, err = self.run_cli("run", "--once")
            self.assertEqual(code, cli.EXIT_LOCK)
            self.assertIn("one bot instance per game", err)
        finally:
            holder.release_lock("manager", "another-bot")
            holder.close()

    def test_reconcile_with_nothing_in_flight(self):
        self.register("--confirm-lineage")
        code, out, err = self.run_cli("reconcile")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("nothing to reconcile", out)

    def _in_flight_intent(self, state=ActionState.EXECUTING):
        """One intent left in flight by an earlier process, over the registered career of this database."""
        store = Store(self.db)
        career = store.list_careers()[0]
        branch = store.list_branches(career.career_id)[0]
        client = BridgeClient(FakeTransport(dict(self.world)), store, context={"career_id": career.career_id, "branch_id": branch.branch_id})
        context = CollectionContext(career.career_id, branch.branch_id, InformationMode.BRIDGE_OBSERVED, None, True, False, None, 0)
        snapshot = SnapshotCollector(client, store).collect(SnapshotRequirements(routes=list(cli.DEFAULT_ROUTES), label="pre-crash"), context)
        self.assertTrue(snapshot.valid, snapshot.consistency_reasons)
        intent = IntentFactory(store).create("select_validated_tactic", "tactics.select", snapshot, {"routes": ["/tactics"]}, {"catalog_id": "x"}, verification="tactic_selection")
        for target in (ActionState.VALIDATED, ActionState.QUEUED, state):
            transition(store, intent, target, "left behind by an earlier process")
        return store, intent

    def test_reconcile_settles_an_earlier_process_intent_while_its_manager_lock_is_still_fresh(self):
        """Spec 12.4 / REC 01: ``reconcile`` is the recovery path for intents left in flight by a process that died.

        A dead instance's manager lock row survives with a fresh heartbeat, and the bot deliberately refuses to steal a
        fresh lock (``run`` exits EXIT_LOCK). Requiring the manager lock here would therefore refuse the recovery command
        for the whole stale window, exactly when the operator needs it, so ``reconcile`` deliberately does not take it.
        """
        self.register("--confirm-lineage")
        store, intent = self._in_flight_intent()
        try:
            self.assertTrue(store.acquire_lock("manager", "crashed-instance"), "the dead process's lock row is still there")
            code, _, err = self.run_cli("run", "--once")
            self.assertEqual(code, cli.EXIT_LOCK, "a lock-taking command cannot recover from this state")
            self.assertEqual(store.get_intent(intent.action_id).state, ActionState.EXECUTING)
            code, out, err = self.run_cli("reconcile")
            self.assertEqual(code, cli.EXIT_OK, err)
            self.assertIn("EXECUTING -> UNCERTAIN", out)
            self.assertEqual(store.get_intent(intent.action_id).state, ActionState.UNCERTAIN)
        finally:
            store.release_lock("manager", "crashed-instance")
            store.close()

    def test_reconcile_never_requeues_or_re_dispatches_an_in_flight_intent(self):
        """Spec 12.3/12.4: reconciliation reads back, it never sends input and never silently resets an intent to QUEUED."""
        self.register("--confirm-lineage")
        store, intent = self._in_flight_intent()
        try:
            self.assertEqual(self.run_cli("reconcile")[0], cli.EXIT_OK)
            self.assertEqual(store.list_intents([ActionState.QUEUED]), [], "no intent is put back on the queue")
            self.assertEqual(store.list_attempts(intent.action_id), [], "no UI input was sent")
            code, out, err = self.run_cli("reconcile")
            self.assertEqual(code, cli.EXIT_OK, err)
            self.assertIn("UNCERTAIN -> UNCERTAIN", out, "an unestablished effect stays unestablished; it is not guessed away")
        finally:
            store.close()

    def test_run_exits_with_game_state_when_the_game_or_bridge_state_prevented_the_pass(self):
        """The documented exit-code contract: 1 when the game or bridge state prevented the command. A clock that never settles
        leaves the pass inconsistent, and a stop for identity resolution means nothing was decided, so both exit 1; a pass that
        planned, acted, was blocked on a capability or found nothing changed exits 0."""
        self.register("--confirm-lineage")
        self.world["/game"] = [FakeTransport.envelope({"date": fx.GAME_DATE, "time": f"{10 + i // 60:02d}:{i % 60:02d}"}, session_id=fx.SESSION) for i in range(40)]
        code, out, _ = self.run_cli("run", "--once")
        self.assertEqual(code, cli.EXIT_GAME_STATE, out)
        self.assertIn("inconsistent", out)
        self.world = fx.world()
        prevented = (PassStatus.DISCONNECTED, PassStatus.BUILD_UNSUPPORTED, PassStatus.IDENTITY_MISMATCH, PassStatus.IDENTITY_RESOLUTION_REQUIRED, PassStatus.INCONSISTENT, PassStatus.LOCK_LOST)
        saved = cli.Orchestrator
        cli.Orchestrator = StubOrchestrator
        try:
            for status in prevented:
                with self.subTest(status=status):
                    StubOrchestrator.statuses = [status]
                    self.assertEqual(self.run_cli("run", "--once")[0], cli.EXIT_GAME_STATE)
            for status in (PassStatus.PLANNED, PassStatus.ACTED, PassStatus.BLOCKED, PassStatus.UNCHANGED, PassStatus.MATCH_OBSERVED, PassStatus.STOPPED):
                with self.subTest(status=status):
                    StubOrchestrator.statuses = [status]
                    self.assertEqual(self.run_cli("run", "--once")[0], cli.EXIT_OK)
            StubOrchestrator.statuses = [PassStatus.INCONSISTENT, PassStatus.PLANNED]
            self.assertEqual(self.run_cli("run", "--iterations", "2")[0], cli.EXIT_OK, "the last pass decides the exit code")
        finally:
            cli.Orchestrator = saved
            StubOrchestrator.statuses = []

    def test_bad_iterations_is_a_usage_error(self):
        self.register("--confirm-lineage")
        code, _, err = self.run_cli("run", "--iterations", "0")
        self.assertEqual(code, cli.EXIT_SETUP)
        self.assertIn("--iterations", err)


class DatabasePathTests(CliCase):
    """An unusable ``--db`` path is a setup problem, never a raw SQLite traceback (spec 15.1, 15.3).

    The bot's own journal is part of its setup, so every way of failing to
    open it - a directory that does not exist, a file that is not a database,
    a schema newer than this bot understands - has to reach the operator as an
    explicit error naming the path, with the documented setup exit code (2).
    """

    def at(self, db_path: str, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--db", db_path, *(argv or ("status",))], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_a_db_path_whose_directory_does_not_exist_is_a_setup_error(self):
        missing = os.path.join(self.tmp.name, "no-such-directory", "bot.sqlite3")
        code, out, err = self.at(missing)
        self.assertEqual(code, cli.EXIT_SETUP, err)
        self.assertEqual(out, "")
        self.assertIn(missing, err)
        self.assertIn("cannot open the bot's journal", err)
        self.assertNotIn("Traceback", err)

    def test_a_db_path_that_is_a_directory_is_a_setup_error(self):
        directory = os.path.join(self.tmp.name, "a-directory")
        os.mkdir(directory)
        code, _, err = self.at(directory)
        self.assertEqual(code, cli.EXIT_SETUP, err)
        self.assertIn(directory, err)

    def test_a_file_that_is_not_a_database_is_a_setup_error(self):
        junk = os.path.join(self.tmp.name, "junk.sqlite3")
        with open(junk, "w", encoding="utf-8") as handle:
            handle.write("not a database at all\n")
        code, _, err = self.at(junk)
        self.assertEqual(code, cli.EXIT_SETUP, err)
        self.assertIn("not a database", err)

    def test_a_schema_newer_than_this_bot_is_reported_not_migrated(self):
        store = Store(self.db)
        store.close()
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION + 1, utc_now()))
            connection.commit()
        finally:
            connection.close()
        code, _, err = self.at(self.db)
        self.assertEqual(code, cli.EXIT_SETUP, err)
        self.assertIn(f"newer than this bot's {SCHEMA_VERSION}", err)
        self.assertNotIn("Traceback", err)

    def test_a_usable_db_path_still_opens(self):
        code, out, err = self.at(os.path.join(self.tmp.name, "fresh.sqlite3"))
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("no career registered", out)


if __name__ == "__main__":
    unittest.main()
