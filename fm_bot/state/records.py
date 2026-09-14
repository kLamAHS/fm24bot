"""Core records (design specification section 5.3).

Every record is a plain dataclass with ``to_json``/``from_json`` so it can be
journaled, stored and shown to the operator without solver or database
terminology leaking into the interface layer.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from .identity import new_id, utc_now
from .status import ValueStatus
from .units import Money, Period


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(value: Any):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Money):
        return value.to_json()
    if dataclasses.is_dataclass(value):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class QualityStatus(str, Enum):
    VALIDATED = "validated"          # bridge returned 200 and the schema matched
    SCHEMA_MISMATCH = "schema_mismatch"
    UNAVAILABLE = "unavailable"      # 503, disconnect, or explicit unavailable state
    ERROR = "error"                  # transport or HTTP error
    UI_UNVERIFIED = "ui_unverified"  # UI adapter observation without corroboration
    UI_VERIFIED = "ui_verified"


class Visibility(str, Enum):
    VISIBLE = "visible"          # demonstrated visible to the manager at that time
    PRIVILEGED = "privileged"    # actual memory values (e.g. attributes without a scouting mask)
    UNKNOWN = "unknown"          # not established; unavailable in manager-visible mode


@dataclass
class Observation:
    """One raw evidence record. Immutable once journaled."""

    observation_id: str
    source: str                      # e.g. "bridge:/squad", "ui:screen.squad"
    payload_hash: str
    career_id: str | None
    branch_id: str | None
    session_id: str | None
    game_date: str | None
    game_time: str | None
    observed_at: str
    schema_version: str
    visibility: Visibility
    quality: QualityStatus
    http_status: int | None = None
    error: str | None = None
    sequence: int = 0
    payload: Any = None              # kept in the blob store by hash; may be None when loaded lazily

    @classmethod
    def create(cls, source: str, payload: Any, *, career_id, branch_id, session_id, game_date, game_time, schema_version, visibility=Visibility.PRIVILEGED, quality=QualityStatus.VALIDATED, http_status=None, error=None, sequence=0) -> "Observation":
        return cls(new_id("obs"), source, payload_hash(payload), career_id, branch_id, session_id, game_date, game_time, utc_now(), schema_version, visibility, quality, http_status, error, sequence, payload)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["visibility"] = self.visibility.value
        data["quality"] = self.quality.value
        return data


class ConsistencyStatus(str, Enum):
    CONSISTENT = "consistent"
    TIME_CHANGED = "time_changed"
    SESSION_CHANGED = "session_changed"
    IDENTITY_CHANGED = "identity_changed"
    FIELD_CHANGED = "field_changed"
    DISCONNECTED = "disconnected"
    BUILD_UNSUPPORTED = "build_unsupported"
    ROUTE_UNAVAILABLE = "route_unavailable"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


@dataclass
class DecisionSnapshot:
    snapshot_id: str
    observation_ids: list[str]
    consistency: ConsistencyStatus
    consistency_reasons: list[str]
    capabilities: list[str]
    unresolved: list[str]
    entity_versions: dict[str, str]        # route or entity -> payload hash
    information_mode: str
    career_id: str | None
    branch_id: str | None
    session_id: str | None
    game_date: str | None
    game_time: str | None
    collected_at: str = field(default_factory=utc_now)
    routes: dict[str, Any] = field(default_factory=dict)   # route -> payload (data part)
    attempts: int = 1
    continuity: dict[str, Any] | None = None               # ContinuityResult.to_json()
    requirements: dict[str, Any] = field(default_factory=dict)
    manager_id: int | None = None
    club_id: int | None = None

    @property
    def valid(self) -> bool:
        return self.consistency is ConsistencyStatus.CONSISTENT

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["consistency"] = self.consistency.value
        return data


@dataclass
class PlayerState:
    player_id: int
    name: str
    attributes: dict[str, int]             # validated 1..20 values, privileged unless masked
    attribute_units: str
    positions: list[str]
    position_ratings: dict[str, int]
    employment: list[dict[str, Any]]
    morale: str | None
    morale_status: str
    condition: float | None
    match_sharpness: float | None
    readiness_status: str                   # current / stale / unknown
    eligibility: dict[str, Any]             # separately sourced; {"status": ..., "source": ..., ...}
    age: int | None = None
    source_observation_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class Certainty(str, Enum):
    OBSERVED_COMMITTED = "observed_committed"
    CONDITIONAL = "conditional"
    FORECAST = "forecast"
    UNKNOWN = "unknown"


class MovementKind(str, Enum):
    RECEIPT = "receipt"
    PAYMENT = "payment"


@dataclass
class FinancialCommitment:
    commitment_id: str
    counterparty: str
    kind: MovementKind
    amount: Money
    due_date: str                       # ISO date of the first (or only) payment
    recurrence: Period
    end_date: str | None                # last possible payment date for recurring items
    trigger: str | None                 # condition text for conditional items
    payer: str                          # "club" or counterparty name
    certainty: Certainty
    source: str                         # observation id / contract id
    version: int = 1
    category: str = "other"             # wages, transfer_fee, bonus, loan_fee, ...
    included_in_aggregate: str | None = None   # e.g. "payroll_spending_weekly" to prevent double counting
    probability: float | None = None    # for conditional/forecast items when modelled

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["amount"] = self.amount.to_json()
        data["kind"] = self.kind.value
        data["recurrence"] = self.recurrence.value
        data["certainty"] = self.certainty.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FinancialCommitment":
        data = dict(data)
        data["amount"] = Money.from_json(data["amount"])
        data["kind"] = MovementKind(data["kind"])
        data["recurrence"] = Period(data["recurrence"])
        data["certainty"] = Certainty(data["certainty"])
        return cls(**data)


@dataclass
class CompetitionContext:
    competition_id: int
    competition_name: str
    stage: str | None
    fixture_identity: str | None          # e.g. "2024-02-17T15:00 home:742 away:1234 comp:14"
    squad_rules: dict[str, Any]           # registration limits, homegrown, etc. or {"status": "missing"}
    substitution_rules: dict[str, Any]    # {"named_substitutes": 7, "substitutions": 5} or {"status": "missing"}
    deadlines: list[dict[str, Any]]
    source: str
    rules_version: int = 1

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Promise:
    promise_id: str
    party_id: int
    party_kind: str                       # player / staff
    commitment: str                       # exact observed terms
    deadline: str | None
    progress: str
    consequences: str | None
    source_choice: str | None
    confidence: str                       # observed / inferred
    source: str
    created_at: str = field(default_factory=utc_now)
    playing_time_minutes_per_fixture: int | None = None
    status: str = "open"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    decision_id: str
    objective_version: str
    snapshot_id: str
    candidates: list[dict[str, Any]]
    constraints: list[dict[str, Any]]
    forecasts: list[dict[str, Any]]           # each with intervals and status
    selected: dict[str, Any] | None
    reasons: list[str]
    components: dict[str, Any] = field(default_factory=dict)   # objective components reported separately
    information_mode: str = "bridge_observed"
    model_versions: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    kind: str = "generic"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class ActionState(str, Enum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    OUTSIDE_SCOPE = "OUTSIDE_SCOPE"
    QUEUED = "QUEUED"
    EXECUTING = "EXECUTING"
    FAILED = "FAILED"
    VERIFYING = "VERIFYING"
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    RECONCILING = "RECONCILING"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = {ActionState.CONFIRMED, ActionState.FAILED, ActionState.OUTSIDE_SCOPE, ActionState.EXPIRED, ActionState.CANCELLED}
IN_FLIGHT_STATES = {ActionState.EXECUTING, ActionState.VERIFYING}

TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.PROPOSED: {ActionState.VALIDATED, ActionState.OUTSIDE_SCOPE, ActionState.CANCELLED, ActionState.EXPIRED},
    ActionState.VALIDATED: {ActionState.QUEUED, ActionState.OUTSIDE_SCOPE, ActionState.CANCELLED, ActionState.EXPIRED},
    ActionState.QUEUED: {ActionState.EXECUTING, ActionState.CANCELLED, ActionState.EXPIRED},
    ActionState.EXECUTING: {ActionState.VERIFYING, ActionState.FAILED, ActionState.UNCERTAIN},
    ActionState.VERIFYING: {ActionState.CONFIRMED, ActionState.UNCERTAIN, ActionState.FAILED},
    ActionState.UNCERTAIN: {ActionState.RECONCILING},
    ActionState.RECONCILING: {ActionState.CONFIRMED, ActionState.FAILED, ActionState.UNCERTAIN, ActionState.QUEUED},
    ActionState.OUTSIDE_SCOPE: set(),
    ActionState.FAILED: set(),
    ActionState.CONFIRMED: set(),
    ActionState.EXPIRED: set(),
    ActionState.CANCELLED: set(),
}


@dataclass
class ActionIntent:
    action_id: str
    kind: str                             # e.g. "select_validated_tactic"
    authority_scope: str                  # e.g. "tactics.select"
    career_id: str
    branch_id: str
    decision_snapshot_id: str
    targets: dict[str, Any]
    parameters: dict[str, Any]
    preconditions: list[str]
    required_capabilities: list[str]
    expires_on: str                       # "relevant_state_change" or ISO time
    verification: str                     # verification plan name
    idempotency_key: str
    schema_version: int = 1
    state: ActionState = ActionState.PROPOSED
    state_reason: str | None = None
    entity_versions: dict[str, str] = field(default_factory=dict)   # source object versions checked before execution
    risk_class: str = "consequential"     # "navigation" allows bounded retries; "consequential" never blind retries
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    decision_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ActionIntent":
        data = dict(data)
        data["state"] = ActionState(data["state"])
        return cls(**data)


class ExecutionOutcome(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass
class ActionResult:
    result_id: str
    action_id: str
    attempt: int
    before_evidence: list[str]            # observation ids
    after_evidence: list[str]
    execution_state: ActionState
    outcome: ExecutionOutcome | None
    confirmed_effect: dict[str, Any] | None
    uncertainty: str | None
    recovery_instruction: str | None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    adapter: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["execution_state"] = self.execution_state.value
        data["outcome"] = self.outcome.value if self.outcome else None
        return data


@dataclass
class Experiment:
    experiment_id: str
    hypothesis: str
    treatment: dict[str, Any]
    controls: dict[str, Any]
    branch_id: str
    checkpoint_id: str
    randomization: dict[str, Any]
    policy_version: str
    outcomes: dict[str, Any] = field(default_factory=dict)
    validity_flags: dict[str, bool] = field(default_factory=dict)
    smallest_useful_effect: float | None = None
    stopping_rule: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class ReleaseState(str, Enum):
    CANDIDATE = "candidate"
    EXPERIMENTAL_ADVISORY = "experimental_advisory"
    RELEASED = "released"
    REJECTED = "rejected"
    RETIRED = "retired"


@dataclass
class ModelVersion:
    model_id: str
    version: str
    feature_schema: dict[str, Any]
    training_lineage: dict[str, Any]        # careers/branches/periods used; must exclude holdouts
    information_mode: str
    build_scope: list[str]
    calibration: dict[str, Any]
    artifact_hash: str
    release_state: ReleaseState
    baseline_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    notes: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["release_state"] = self.release_state.value
        return data
