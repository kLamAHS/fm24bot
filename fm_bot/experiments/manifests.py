"""Run and trial manifests for the laboratory (spec 13.1, EXP 01, BOT 010).

Every laboratory run is written up like a match report that a colleague could
re-run from: which game build and database, which leagues were loaded, the
detail level, which club and manager, which career and branch lineage, the
checkpoint it started from, which policy and model versions made the
decisions, the authority profile in force, the treatment applied, how long it
took on the wall clock and whether the result counts.

A :class:`TrialManifest` sits on top of a run manifest and records the parent
checkpoint and every *known* difference between the trial and that parent.
"Known" is the honest limit: an unrecorded configuration setting is reported
as missing, never assumed equal.

Nothing here is a model or a heuristic; these are bookkeeping records.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from ..state.identity import BranchIdentity, Checkpoint, new_id, utc_now
from ..state.records import payload_hash

MANIFEST_VERSION = "experiments.manifests/1"

# Fields a run manifest must carry before a trial can be counted (spec 13.1).
REQUIRED_RUN_FIELDS: tuple[str, ...] = (
    "build",
    "database_config",
    "loaded_leagues",
    "detail_settings",
    "club_id",
    "manager_id",
    "career_id",
    "branch_id",
    "starting_checkpoint_id",
    "policy_version",
    "authority_profile_version",
    "treatment",
)

# Configuration the bot cannot read from the bridge today. When the operator
# has not supplied it, the manifest carries an explicit status instead of a
# guessed value.
UNRECORDED = "unrecorded"


def unrecorded(reason: str) -> dict[str, str]:
    """An explicit placeholder for configuration nobody has recorded yet."""
    return {"status": UNRECORDED, "reason": reason}


def is_unrecorded(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") == UNRECORDED


@dataclass
class RunManifest:
    """What one laboratory run was, in enough detail to be re-run and audited.

    ``build`` is the bridge's build string; ``build_hash`` is a checksum of the
    game executable when the operator has recorded one, otherwise ``None``
    (unknown, not fabricated). ``parent_chain`` lists branch ids from the root
    production branch down to the parent of ``branch_id`` so lineage is
    readable without a database.
    """

    run_id: str
    experiment_id: str | None
    build: str | None
    build_hash: str | None
    database_config: dict[str, Any] | None
    loaded_leagues: list[str] | dict[str, Any] | None
    detail_settings: dict[str, Any] | None
    club_id: int | None
    manager_id: int | None
    career_id: str | None
    branch_id: str | None
    parent_chain: list[str]
    starting_checkpoint_id: str | None
    policy_version: str | None
    model_versions: dict[str, str]
    authority_profile_version: int | None
    treatment: dict[str, Any] | None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    elapsed_wall_seconds: float | None = None       # measured for this run only; never a throughput claim
    technically_valid: bool | None = None           # None until the run has finished
    validity: dict[str, bool] = field(default_factory=dict)
    invalidity_reasons: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    manifest_version: str = MANIFEST_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunManifest":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def fingerprint(self) -> str:
        """Content hash of the configuration that determines the run (not its timing or result)."""
        keys = [*REQUIRED_RUN_FIELDS, "build_hash", "model_versions", "parent_chain"]
        return payload_hash({k: getattr(self, k) for k in keys})

    def finish(self, *, elapsed_wall_seconds: float, technically_valid: bool, reasons: list[str] | None = None, validity: dict[str, bool] | None = None) -> "RunManifest":
        self.finished_at = utc_now()
        self.elapsed_wall_seconds = elapsed_wall_seconds
        self.technically_valid = technically_valid
        self.invalidity_reasons = list(reasons or [])
        if validity:
            self.validity.update(validity)
        self.validity["technically_valid"] = technically_valid
        return self


def validate(manifest: RunManifest) -> list[str]:
    """List the required fields that are absent (``None``) or explicitly unrecorded.

    An empty list means the run can be counted as a trial. Unrecorded
    configuration is listed so the operator sees exactly what to supply.
    """
    problems: list[str] = []
    for name in REQUIRED_RUN_FIELDS:
        value = getattr(manifest, name)
        if value is None:
            problems.append(f"{name}: missing")
        elif is_unrecorded(value):
            problems.append(f"{name}: {UNRECORDED} ({value.get('reason')})")
        elif name == "treatment" and (not isinstance(value, dict) or not value.get("name")):
            problems.append("treatment: must name the treatment (use {'name': 'control'} for the control arm)")
    if manifest.finished_at is not None and manifest.technically_valid is None:
        problems.append("technically_valid: undecided after the run finished")
    if manifest.manifest_version != MANIFEST_VERSION:
        problems.append(f"manifest_version: {manifest.manifest_version} is not {MANIFEST_VERSION}")
    return problems


def parent_chain(store, branch_id: str | None) -> list[str]:
    """Branch ids from the root of the career down to the parent of ``branch_id``."""
    chain: list[str] = []
    seen: set[str] = set()
    current = store.get_branch(branch_id) if branch_id else None
    while current is not None and current.parent_branch_id and current.parent_branch_id not in seen:
        seen.add(current.parent_branch_id)
        chain.append(current.parent_branch_id)
        current = store.get_branch(current.parent_branch_id)
    chain.reverse()
    return chain


def run_manifest_for(
    *,
    branch: BranchIdentity,
    checkpoint: Checkpoint,
    experiment_id: str | None,
    policy_version: str | None,
    authority_profile_version: int | None,
    treatment: dict[str, Any] | None,
    environment: dict[str, Any] | None = None,
    model_versions: dict[str, str] | None = None,
    parent_chain_ids: list[str] | None = None,
) -> RunManifest:
    """Build a run manifest from registry objects plus the operator-supplied environment.

    ``environment`` may carry ``build_hash``, ``database_config``,
    ``loaded_leagues`` and ``detail_settings``. Anything not supplied is
    recorded as unrecorded and will fail :func:`validate` until provided.
    """
    env = dict(environment or {})
    manifest = checkpoint.manifest
    return RunManifest(
        run_id=new_id("run"),
        experiment_id=experiment_id,
        build=manifest.build,
        build_hash=env.get("build_hash"),
        database_config=env.get("database_config", unrecorded("database/mod configuration not supplied by the operator")),
        loaded_leagues=env.get("loaded_leagues", unrecorded("loaded leagues not supplied by the operator")),
        detail_settings=env.get("detail_settings", unrecorded("simulation detail settings not supplied by the operator")),
        club_id=manifest.club_id,
        manager_id=manifest.manager_id,
        career_id=branch.career_id,
        branch_id=branch.branch_id,
        parent_chain=list(parent_chain_ids or ([branch.parent_branch_id] if branch.parent_branch_id else [])),
        starting_checkpoint_id=checkpoint.checkpoint_id,
        policy_version=policy_version,
        model_versions=dict(model_versions or {}),
        authority_profile_version=authority_profile_version,
        treatment=dict(treatment) if treatment is not None else None,
    )


@dataclass(frozen=True)
class Difference:
    """One known difference between a trial and its parent checkpoint."""

    field: str
    parent: Any
    trial: Any

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field, "parent": self.parent, "trial": self.trial}


# Fields compared when listing differences between a trial run and the
# checkpoint it started from. Configuration is compared against the parent
# run manifest when one exists; otherwise the checkpoint manifest only knows
# build, club, manager and game time.
COMPARED_RUN_FIELDS: tuple[str, ...] = ("build", "build_hash", "database_config", "loaded_leagues", "detail_settings", "club_id", "manager_id", "policy_version", "model_versions", "authority_profile_version", "treatment")


def differences_from_parent(trial_run: RunManifest, parent_checkpoint: Checkpoint, parent_run: RunManifest | None = None) -> list[Difference]:
    """Every known difference between ``trial_run`` and the parent checkpoint.

    Without a parent run manifest only the checkpoint's own identity fields
    can be compared; the treatment is always listed because it is the
    intended difference.
    """
    diffs: list[Difference] = []
    pm = parent_checkpoint.manifest
    if parent_run is None:
        for name, parent_value in (("build", pm.build), ("club_id", pm.club_id), ("manager_id", pm.manager_id)):
            if getattr(trial_run, name) != parent_value:
                diffs.append(Difference(name, parent_value, getattr(trial_run, name)))
        diffs.append(Difference("treatment", None, trial_run.treatment))
        return diffs
    for name in COMPARED_RUN_FIELDS:
        parent_value, trial_value = getattr(parent_run, name), getattr(trial_run, name)
        if parent_value != trial_value:
            diffs.append(Difference(name, parent_value, trial_value))
    return diffs


@dataclass
class TrialManifest:
    """One trial: a run from a parent checkpoint with a declared treatment and its outcomes.

    ``outcomes`` is whatever the outcome collector observed (a defeat, an
    injury, a rejected sale are outcomes). ``technically_valid`` is decided by
    identity and treatment checks only, never by how the club performed.
    """

    trial_id: str
    experiment_id: str
    parent_checkpoint_id: str
    run: RunManifest
    arm: str                                       # "treatment" | "control" | named arm
    differences: list[Difference]
    outcomes: dict[str, Any] = field(default_factory=dict)
    technically_valid: bool | None = None
    invalidity_reasons: list[str] = field(default_factory=list)
    treatment_confirmation: dict[str, Any] | None = None
    created_at: str = field(default_factory=utc_now)
    manifest_version: str = MANIFEST_VERSION

    @property
    def checkpoint_id(self) -> str:
        return self.parent_checkpoint_id

    @property
    def build(self) -> str | None:
        return self.run.build

    @property
    def policy_version(self) -> str | None:
        return self.run.policy_version

    @property
    def treatment(self) -> dict[str, Any] | None:
        return self.run.treatment

    def mapping(self) -> dict[str, Any]:
        """The EXP 01 mapping: checkpoint, build, policy, treatment and outcomes for this trial."""
        return {
            "trial_id": self.trial_id,
            "checkpoint_id": self.parent_checkpoint_id,
            "build": self.run.build,
            "policy_version": self.run.policy_version,
            "treatment": self.run.treatment,
            "outcomes": dict(self.outcomes),
            "technically_valid": self.technically_valid,
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "experiment_id": self.experiment_id,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "run": self.run.to_json(),
            "arm": self.arm,
            "differences": [d.to_json() for d in self.differences],
            "outcomes": dict(self.outcomes),
            "technically_valid": self.technically_valid,
            "invalidity_reasons": list(self.invalidity_reasons),
            "treatment_confirmation": self.treatment_confirmation,
            "created_at": self.created_at,
            "manifest_version": self.manifest_version,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TrialManifest":
        return cls(
            trial_id=data["trial_id"],
            experiment_id=data["experiment_id"],
            parent_checkpoint_id=data["parent_checkpoint_id"],
            run=RunManifest.from_json(data["run"]),
            arm=data["arm"],
            differences=[Difference(d["field"], d["parent"], d["trial"]) for d in data.get("differences", [])],
            outcomes=dict(data.get("outcomes", {})),
            technically_valid=data.get("technically_valid"),
            invalidity_reasons=list(data.get("invalidity_reasons", [])),
            treatment_confirmation=data.get("treatment_confirmation"),
            created_at=data.get("created_at", utc_now()),
            manifest_version=data.get("manifest_version", MANIFEST_VERSION),
        )


def validate_trial(trial: TrialManifest) -> list[str]:
    """Problems that stop a trial from being counted: an incomplete run manifest or missing links."""
    problems = [f"run.{p}" for p in validate(trial.run)]
    if not trial.parent_checkpoint_id:
        problems.append("parent_checkpoint_id: missing")
    if not trial.experiment_id:
        problems.append("experiment_id: missing")
    if trial.run.finished_at is not None and trial.technically_valid is None:
        problems.append("technically_valid: undecided after the run finished")
    return problems
