"""Laboratory save/restore and treatment runner (spec 4.1, 13.1, 13.2, BOT 010).

The laboratory plays "what if" from an immutable starting checkpoint: fork a
laboratory branch, restore the save, apply one declared treatment through the
UI adapter, let the game run on, and collect what happened. The read-only
bridge alone cannot do any of this, so the runner refuses to start unless a
validated save/restore workflow, a UI action adapter and explicit career
registration are all available; the refusal is a
:class:`MissingCapabilityReport` naming what is missing.

Two kinds of invalidity are kept apart. A trial is *technically invalid* when
the restored save is not the checkpoint it claims to be, when the treatment
could not be confirmed as applied, or when progression failed. A defeat, an
injury, a rejected sale or an unhappy board is an *outcome* and is recorded as
such; it never invalidates a trial for being inconvenient.

Save restoration is laboratory-only. Restoring onto a production branch
raises before anything is sent to the game. One game instance is assumed: a
store lock serialises trials.

A learned simulator (:class:`SimulatedRollout`) is a separate approximation.
It writes to the journal under the kind ``simulation`` and never produces an
Experiment record or a trial manifest, so it can never be mistaken for a run
of the actual game.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Protocol

from ..execution.adapter import ANY_SCREEN, RISK_CONSEQUENTIAL, STEP_DONE, UIStep
from ..rules.capabilities import CapabilityRegistry
from ..state.identity import BranchIdentity, BranchKind, CareerIdentity, CareerRegistry, Checkpoint, new_id, utc_now
from ..state.records import DecisionSnapshot, Experiment
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from ..state.status import MissingCapabilityReport
from .manifests import RunManifest, TrialManifest, differences_from_parent, parent_chain, run_manifest_for

RUNNER_VERSION = "experiments.runner/1"

LAB_ACTION_KIND = "lab.restore_checkpoint"
LAB_REQUIRED_CAPABILITIES: tuple[str, ...] = ("save_restore", "ui_action_adapter", "career_registration")
LAB_LOCK_NAME = "laboratory.game_instance"
LAB_LOCK_STALE_SECONDS = 3600.0     # a budget for one trial's wall time before a stale lock may be taken over

JOURNAL_TRIAL = "laboratory.trial"
JOURNAL_RESTORE = "laboratory.restore"
JOURNAL_TREATMENT = "laboratory.treatment"
JOURNAL_SIMULATION = "simulation"

INVALID_RESTORE_FAILED = "restore_failed"
INVALID_WRONG_SAVE_IDENTITY = "wrong_save_identity"
INVALID_SNAPSHOT = "snapshot_invalid"
INVALID_TREATMENT_NOT_APPLIED = "treatment_not_applied"
INVALID_PROGRESSION_FAILED = "progression_failed"
INVALID_OUTCOMES_UNAVAILABLE = "outcomes_unavailable"


class LaboratoryError(RuntimeError):
    """The runner refused to proceed for a structural reason (not a trial outcome)."""


class ProductionRestoreRefused(LaboratoryError):
    """Restoring a save onto a production branch is never allowed."""


class LaboratoryBusy(LaboratoryError):
    """Another trial holds the single game instance."""


# ---------------------------------------------------------------------------
# Results exchanged with the treatment, progression and outcome callables
# ---------------------------------------------------------------------------


@dataclass
class RestoreResult:
    ok: bool
    checkpoint_id: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TreatmentApplication:
    """What the treatment applier confirms. ``applied`` must rest on a readback, not on having sent input."""

    applied: bool
    confirmation: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProgressionResult:
    ok: bool
    game_date_after: str | None = None
    game_time_after: str | None = None
    steps: int = 0
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrialContext:
    """Everything a treatment applier, progression or outcome collector may use."""

    store: Any
    adapter: Any
    collector: SnapshotCollector
    career: CareerIdentity
    branch: BranchIdentity                  # the laboratory branch created for this trial
    checkpoint: Checkpoint                  # the immutable starting checkpoint
    experiment: Experiment
    snapshot: DecisionSnapshot | None = None   # the post-restore snapshot once available


@dataclass
class TrialReport:
    trial: TrialManifest
    run: RunManifest
    branch: BranchIdentity
    experiment: Experiment
    technically_valid: bool
    invalidity_reasons: list[str]
    outcomes: dict[str, Any]
    restore: RestoreResult | None = None
    treatment: TreatmentApplication | None = None
    progression: ProgressionResult | None = None

    def to_json(self) -> dict[str, Any]:
        return {"trial": self.trial.to_json(), "branch_id": self.branch.branch_id, "technically_valid": self.technically_valid, "invalidity_reasons": list(self.invalidity_reasons), "outcomes": dict(self.outcomes)}


TreatmentApplier = Callable[[TrialContext, dict[str, Any]], TreatmentApplication]
Progression = Callable[[TrialContext], ProgressionResult]
OutcomeCollector = Callable[[TrialContext], dict[str, Any]]


# ---------------------------------------------------------------------------
# Restore workflow
# ---------------------------------------------------------------------------


class CheckpointRestorer(Protocol):
    """Loads the save recorded by a checkpoint onto a laboratory branch."""

    name: str

    def restore(self, checkpoint: Checkpoint, branch: BranchIdentity) -> RestoreResult: ...


class AdapterRestorer:
    """Restore through the UI adapter's ``load_save`` action.

    This is the shape of the workflow, not a validated one: no adapter in the
    tree implements ``load_save`` yet, so the step is refused and the restore
    reports failure. The runner then marks the trial technically invalid
    rather than pretending the save was loaded.
    """

    name = "adapter_restorer"

    def __init__(self, adapter):
        self.adapter = adapter

    def restore(self, checkpoint: Checkpoint, branch: BranchIdentity) -> RestoreResult:
        path = checkpoint.manifest.path
        if not path:
            return RestoreResult(False, checkpoint.checkpoint_id, error="checkpoint manifest has no save path")
        step = UIStep(f"restore-{checkpoint.checkpoint_id}", "load_save", ANY_SCREEN, {"path": path, "checksum_sha256": checkpoint.manifest.checksum_sha256}, RISK_CONSEQUENTIAL)
        result = self.adapter.perform(step)
        return RestoreResult(result.ok and result.status == STEP_DONE, checkpoint.checkpoint_id, {"step": result.to_json()}, None if result.ok else (result.error or result.status))


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class LaboratoryRunner:
    """Runs one trial at a time on one game instance, laboratory branches only."""

    def __init__(self, store, registry: CareerRegistry, capabilities: CapabilityRegistry, adapter, collector: SnapshotCollector, *, restorer: CheckpointRestorer | None = None, owner_id: str | None = None, clock: Callable[[], float] = time.perf_counter):
        self.store = store
        self.registry = registry
        self.capabilities = capabilities
        self.adapter = adapter
        self.collector = collector
        self.restorer = restorer or AdapterRestorer(adapter)
        self.owner_id = owner_id or new_id("lab")
        self.clock = clock

    # ----- gates -----
    def readiness(self) -> MissingCapabilityReport:
        """Which laboratory prerequisites are missing. Blocked means no rollout is attempted."""
        report = self.capabilities.check(LAB_ACTION_KIND, extra=LAB_REQUIRED_CAPABILITIES)
        report.blocked_action = "laboratory.run_trial"
        if self.adapter is not None and "ui_action_adapter" not in (self.adapter.capabilities() or []):
            report.add("ui_action_adapter", f"adapter {getattr(self.adapter, 'name', '?')!r} does not verifiably provide ui_action_adapter")
        return report

    def restore_checkpoint(self, branch: BranchIdentity, checkpoint: Checkpoint) -> RestoreResult:
        """Restore ``checkpoint`` onto ``branch``. Production branches are refused before any input is sent."""
        if branch.kind is not BranchKind.LABORATORY:
            raise ProductionRestoreRefused(f"branch {branch.branch_id} is {branch.kind.value}; save restoration is laboratory-only")
        report = self.readiness()
        if report.blocked:
            return RestoreResult(False, checkpoint.checkpoint_id, {"missing_capabilities": report.to_json()}, "laboratory capabilities missing: " + ", ".join(report.missing))
        result = self.restorer.restore(checkpoint, branch)
        self.store.journal(JOURNAL_RESTORE, {"branch_id": branch.branch_id, "checkpoint_id": checkpoint.checkpoint_id, "restorer": getattr(self.restorer, "name", "?"), "result": result.to_json()}, branch.branch_id)
        return result

    # ----- identity after restore -----
    def _verify_identity(self, career: CareerIdentity, checkpoint: Checkpoint, snapshot: DecisionSnapshot) -> list[str]:
        """Wrong-save checks: build, manager, club and the checkpoint's game time must all match."""
        status = getattr(self.collector.client, "last_status", None) or {}
        _, problems = self.registry.matches_registration(career, status, snapshot.manager_id, snapshot.club_id)
        manifest = checkpoint.manifest
        if (snapshot.manager_id, snapshot.club_id) != (manifest.manager_id, manifest.club_id):
            problems.append(f"manager/club {snapshot.manager_id}/{snapshot.club_id} differ from checkpoint {manifest.manager_id}/{manifest.club_id}")
        if snapshot.game_date != manifest.game_date:
            problems.append(f"game date {snapshot.game_date} differs from checkpoint {manifest.game_date}; a different save was restored")
        elif manifest.game_time is not None and snapshot.game_time is not None and snapshot.game_time != manifest.game_time:
            problems.append(f"game time {snapshot.game_time} differs from checkpoint {manifest.game_time}")
        return problems

    def _collect(self, career: CareerIdentity, branch: BranchIdentity, routes: list[str] | None = None) -> DecisionSnapshot:
        return self.collector.collect(SnapshotRequirements(routes=list(routes or []), label="laboratory"), CollectionContext(career.career_id, branch.branch_id, lineage_confirmed=True))

    # ----- the trial -----
    def run_trial(self, experiment: Experiment, treatment_applier: TreatmentApplier, progression: Progression, outcome_collector: OutcomeCollector, *, environment: dict[str, Any] | None = None, authority_profile_version: int | None = None, model_versions: dict[str, str] | None = None, arm: str = "treatment", observe_routes: list[str] | None = None) -> TrialReport | MissingCapabilityReport:
        """Fork, restore, treat, progress and collect one trial of ``experiment``.

        ``experiment.branch_id`` / ``experiment.checkpoint_id`` name the parent
        branch and the immutable starting checkpoint. The trial always runs on
        a fresh laboratory branch forked from them. Returns a
        :class:`MissingCapabilityReport` instead of running when the
        laboratory prerequisites are missing.
        """
        report = self.readiness()
        if report.blocked:
            return report
        parent = self.store.get_branch(experiment.branch_id)
        checkpoint = self.store.get_checkpoint(experiment.checkpoint_id)
        if parent is None or checkpoint is None:
            raise LaboratoryError(f"experiment {experiment.experiment_id} references an unregistered branch or checkpoint")
        career = self.store.get_career(parent.career_id)
        if not self.store.acquire_lock(LAB_LOCK_NAME, self.owner_id, stale_after_seconds=LAB_LOCK_STALE_SECONDS):
            raise LaboratoryBusy(f"game instance held by {self.store.lock_owner(LAB_LOCK_NAME)}; one instance is assumed")
        started = self.clock()
        try:
            branch, _fork_checkpoint = self.registry.fork_branch(parent, checkpoint.manifest, kind=BranchKind.LABORATORY, label=f"trial:{experiment.experiment_id}")
            if self.store.get_experiment(experiment.experiment_id) is None:
                self.store.insert_experiment(experiment)
            run = run_manifest_for(branch=branch, checkpoint=checkpoint, experiment_id=experiment.experiment_id, policy_version=experiment.policy_version, authority_profile_version=authority_profile_version, treatment=experiment.treatment, environment=environment, model_versions=model_versions, parent_chain_ids=parent_chain(self.store, branch.branch_id))
            run.notes["fork_checkpoint_id"] = _fork_checkpoint.checkpoint_id
            context = TrialContext(self.store, self.adapter, self.collector, career, branch, checkpoint, experiment)
            reasons: list[str] = []
            validity: dict[str, bool] = {}
            outcomes: dict[str, Any] = {}
            restore = treatment = progress = None

            restore = self.restore_checkpoint(branch, checkpoint)
            validity["restored"] = restore.ok
            if not restore.ok:
                reasons.append(f"{INVALID_RESTORE_FAILED}: {restore.error}")
            else:
                snapshot = self._collect(career, branch, observe_routes)
                context.snapshot = snapshot
                if not snapshot.valid:
                    validity["identity_confirmed"] = False
                    reasons.append(f"{INVALID_SNAPSHOT}: {snapshot.consistency.value}: " + "; ".join(snapshot.consistency_reasons))
                else:
                    identity_problems = self._verify_identity(career, checkpoint, snapshot)
                    validity["identity_confirmed"] = not identity_problems
                    if identity_problems:
                        reasons.append(f"{INVALID_WRONG_SAVE_IDENTITY}: " + "; ".join(identity_problems))
            if not reasons:
                treatment = self._apply_treatment(context, treatment_applier)
                validity["treatment_applied"] = treatment.applied
                if not treatment.applied:
                    reasons.append(f"{INVALID_TREATMENT_NOT_APPLIED}: {treatment.error or 'no confirmation'}")
            if not reasons:
                progress = progression(context)
                validity["progressed"] = progress.ok
                if not progress.ok:
                    reasons.append(f"{INVALID_PROGRESSION_FAILED}: {progress.error or 'no detail'}")
            if not reasons:
                outcomes = dict(outcome_collector(context) or {})
                validity["outcomes_collected"] = bool(outcomes)
                if not outcomes:
                    reasons.append(f"{INVALID_OUTCOMES_UNAVAILABLE}: outcome collector returned nothing")
            technically_valid = not reasons
            run.finish(elapsed_wall_seconds=self.clock() - started, technically_valid=technically_valid, reasons=reasons, validity=validity)
            trial = TrialManifest(new_id("trial"), experiment.experiment_id, checkpoint.checkpoint_id, run, arm, differences_from_parent(run, checkpoint), outcomes, technically_valid, list(reasons), treatment.to_json() if treatment else None)
            self._record(experiment, trial)
            return TrialReport(trial, run, branch, experiment, technically_valid, list(reasons), outcomes, restore, treatment, progress)
        finally:
            self.store.release_lock(LAB_LOCK_NAME, self.owner_id)

    def _apply_treatment(self, context: TrialContext, applier: TreatmentApplier) -> TreatmentApplication:
        try:
            application = applier(context, dict(context.experiment.treatment))
        except Exception as exc:  # the applier failing is a technical fault, recorded as such
            application = TreatmentApplication(False, {}, f"{type(exc).__name__}: {exc}")
        self.store.journal(JOURNAL_TREATMENT, {"branch_id": context.branch.branch_id, "experiment_id": context.experiment.experiment_id, "treatment": context.experiment.treatment, "application": application.to_json()}, context.branch.branch_id)
        return application

    def _record(self, experiment: Experiment, trial: TrialManifest) -> None:
        """Append this trial's outcomes and validity to the stored experiment; earlier trials are never overwritten."""
        stored = self.store.get_experiment(experiment.experiment_id)
        if stored is not None:
            for trial_id, outcomes in stored.outcomes.items():
                experiment.outcomes.setdefault(trial_id, outcomes)
            for trial_id, flag in stored.validity_flags.items():
                experiment.validity_flags.setdefault(trial_id, flag)
        experiment.outcomes[trial.trial_id] = dict(trial.outcomes)
        experiment.validity_flags[trial.trial_id] = bool(trial.technically_valid)
        with self.store.transaction():
            self.store.update_experiment(experiment)
            self.store.journal(JOURNAL_TRIAL, trial.to_json(), trial.trial_id)


