"""SQLite state store with transactional writes and an append-only journal (spec 5.4).

Tables: careers, branches, checkpoints, observations, snapshots, decisions,
action_intents, action_attempts, commitments, promises, experiments,
model_versions, settings, journal, blobs. Foreign keys are enforced. Large
immutable evidence payloads live in ``blobs`` keyed by content hash. Schema
migrations are explicit version increments; a backup of an existing database
is written before a migration runs.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .identity import BranchIdentity, BranchKind, CareerIdentity, Checkpoint, SaveManifest, utc_now
from .records import (ActionIntent, ActionResult, ActionState, Decision, DecisionSnapshot, Experiment, FinancialCommitment, ModelVersion, Observation, Promise, canonical_json)

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, list[str]] = {
    1: [
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)",
        "CREATE TABLE careers (career_id TEXT PRIMARY KEY, label TEXT NOT NULL, build TEXT NOT NULL, manager_id INTEGER NOT NULL, club_id INTEGER NOT NULL, registered_at TEXT NOT NULL)",
        "CREATE TABLE branches (branch_id TEXT PRIMARY KEY, career_id TEXT NOT NULL REFERENCES careers(career_id), kind TEXT NOT NULL, parent_branch_id TEXT REFERENCES branches(branch_id), checkpoint_id TEXT, created_at TEXT NOT NULL, label TEXT)",
        "CREATE TABLE checkpoints (checkpoint_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(branch_id), manifest TEXT NOT NULL, created_at TEXT NOT NULL, label TEXT)",
        "CREATE TABLE blobs (hash TEXT PRIMARY KEY, content TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE observations (observation_id TEXT PRIMARY KEY, source TEXT NOT NULL, payload_hash TEXT NOT NULL REFERENCES blobs(hash), career_id TEXT REFERENCES careers(career_id), branch_id TEXT REFERENCES branches(branch_id), session_id TEXT, game_date TEXT, game_time TEXT, observed_at TEXT NOT NULL, schema_version TEXT NOT NULL, visibility TEXT NOT NULL, quality TEXT NOT NULL, http_status INTEGER, error TEXT, sequence INTEGER NOT NULL)",
        "CREATE INDEX observations_branch_time ON observations(branch_id, observed_at)",
        "CREATE TABLE snapshots (snapshot_id TEXT PRIMARY KEY, branch_id TEXT REFERENCES branches(branch_id), consistency TEXT NOT NULL, information_mode TEXT NOT NULL, game_date TEXT, game_time TEXT, session_id TEXT, collected_at TEXT NOT NULL, body TEXT NOT NULL)",
        "CREATE TABLE decisions (decision_id TEXT PRIMARY KEY, snapshot_id TEXT REFERENCES snapshots(snapshot_id), kind TEXT NOT NULL, objective_version TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL)",
        "CREATE TABLE action_intents (action_id TEXT PRIMARY KEY, kind TEXT NOT NULL, authority_scope TEXT NOT NULL, career_id TEXT NOT NULL REFERENCES careers(career_id), branch_id TEXT NOT NULL REFERENCES branches(branch_id), decision_snapshot_id TEXT, idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL, state_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, body TEXT NOT NULL)",
        "CREATE INDEX action_intents_state ON action_intents(state)",
        "CREATE TABLE action_attempts (result_id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES action_intents(action_id), attempt INTEGER NOT NULL, execution_state TEXT NOT NULL, outcome TEXT, started_at TEXT NOT NULL, finished_at TEXT, body TEXT NOT NULL, UNIQUE(action_id, attempt))",
        "CREATE TABLE commitments (commitment_id TEXT PRIMARY KEY, branch_id TEXT REFERENCES branches(branch_id), counterparty TEXT NOT NULL, kind TEXT NOT NULL, due_date TEXT NOT NULL, certainty TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL)",
        "CREATE TABLE promises (promise_id TEXT PRIMARY KEY, branch_id TEXT REFERENCES branches(branch_id), party_id INTEGER NOT NULL, status TEXT NOT NULL, deadline TEXT, body TEXT NOT NULL)",
        "CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES branches(branch_id), checkpoint_id TEXT NOT NULL REFERENCES checkpoints(checkpoint_id), policy_version TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL)",
        "CREATE TABLE model_versions (model_id TEXT NOT NULL, version TEXT NOT NULL, information_mode TEXT NOT NULL, release_state TEXT NOT NULL, artifact_hash TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(model_id, version))",
        "CREATE TABLE settings (key TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL, updated_at TEXT NOT NULL)",
        "CREATE TABLE journal (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, at_utc TEXT NOT NULL, ref_id TEXT, body TEXT NOT NULL)",
        "CREATE TABLE locks (name TEXT PRIMARY KEY, owner TEXT NOT NULL, acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL)",
    ],
}


class StoreError(RuntimeError):
    pass


class JournalWriteError(StoreError):
    """The journal is append-only; updates and deletes are refused."""


class Store:
    """One process-wide connection with a re-entrant lock; a single journal writer."""

    def __init__(self, path: str = ":memory:", *, backup_before_migration: bool = True):
        self.path = path
        self._lock = threading.RLock()
        self._depth = 0
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL") if path != ":memory:" else None
        self.migrate(backup=backup_before_migration)
        self.connection.create_function("journal_guard", 0, self._journal_guard)
        self.connection.execute("CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END")
        self.connection.execute("CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END")
        self.connection.execute("CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT, 'observations are immutable'); END")
        self.connection.execute("CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT, 'observations are immutable'); END")

    @staticmethod
    def _journal_guard():
        return 1

    @classmethod
    def memory(cls) -> "Store":
        return cls(":memory:")

    # ----- migrations -----
    def current_version(self) -> int:
        try:
            row = self.connection.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["v"] or 0)

    def migrate(self, *, backup: bool = True) -> None:
        version = self.current_version()
        if version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise StoreError(f"database schema {version} is newer than this bot's {SCHEMA_VERSION}")
        if version > 0 and backup and self.path != ":memory:" and os.path.exists(self.path):
            shutil.copy2(self.path, f"{self.path}.v{version}.bak")
        with self.transaction():
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    self.connection.execute(statement)
                self.connection.execute("INSERT INTO schema_version(version, applied_at) VALUES (?, ?)", (target, utc_now()))

    # ----- transactions -----
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._depth == 0:
                self.connection.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.connection
            except sqlite3.IntegrityError as exc:
                self._depth -= 1
                if self._depth == 0:
                    self.connection.execute("ROLLBACK")
                raise StoreError(f"integrity violation: {exc}; register the career/branch before recording against it") from exc
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.connection.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    # ----- journal & blobs -----
    def journal(self, kind: str, body: Any, ref_id: str | None = None) -> int:
        with self.transaction() as conn:
            cursor = conn.execute("INSERT INTO journal(kind, at_utc, ref_id, body) VALUES (?, ?, ?, ?)", (kind, utc_now(), ref_id, canonical_json(body)))
            return int(cursor.lastrowid)

    def journal_entries(self, kind: str | None = None, ref_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        query = "SELECT seq, kind, at_utc, ref_id, body FROM journal"
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?"); params.append(kind)
        if ref_id:
            clauses.append("ref_id = ?"); params.append(ref_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY seq ASC LIMIT ?"
        params.append(limit)
        return [{"seq": r["seq"], "kind": r["kind"], "at_utc": r["at_utc"], "ref_id": r["ref_id"], "body": json.loads(r["body"])} for r in self.connection.execute(query, params)]

    def put_blob(self, content_hash: str, payload: Any) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO blobs(hash, content, created_at) VALUES (?, ?, ?)", (content_hash, canonical_json(payload), utc_now()))

    def get_blob(self, content_hash: str) -> Any:
        row = self.connection.execute("SELECT content FROM blobs WHERE hash = ?", (content_hash,)).fetchone()
        if row is None:
            raise KeyError(content_hash)
        return json.loads(row["content"])

    # ----- careers -----
    def insert_career(self, career: CareerIdentity) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO careers VALUES (?, ?, ?, ?, ?, ?)", (career.career_id, career.label, career.build, career.manager_id, career.club_id, career.registered_at))
            self.journal("career.registered", career.to_json(), career.career_id)

    def get_career(self, career_id: str) -> CareerIdentity | None:
        row = self.connection.execute("SELECT * FROM careers WHERE career_id = ?", (career_id,)).fetchone()
        return CareerIdentity(row["career_id"], row["label"], row["build"], row["manager_id"], row["club_id"], row["registered_at"]) if row else None

    def list_careers(self) -> list[CareerIdentity]:
        return [CareerIdentity(r["career_id"], r["label"], r["build"], r["manager_id"], r["club_id"], r["registered_at"]) for r in self.connection.execute("SELECT * FROM careers ORDER BY registered_at")]

    def insert_branch(self, branch: BranchIdentity) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO branches VALUES (?, ?, ?, ?, ?, ?, ?)", (branch.branch_id, branch.career_id, branch.kind.value, branch.parent_branch_id, branch.checkpoint_id, branch.created_at, branch.label))
            self.journal("branch.created", branch.to_json(), branch.branch_id)

    def get_branch(self, branch_id: str) -> BranchIdentity | None:
        row = self.connection.execute("SELECT * FROM branches WHERE branch_id = ?", (branch_id,)).fetchone()
        return BranchIdentity(row["branch_id"], row["career_id"], BranchKind(row["kind"]), row["parent_branch_id"], row["checkpoint_id"], row["created_at"], row["label"]) if row else None

    def list_branches(self, career_id: str) -> list[BranchIdentity]:
        return [BranchIdentity(r["branch_id"], r["career_id"], BranchKind(r["kind"]), r["parent_branch_id"], r["checkpoint_id"], r["created_at"], r["label"]) for r in self.connection.execute("SELECT * FROM branches WHERE career_id = ? ORDER BY created_at", (career_id,))]

    def insert_checkpoint(self, checkpoint: Checkpoint) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)", (checkpoint.checkpoint_id, checkpoint.branch_id, canonical_json(checkpoint.manifest.to_json()), checkpoint.created_at, checkpoint.label))
            self.journal("checkpoint.recorded", checkpoint.to_json(), checkpoint.checkpoint_id)

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        row = self.connection.execute("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)).fetchone()
        return Checkpoint(row["checkpoint_id"], row["branch_id"], SaveManifest.from_json(json.loads(row["manifest"])), row["created_at"], row["label"]) if row else None

    def list_checkpoints(self, branch_id: str) -> list[Checkpoint]:
        return [Checkpoint(r["checkpoint_id"], r["branch_id"], SaveManifest.from_json(json.loads(r["manifest"])), r["created_at"], r["label"]) for r in self.connection.execute("SELECT * FROM checkpoints WHERE branch_id = ? ORDER BY created_at", (branch_id,))]

    # ----- observations & snapshots -----
    def insert_observation(self, observation: Observation) -> None:
        with self.transaction() as conn:
            self.put_blob(observation.payload_hash, observation.payload)
            conn.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (observation.observation_id, observation.source, observation.payload_hash, observation.career_id, observation.branch_id, observation.session_id, observation.game_date, observation.game_time, observation.observed_at, observation.schema_version, observation.visibility.value, observation.quality.value, observation.http_status, observation.error, observation.sequence))
            self.journal("observation", {k: v for k, v in observation.to_json().items() if k != "payload"}, observation.observation_id)

    def get_observation(self, observation_id: str, *, with_payload: bool = True) -> Observation | None:
        row = self.connection.execute("SELECT * FROM observations WHERE observation_id = ?", (observation_id,)).fetchone()
        if row is None:
            return None
        from .records import QualityStatus, Visibility
        payload = self.get_blob(row["payload_hash"]) if with_payload else None
        return Observation(row["observation_id"], row["source"], row["payload_hash"], row["career_id"], row["branch_id"], row["session_id"], row["game_date"], row["game_time"], row["observed_at"], row["schema_version"], Visibility(row["visibility"]), QualityStatus(row["quality"]), row["http_status"], row["error"], row["sequence"], payload)

    def list_observations(self, *, branch_id: str | None = None, source: str | None = None, limit: int = 1000) -> list[Observation]:
        clauses, params = [], []
        if branch_id:
            clauses.append("branch_id = ?"); params.append(branch_id)
        if source:
            clauses.append("source = ?"); params.append(source)
        query = "SELECT observation_id FROM observations" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY sequence, observed_at LIMIT ?"
        params.append(limit)
        return [self.get_observation(r["observation_id"], with_payload=False) for r in self.connection.execute(query, params)]

    def insert_snapshot(self, snapshot: DecisionSnapshot) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (snapshot.snapshot_id, snapshot.branch_id, snapshot.consistency.value, snapshot.information_mode, snapshot.game_date, snapshot.game_time, snapshot.session_id, snapshot.collected_at, canonical_json(snapshot.to_json())))
            self.journal("snapshot", {k: v for k, v in snapshot.to_json().items() if k != "routes"}, snapshot.snapshot_id)

    def get_snapshot(self, snapshot_id: str) -> DecisionSnapshot | None:
        row = self.connection.execute("SELECT body FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)).fetchone()
        if row is None:
            return None
        from .records import ConsistencyStatus
        data = json.loads(row["body"])
        data["consistency"] = ConsistencyStatus(data["consistency"])
        return DecisionSnapshot(**data)

    # ----- decisions -----
    def insert_decision(self, decision: Decision) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)", (decision.decision_id, decision.snapshot_id, decision.kind, decision.objective_version, decision.created_at, canonical_json(decision.to_json())))
            self.journal("decision", decision.to_json(), decision.decision_id)

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self.connection.execute("SELECT body FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        return Decision(**json.loads(row["body"])) if row else None

    def list_decisions(self, limit: int = 100) -> list[Decision]:
        return [Decision(**json.loads(r["body"])) for r in self.connection.execute("SELECT body FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,))]

    # ----- intents & attempts -----
    def insert_intent(self, intent: ActionIntent) -> None:
        with self.transaction() as conn:
            existing = conn.execute("SELECT action_id FROM action_intents WHERE idempotency_key = ?", (intent.idempotency_key,)).fetchone()
            if existing:
                raise StoreError(f"idempotency key {intent.idempotency_key} already used by {existing['action_id']}")
            conn.execute("INSERT INTO action_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (intent.action_id, intent.kind, intent.authority_scope, intent.career_id, intent.branch_id, intent.decision_snapshot_id, intent.idempotency_key, intent.state.value, intent.state_reason, intent.created_at, intent.updated_at, canonical_json(intent.to_json())))
            self.journal("intent.created", intent.to_json(), intent.action_id)

    def update_intent_state(self, intent: ActionIntent, new_state: ActionState, reason: str | None = None) -> ActionIntent:
        from .records import TRANSITIONS
        if new_state not in TRANSITIONS[intent.state]:
            raise StoreError(f"illegal transition {intent.state.value} -> {new_state.value} for {intent.action_id}")
        previous = intent.state
        intent.state = new_state
        intent.state_reason = reason
        intent.updated_at = utc_now()
        with self.transaction() as conn:
            conn.execute("UPDATE action_intents SET state = ?, state_reason = ?, updated_at = ?, body = ? WHERE action_id = ?", (new_state.value, reason, intent.updated_at, canonical_json(intent.to_json()), intent.action_id))
            self.journal("intent.transition", {"action_id": intent.action_id, "from": previous.value, "to": new_state.value, "reason": reason}, intent.action_id)
        return intent

    def get_intent(self, action_id: str) -> ActionIntent | None:
        row = self.connection.execute("SELECT body FROM action_intents WHERE action_id = ?", (action_id,)).fetchone()
        return ActionIntent.from_json(json.loads(row["body"])) if row else None

    def find_intent_by_key(self, idempotency_key: str) -> ActionIntent | None:
        row = self.connection.execute("SELECT body FROM action_intents WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return ActionIntent.from_json(json.loads(row["body"])) if row else None

    def list_intents(self, states: list[ActionState] | None = None, branch_id: str | None = None) -> list[ActionIntent]:
        clauses, params = [], []
        if states:
            clauses.append("state IN (%s)" % ",".join("?" * len(states))); params.extend(s.value for s in states)
        if branch_id:
            clauses.append("branch_id = ?"); params.append(branch_id)
        query = "SELECT body FROM action_intents" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at"
        return [ActionIntent.from_json(json.loads(r["body"])) for r in self.connection.execute(query, params)]

    def insert_attempt(self, result: ActionResult) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO action_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (result.result_id, result.action_id, result.attempt, result.execution_state.value, result.outcome.value if result.outcome else None, result.started_at, result.finished_at, canonical_json(result.to_json())))
            self.journal("attempt", result.to_json(), result.action_id)

    def update_attempt(self, result: ActionResult) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE action_attempts SET execution_state = ?, outcome = ?, finished_at = ?, body = ? WHERE result_id = ?", (result.execution_state.value, result.outcome.value if result.outcome else None, result.finished_at, canonical_json(result.to_json()), result.result_id))
            self.journal("attempt.updated", result.to_json(), result.action_id)

    def list_attempts(self, action_id: str) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.connection.execute("SELECT body FROM action_attempts WHERE action_id = ? ORDER BY attempt", (action_id,))]

    # ----- commitments, promises -----
    def upsert_commitment(self, commitment: FinancialCommitment, branch_id: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO commitments VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (commitment.commitment_id, branch_id, commitment.counterparty, commitment.kind.value, commitment.due_date, commitment.certainty.value, commitment.version, canonical_json(commitment.to_json())))
            self.journal("commitment", commitment.to_json(), commitment.commitment_id)

    def list_commitments(self, branch_id: str | None = None) -> list[FinancialCommitment]:
        if branch_id is None:
            rows = self.connection.execute("SELECT body FROM commitments ORDER BY due_date")
        else:
            rows = self.connection.execute("SELECT body FROM commitments WHERE branch_id = ? ORDER BY due_date", (branch_id,))
        return [FinancialCommitment.from_json(json.loads(r["body"])) for r in rows]

    def upsert_promise(self, promise: Promise, branch_id: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO promises VALUES (?, ?, ?, ?, ?, ?)", (promise.promise_id, branch_id, promise.party_id, promise.status, promise.deadline, canonical_json(promise.to_json())))
            self.journal("promise", promise.to_json(), promise.promise_id)

    def list_promises(self, branch_id: str | None = None, status: str | None = None) -> list[Promise]:
        clauses, params = [], []
        if branch_id is not None:
            clauses.append("branch_id = ?"); params.append(branch_id)
        if status is not None:
            clauses.append("status = ?"); params.append(status)
        query = "SELECT body FROM promises" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY deadline"
        return [Promise(**json.loads(r["body"])) for r in self.connection.execute(query, params)]

    # ----- experiments & models -----
    def insert_experiment(self, experiment: Experiment) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?)", (experiment.experiment_id, experiment.branch_id, experiment.checkpoint_id, experiment.policy_version, experiment.created_at, canonical_json(experiment.to_json())))
            self.journal("experiment", experiment.to_json(), experiment.experiment_id)

    def update_experiment(self, experiment: Experiment) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE experiments SET body = ? WHERE experiment_id = ?", (canonical_json(experiment.to_json()), experiment.experiment_id))
            self.journal("experiment.updated", experiment.to_json(), experiment.experiment_id)

    def get_experiment(self, experiment_id: str) -> Experiment | None:
        row = self.connection.execute("SELECT body FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()
        return Experiment(**json.loads(row["body"])) if row else None

    def list_experiments(self, branch_id: str | None = None) -> list[Experiment]:
        rows = self.connection.execute("SELECT body FROM experiments ORDER BY created_at") if branch_id is None else self.connection.execute("SELECT body FROM experiments WHERE branch_id = ? ORDER BY created_at", (branch_id,))
        return [Experiment(**json.loads(r["body"])) for r in rows]

    def insert_model_version(self, model: ModelVersion) -> None:
        with self.transaction() as conn:
            conn.execute("INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?)", (model.model_id, model.version, model.information_mode, model.release_state.value, model.artifact_hash, model.created_at, canonical_json(model.to_json())))
            self.journal("model.version", model.to_json(), f"{model.model_id}:{model.version}")

    def update_model_version(self, model: ModelVersion) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE model_versions SET release_state = ?, body = ? WHERE model_id = ? AND version = ?", (model.release_state.value, canonical_json(model.to_json()), model.model_id, model.version))
            self.journal("model.release_state", {"model_id": model.model_id, "version": model.version, "release_state": model.release_state.value}, f"{model.model_id}:{model.version}")

    def list_model_versions(self, model_id: str | None = None) -> list[ModelVersion]:
        from .records import ReleaseState
        rows = self.connection.execute("SELECT body FROM model_versions ORDER BY created_at") if model_id is None else self.connection.execute("SELECT body FROM model_versions WHERE model_id = ? ORDER BY created_at", (model_id,))
        result = []
        for r in rows:
            data = json.loads(r["body"])
            data["release_state"] = ReleaseState(data["release_state"])
            result.append(ModelVersion(**data))
        return result

    # ----- settings -----
    def put_setting(self, key: str, body: Any) -> int:
        with self.transaction() as conn:
            row = conn.execute("SELECT version FROM settings WHERE key = ?", (key,)).fetchone()
            version = (int(row["version"]) + 1) if row else 1
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?, ?)", (key, version, canonical_json(body), utc_now()))
            self.journal("setting", {"key": key, "version": version, "body": body}, key)
            return version

    def get_setting(self, key: str) -> tuple[Any, int] | None:
        row = self.connection.execute("SELECT body, version FROM settings WHERE key = ?", (key,)).fetchone()
        return (json.loads(row["body"]), int(row["version"])) if row else None

    # ----- locks -----
    @staticmethod
    def _lock_instant(now: str | None):
        """The wall-clock reading a lock is stamped and judged by: the caller's clock, or the system clock.

        A lock's staleness is wall-clock, not in-game time (a crashed process
        stops heartbeating whatever the game does), but the caller may inject
        its own clock so that "this heartbeat is older than the threshold" is
        expressible without waiting for it. A reading without an offset is
        read as UTC; a malformed one is a caller bug, not a stale lock.
        """
        import datetime as dt
        moment = dt.datetime.now(dt.timezone.utc) if now is None else dt.datetime.fromisoformat(now)
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.timezone.utc)

    def acquire_lock(self, name: str, owner: str, *, stale_after_seconds: float = 300.0, now: str | None = None) -> bool:
        """Manager lock: prevents two bot instances from controlling one game.

        ``now`` is an ISO-8601 wall-clock reading; it defaults to the system
        clock, so existing callers are unaffected. A foreign lock is taken
        over only when its last heartbeat is at least ``stale_after_seconds``
        older than ``now`` (spec 4.2, 12.4).
        """
        moment = self._lock_instant(now)
        stamp = moment.isoformat()
        with self.transaction() as conn:
            row = conn.execute("SELECT owner, heartbeat_at FROM locks WHERE name = ?", (name,)).fetchone()
            if row and row["owner"] != owner:
                if (moment - self._lock_instant(row["heartbeat_at"])).total_seconds() < stale_after_seconds:
                    return False
            conn.execute("INSERT OR REPLACE INTO locks VALUES (?, ?, ?, ?)", (name, owner, stamp, stamp))
            return True

    def heartbeat_lock(self, name: str, owner: str, *, now: str | None = None) -> bool:
        """Refresh the owner's lock. ``now`` is the same injectable wall-clock reading as :meth:`acquire_lock`."""
        with self.transaction() as conn:
            row = conn.execute("SELECT owner FROM locks WHERE name = ?", (name,)).fetchone()
            if not row or row["owner"] != owner:
                return False
            conn.execute("UPDATE locks SET heartbeat_at = ? WHERE name = ?", (self._lock_instant(now).isoformat(), name))
            return True

    def release_lock(self, name: str, owner: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))

    def lock_owner(self, name: str) -> str | None:
        row = self.connection.execute("SELECT owner FROM locks WHERE name = ?", (name,)).fetchone()
        return row["owner"] if row else None
