"""Tests for the SQLite state store (spec 5.4, 15.3)."""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest

from ..state.identity import Anchor, CareerRegistry, SaveManifest
from ..state.records import (
    ActionIntent, ActionResult, ActionState, Certainty, Decision, ExecutionOutcome, FinancialCommitment, ModelVersion,
    MovementKind, Observation, Promise, ReleaseState, TRANSITIONS,
)
from ..state.store import MIGRATIONS, SCHEMA_VERSION, Store, StoreError
from ..state.units import Money, Period
from . import fixtures as fx


def registered_store():
    store = Store.memory()
    career, branch, checkpoint = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    return store, career, branch, checkpoint


def intent(career, branch, *, action_id="act-1", key="key-1", state=ActionState.PROPOSED) -> ActionIntent:
    return ActionIntent(action_id, "select_validated_tactic", "tactics.select", career.career_id, branch.branch_id, "snap-1", {"tactic": "4-4-2"}, {}, [], ["ui_action_adapter"], "relevant_state_change", "tactic_readback", key, state=state)


def observation(career, branch, payload=None, sequence=1) -> Observation:
    return Observation.create("bridge:/game", payload or {"date": fx.GAME_DATE}, career_id=career.career_id, branch_id=branch.branch_id, session_id="s", game_date=fx.GAME_DATE, game_time=fx.GAME_TIME, schema_version="v", sequence=sequence)