# ---------------------------------------------------------------------------
# Repeatability of restored runs
# ---------------------------------------------------------------------------


@dataclass
class RepeatabilityReport:
    """Whether repeated restores of one checkpoint replayed the same random behaviour.

    ``effective_units`` is the honest count of independent replicates the
    repeated restores provide: 1 when every run repeated, ``runs`` when every
    run differed, in between otherwise. Identical outcomes mean reload does
    not create independent trials; only varied career starts do.
    """

    runs: int
    distinct_outcomes: int
    verdict: str                 # "repeats" | "varies" | "partially_repeats" | "insufficient_runs"
    effective_units: int
    note: str

    @property
    def independent(self) -> bool:
        return self.verdict == "varies"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def characterize_repeatability(outcomes_from_repeated_restores: list[dict[str, Any]], *, minimum_runs: int = 2) -> RepeatabilityReport:
    """Compare outcomes from restoring the same checkpoint several times without any treatment.

    Do not assume a seed setter exists or that reloading creates independent
    outcomes; measure it. Outcomes are compared by canonical content.
    """
    from ..state.records import payload_hash

    runs = len(outcomes_from_repeated_restores)
    if runs < minimum_runs:
        return RepeatabilityReport(runs, runs, "insufficient_runs", 0, f"need at least {minimum_runs} repeated restores to characterise repeatability")
    distinct = len({payload_hash(o) for o in outcomes_from_repeated_restores})
    if distinct == 1:
        return RepeatabilityReport(runs, 1, "repeats", 1, "restored runs repeated the same outcomes; reloads are not independent replicates")
    if distinct == runs:
        return RepeatabilityReport(runs, distinct, "varies", runs, "every restored run differed; reloads may be treated as replicates only after clustering at the checkpoint level")
    return RepeatabilityReport(runs, distinct, "partially_repeats", distinct, "some restored runs repeated; count distinct outcomes as the upper bound on independent units")


