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
import tempfile
import unittest
from typing import Any

from .. import __main__ as cli
from ..bridge_client.transport import FakeTransport
from ..orchestrator import FakeClock
from ..state.store import Store
from . import fixtures as fx


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

    def test_bad_iterations_is_a_usage_error(self):
        self.register("--confirm-lineage")
        code, _, err = self.run_cli("run", "--iterations", "0")
        self.assertEqual(code, cli.EXIT_SETUP)
        self.assertIn("--iterations", err)


if __name__ == "__main__":
    unittest.main()