class MigrationTests(unittest.TestCase):
    def test_fresh_memory_store_is_at_current_version(self):
        store = Store.memory()
        self.assertEqual(store.current_version(), SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, max(MIGRATIONS))

    def test_migrate_is_idempotent(self):
        store = Store.memory()
        store.migrate()
        store.migrate()
        rows = store.connection.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
        self.assertEqual(rows, SCHEMA_VERSION)

    def test_reopening_an_on_disk_store_does_not_remigrate_or_back_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bot.sqlite")
            store = Store(path)
            store.put_setting("k", 1)
            store.close()
            again = Store(path)
            self.assertEqual(again.current_version(), SCHEMA_VERSION)
            self.assertEqual(again.get_setting("k"), (1, 1))
            again.close()
            self.assertFalse(any(name.endswith(".bak") for name in os.listdir(tmp)))

    def test_newer_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bot.sqlite")
            Store(path).close()
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION + 1, "x"))
            conn.commit()
            conn.close()
            with self.assertRaises(StoreError):
                Store(path)

    def test_backup_written_before_migrating_an_older_on_disk_database(self):
        """An on-disk database at an older schema is copied to ``<path>.v<old>.bak`` before migrating.

        Simulated by pretending the current schema is one version higher with an empty migration.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bot.sqlite")
            store = Store(path)
            store.put_setting("k", "before")
            store.close()
            import fm_bot.state.store as store_module
            original = store_module.SCHEMA_VERSION
            store_module.SCHEMA_VERSION = original + 1
            MIGRATIONS[original + 1] = ["CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)"]
            try:
                upgraded = Store(path)
                self.assertEqual(upgraded.current_version(), original + 1)
                self.assertEqual(upgraded.get_setting("k"), ("before", 1))
                upgraded.close()
                backup = f"{path}.v{original}.bak"
                self.assertTrue(os.path.exists(backup))
                old = sqlite3.connect(backup)
                self.assertEqual(old.execute("SELECT MAX(version) FROM schema_version").fetchone()[0], original)
                old.close()
            finally:
                store_module.SCHEMA_VERSION = original
                MIGRATIONS.pop(original + 1, None)


class JournalTests(unittest.TestCase):
    def test_journal_entries_are_ordered_and_filterable(self):
        store = Store.memory()
        first = store.journal("note", {"a": 1}, "ref-1")
        second = store.journal("note", {"a": 2}, "ref-2")
        self.assertLess(first, second)
        entries = store.journal_entries(kind="note")
        self.assertEqual([e["body"] for e in entries], [{"a": 1}, {"a": 2}])
        self.assertEqual(store.journal_entries(ref_id="ref-2")[0]["seq"], second)

    def test_the_newest_entry_of_a_kind_is_readable_past_any_page_limit(self):
        """Spec 15.1: "the last one" is read as the newest row. A page of ``journal_entries`` is the OLDEST ``limit`` rows, so its
        end freezes once a kind has more entries than the page; ``latest_journal_entry`` follows the journal instead."""
        store = Store.memory()
        for index in range(5):
            store.journal("connection", {"connected": True, "index": index})
        self.assertEqual(store.journal_entries(kind="connection", limit=2)[-1]["body"]["index"], 1, "a page of the oldest rows ends on a stale row")
        self.assertEqual(store.journal_entries(kind="connection", limit=2, newest_first=True)[0]["body"]["index"], 4)
        self.assertEqual(store.latest_journal_entry(kind="connection")["body"]["index"], 4)
        store.journal("connection", {"connected": False, "index": 5}, "ref-x")
        self.assertEqual(store.latest_journal_entry(kind="connection")["body"]["index"], 5)
        self.assertEqual(store.latest_journal_entry(ref_id="ref-x")["body"]["index"], 5)
        self.assertIsNone(store.latest_journal_entry(kind="never-journaled"), "an absent kind is absent, not a guess")

    def test_journal_is_append_only(self):
        store = Store.memory()
        seq = store.journal("note", {"a": 1})
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE journal SET body = '{}' WHERE seq = ?", (seq,))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("DELETE FROM journal WHERE seq = ?", (seq,))
        self.assertEqual(len(store.journal_entries()), 1)

    def test_blob_store_is_content_addressed(self):
        store = Store.memory()
        store.put_blob("h1", {"x": 1})
        store.put_blob("h1", {"x": 2})   # ignored: same hash means same content
        self.assertEqual(store.get_blob("h1"), {"x": 1})
        with self.assertRaises(KeyError):
            store.get_blob("nope")


class AnchorTests(unittest.TestCase):
    """ID 01 / spec 5.1: the last witnessed identity-and-time context per career branch, so continuity outlives the process."""

    def test_the_witnessed_anchor_round_trips_per_branch_and_can_be_forgotten(self):
        store, career, branch, _ = registered_store()
        self.assertIsNone(store.get_anchor(career.career_id, branch.branch_id), "nothing witnessed yet is None, not a default")
        anchor = Anchor(career.career_id, branch.branch_id, "session-1", fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME, 7)
        store.put_anchor(anchor)
        self.assertEqual(store.get_anchor(career.career_id, branch.branch_id), anchor)
        later = Anchor(career.career_id, branch.branch_id, "session-1", fx.BUILD, 90001, 742, "2024-02-18", "09:00", 8)
        store.put_anchor(later)
        self.assertEqual(store.get_anchor(career.career_id, branch.branch_id), later, "one row per branch: the latest witnessed anchor")
        self.assertIsNone(store.get_anchor(career.career_id, "branch-other"), "anchors are per branch")
        store.delete_anchor(career.career_id, branch.branch_id)
        self.assertIsNone(store.get_anchor(career.career_id, branch.branch_id))

    def test_an_anchor_without_a_career_branch_is_refused(self):
        store = Store.memory()
        with self.assertRaises(StoreError):
            store.put_anchor(Anchor(None, None, "session-1", fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME, 1))


class ObservationTests(unittest.TestCase):
    def test_observations_are_immutable(self):
        store, career, branch, _ = registered_store()
        obs = observation(career, branch)
        store.insert_observation(obs)
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("UPDATE observations SET source = 'x' WHERE observation_id = ?", (obs.observation_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            store.connection.execute("DELETE FROM observations WHERE observation_id = ?", (obs.observation_id,))
        loaded = store.get_observation(obs.observation_id)
        self.assertEqual(loaded.payload, {"date": fx.GAME_DATE})
        self.assertEqual(loaded.payload_hash, obs.payload_hash)
        self.assertIsNone(store.get_observation(obs.observation_id, with_payload=False).payload)

    def test_foreign_keys_enforced_with_store_error(self):
        store = Store.memory()
        obs = Observation.create("bridge:/game", {}, career_id="career-x", branch_id="branch-x", session_id="s", game_date=None, game_time=None, schema_version="v")
        with self.assertRaises(StoreError) as ctx:
            store.insert_observation(obs)
        self.assertIn("register the career/branch", str(ctx.exception))
        self.assertEqual(store.list_observations(), [])
        self.assertEqual(store.journal_entries(kind="observation"), [])

    def test_failed_transaction_rolls_back_journal(self):
        """A failed write leaves no partial journal entry behind."""
        store, career, branch, _ = registered_store()
        before = len(store.journal_entries())
        bad = intent(career, branch)
        bad.branch_id = "branch-missing"
        with self.assertRaises(StoreError):
            store.insert_intent(bad)
        self.assertEqual(len(store.journal_entries()), before)

    def test_list_observations_filters_by_branch_and_source(self):
        store, career, branch, _ = registered_store()
        store.insert_observation(observation(career, branch, sequence=2))
        store.insert_observation(Observation.create("bridge:/squad", [], career_id=career.career_id, branch_id=branch.branch_id, session_id="s", game_date=None, game_time=None, schema_version="v", sequence=1))
        listed = store.list_observations(branch_id=branch.branch_id)
        self.assertEqual([o.source for o in listed], ["bridge:/squad", "bridge:/game"])
        self.assertEqual(len(store.list_observations(source="bridge:/game")), 1)


class IntentTests(unittest.TestCase):
    def test_transitions_follow_the_lifecycle(self):
        store, career, branch, _ = registered_store()
        item = intent(career, branch)
        store.insert_intent(item)
        for state in (ActionState.VALIDATED, ActionState.QUEUED, ActionState.EXECUTING, ActionState.VERIFYING, ActionState.CONFIRMED):
            store.update_intent_state(item, state, f"-> {state.value}")
        self.assertIs(store.get_intent("act-1").state, ActionState.CONFIRMED)
        transitions = [e["body"] for e in store.journal_entries(kind="intent.transition")]
        self.assertEqual([t["from"] for t in transitions], ["PROPOSED", "VALIDATED", "QUEUED", "EXECUTING", "VERIFYING"])

    def test_illegal_transition_raises_and_leaves_state_unchanged(self):
        store, career, branch, _ = registered_store()
        item = intent(career, branch)
        store.insert_intent(item)
        with self.assertRaises(StoreError):
            store.update_intent_state(item, ActionState.EXECUTING)
        self.assertIs(item.state, ActionState.PROPOSED)
        self.assertIs(store.get_intent("act-1").state, ActionState.PROPOSED)

    def test_terminal_states_have_no_exits(self):
        store, career, branch, _ = registered_store()
        item = intent(career, branch)
        store.insert_intent(item)
        store.update_intent_state(item, ActionState.CANCELLED)
        for state in ActionState:
            with self.assertRaises(StoreError):
                store.update_intent_state(item, state)
        self.assertEqual(TRANSITIONS[ActionState.CONFIRMED], set())

    def test_uncertain_must_reconcile(self):
        store, career, branch, _ = registered_store()
        item = intent(career, branch)
        store.insert_intent(item)
        for state in (ActionState.VALIDATED, ActionState.QUEUED, ActionState.EXECUTING, ActionState.UNCERTAIN):
            store.update_intent_state(item, state)
        with self.assertRaises(StoreError):
            store.update_intent_state(item, ActionState.QUEUED)   # no blind retry from UNCERTAIN
        store.update_intent_state(item, ActionState.RECONCILING)
        store.update_intent_state(item, ActionState.CONFIRMED)

    def test_idempotency_key_is_unique(self):
        store, career, branch, _ = registered_store()
        store.insert_intent(intent(career, branch, action_id="act-1", key="same"))
        with self.assertRaises(StoreError) as ctx:
            store.insert_intent(intent(career, branch, action_id="act-2", key="same"))
        self.assertIn("idempotency key", str(ctx.exception))
        self.assertEqual(store.find_intent_by_key("same").action_id, "act-1")
        self.assertIsNone(store.find_intent_by_key("other"))

    def test_list_intents_by_state_and_branch(self):
        store, career, branch, _ = registered_store()
        a = intent(career, branch, action_id="a", key="ka")
        b = intent(career, branch, action_id="b", key="kb")
        store.insert_intent(a)
        store.insert_intent(b)
        store.update_intent_state(b, ActionState.VALIDATED)
        self.assertEqual([i.action_id for i in store.list_intents([ActionState.VALIDATED])], ["b"])
        self.assertEqual(len(store.list_intents(branch_id=branch.branch_id)), 2)
        self.assertEqual(store.list_intents(branch_id="none"), [])

    def test_attempts_are_recorded_per_intent(self):
        store, career, branch, _ = registered_store()
        item = intent(career, branch)
        store.insert_intent(item)
        result = ActionResult("res-1", "act-1", 1, ["obs-1"], [], ActionState.EXECUTING, None, None, None, None)
        store.insert_attempt(result)
        result.execution_state = ActionState.CONFIRMED
        result.outcome = ExecutionOutcome.CONFIRMED
        result.finished_at = "2026-01-01T00:00:00Z"
        store.update_attempt(result)
        attempts = store.list_attempts("act-1")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["outcome"], "confirmed")
        with self.assertRaises(StoreError):
            store.insert_attempt(ActionResult("res-2", "act-1", 1, [], [], ActionState.EXECUTING, None, None, None, None))


class RecordTableTests(unittest.TestCase):
    def test_commitments_round_trip_money(self):
        store, _, branch, _ = registered_store()
        item = FinancialCommitment("c-1", "Sam Vokes", MovementKind.PAYMENT, Money.native_gbp(6100, Period.WEEKLY), "2024-02-23", Period.WEEKLY, "2025-06-30", None, "club", Certainty.OBSERVED_COMMITTED, "obs-1", category="wages", included_in_aggregate="payroll_spending_weekly")
        store.upsert_commitment(item, branch.branch_id)
        item.version = 2
        store.upsert_commitment(item, branch.branch_id)
        listed = store.list_commitments(branch.branch_id)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].amount, Money.native_gbp(6100, Period.WEEKLY))
        self.assertEqual(listed[0].version, 2)
        self.assertEqual(store.list_commitments("other"), [])

    def test_promises_filter_by_status(self):
        store, _, branch, _ = registered_store()
        store.upsert_promise(Promise("p-1", 1015, "player", "start 3 of next 5", "2024-03-16", "0/3", None, None, "observed", "obs-1"), branch.branch_id)
        store.upsert_promise(Promise("p-2", 1014, "player", "new contract talks", None, "none", None, None, "observed", "obs-1", status="kept"), branch.branch_id)
        self.assertEqual([p.promise_id for p in store.list_promises(branch.branch_id, status="open")], ["p-1"])
        self.assertEqual(len(store.list_promises(branch.branch_id)), 2)

    def test_decisions_and_snapshots_are_stored(self):
        store, _, _, _ = registered_store()
        decision = Decision("d-1", "objective-v1", "snap-missing", [], [], [], None, ["no candidates"])
        with self.assertRaises(StoreError):
            store.insert_decision(decision)   # snapshot must exist
        self.assertIsNone(store.get_decision("d-1"))

    def test_model_versions_update_release_state(self):
        store = Store.memory()
        model = ModelVersion("minutes", "1", {}, {}, "bridge_observed", [fx.BUILD], {}, "hash", ReleaseState.CANDIDATE)
        store.insert_model_version(model)
        model.release_state = ReleaseState.RELEASED
        store.update_model_version(model)
        listed = store.list_model_versions("minutes")
        self.assertIs(listed[0].release_state, ReleaseState.RELEASED)
        with self.assertRaises(StoreError):
            store.insert_model_version(model)   # (model_id, version) is unique


class SettingsTests(unittest.TestCase):
    def test_settings_are_versioned_and_journaled(self):
        store = Store.memory()
        self.assertIsNone(store.get_setting("authority"))
        self.assertEqual(store.put_setting("authority", {"mode": "advise"}), 1)
        self.assertEqual(store.put_setting("authority", {"mode": "scoped_execution"}), 2)
        self.assertEqual(store.get_setting("authority"), ({"mode": "scoped_execution"}, 2))
        versions = [e["body"]["version"] for e in store.journal_entries(kind="setting", ref_id="authority")]
        self.assertEqual(versions, [1, 2])


class LockTests(unittest.TestCase):
    def test_lock_is_exclusive_while_fresh(self):
        store = Store.memory()
        self.assertTrue(store.acquire_lock("manager", "bot-a"))
        self.assertFalse(store.acquire_lock("manager", "bot-b"))
        self.assertEqual(store.lock_owner("manager"), "bot-a")
        self.assertTrue(store.acquire_lock("manager", "bot-a"))    # re-entrant for the owner

    def test_heartbeat_only_for_owner_and_release(self):
        store = Store.memory()
        store.acquire_lock("manager", "bot-a")
        self.assertTrue(store.heartbeat_lock("manager", "bot-a"))
        self.assertFalse(store.heartbeat_lock("manager", "bot-b"))
        store.release_lock("manager", "bot-b")   # not the owner: no effect
        self.assertEqual(store.lock_owner("manager"), "bot-a")
        store.release_lock("manager", "bot-a")
        self.assertIsNone(store.lock_owner("manager"))
        self.assertFalse(store.heartbeat_lock("manager", "bot-a"))

    def test_stale_lock_can_be_taken_over(self):
        store = Store.memory()
        store.acquire_lock("manager", "bot-a")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)).isoformat()
        store.connection.execute("UPDATE locks SET heartbeat_at = ? WHERE name = 'manager'", (old,))
        self.assertFalse(store.acquire_lock("manager", "bot-b", stale_after_seconds=1000))
        self.assertTrue(store.acquire_lock("manager", "bot-b", stale_after_seconds=300))
        self.assertEqual(store.lock_owner("manager"), "bot-b")
        self.assertFalse(store.heartbeat_lock("manager", "bot-a"))

    def test_staleness_is_judged_by_the_callers_clock_when_one_is_given(self):
        """Spec 4.2, 12.4: a caller with its own clock (the orchestrator's injected ``Clock``) can stamp and judge the lock by
        that clock, so "this heartbeat is older than the threshold" is expressible without waiting for it. Omitting ``now``
        keeps the system clock, which is what every other caller relies on."""
        store = Store.memory()
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        at = lambda seconds: (start + dt.timedelta(seconds=seconds)).isoformat()  # noqa: E731
        self.assertTrue(store.acquire_lock("manager", "bot-a", now=at(0)))
        self.assertEqual(store.connection.execute("SELECT heartbeat_at FROM locks WHERE name = 'manager'").fetchone()["heartbeat_at"], at(0))
        self.assertFalse(store.acquire_lock("manager", "bot-b", stale_after_seconds=300, now=at(299)), "a heartbeat 299s old is not stale")
        self.assertTrue(store.heartbeat_lock("manager", "bot-a", now=at(250)))
        self.assertFalse(store.acquire_lock("manager", "bot-b", stale_after_seconds=300, now=at(549)), "the heartbeat moved the deadline")
        self.assertTrue(store.acquire_lock("manager", "bot-b", stale_after_seconds=300, now=at(550)))
        self.assertEqual(store.lock_owner("manager"), "bot-b")
        # the system-clock default is unchanged: a lock just taken with the default is fresh by the default
        store.release_lock("manager", "bot-b")
        self.assertTrue(store.acquire_lock("manager", "bot-c"))
        self.assertFalse(store.acquire_lock("manager", "bot-d"))
        with self.assertRaises(ValueError):
            store.acquire_lock("manager", "bot-e", now="not a timestamp")


if __name__ == "__main__":
    unittest.main()