# ---------------------------------------------------------------------------
# Learned simulator hook - never an FM experiment
# ---------------------------------------------------------------------------


@dataclass
class SimulationResult:
    simulation_id: str
    experiment_id: str
    simulator_version: str
    outcomes: dict[str, Any]
    is_fm_experiment: bool = False
    kind: str = JOURNAL_SIMULATION
    created_at: str = field(default_factory=utc_now)
    disclaimer: str = "outcome of a learned simulator; not an observation of Football Manager and never a laboratory trial"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class SimulatedRollout:
    """Runs an experiment's treatment through a learned simulator instead of the game.

    The result is an approximation for planning which trials to run. It is
    journaled under the kind ``simulation``, produces no Experiment update, no
    branch, no checkpoint and no trial manifest, and its ``is_fm_experiment``
    flag is always ``False``.
    """

    def __init__(self, store, simulator: Callable[[Experiment], dict[str, Any]], *, simulator_version: str):
        self.store = store
        self.simulator = simulator
        self.simulator_version = simulator_version

    def run(self, experiment: Experiment) -> SimulationResult:
        outcomes = dict(self.simulator(experiment) or {})
        result = SimulationResult(new_id("sim"), experiment.experiment_id, self.simulator_version, outcomes)
        self.store.journal(JOURNAL_SIMULATION, result.to_json(), result.simulation_id)
        return result
