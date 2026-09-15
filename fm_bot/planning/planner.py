"""The shared club planner (spec 4.1, 4.2, 5.4, 6.2, 7.3, 8.2, 16.2 phase 1).

One planner reconciles what the specialist modules propose - selection,
minutes, finance, recruitment, promises, deadlines - into one shared plan
for money, minutes, squad places and deadlines, and writes the result down
as :class:`~fm_bot.state.records.Decision` records the operator can read.

Interfaces (spec 5.4)::

    propose(snapshot, objective, limits)      -> CandidateDecision[]
    evaluate(candidate, scenarios)            -> ForecastAndConstraintReport
    reconcile(candidates, evaluations)        -> SharedPlan
    plan_once(snapshot, ...)                  -> PlanReport

Horizons (spec 6.1: separate horizons coupled through shared resources):

* ``next_decision`` - the immediate fixture's eleven, the Continue gate and
  any mandatory inbox decision;
* ``next_fixtures`` - the 4-6 fixture minutes plan;
* ``rolling_12_months`` - the partial finance model and recruitment /
  renewal packages against the shared wage and fee pools;
* ``succession_3_years`` - a gap list only, never a plan.

Honesty rules enforced here
---------------------------
* An advisory lineup whose eligibility is not verified is labelled
  ``unverified`` and is never turned into a ``submit.lineup`` intent (spec
  7.3). Intents are built through :class:`~fm_bot.execution.lifecycle.IntentFactory`
  only for kinds whose capability check passes; otherwise the report says why.
* Every gated action family gets a :class:`MissingCapabilityReport`; a
  missing subsystem blocks only the actions that need it.
* ``reconcile`` produces one :class:`SharedPlan`: wage headroom and the
  transfer budget are drawn down across renewals and recruits in one order,
  promised minutes are reserved inside the minutes plan, squad places are
  counted against the observed limit, deadlines are listed once, and the
  immediate fixture's eligibility is one shared fact every next-decision
  candidate depends on (unverified is never treated as eligible).
* Forecasts come from :mod:`fm_bot.models.baselines` (or a released model
  resolved through the registry) and stay ``unavailable`` when their inputs
  are missing; objective components are reported separately and a missing
  component leaves the total unavailable (never zero).
* The sporting component converts lineup-objective units to expected points
  through one explicit, versioned heuristic constant
  (:data:`LINEUP_POINTS_PER_OBJECTIVE_UNIT`) rather than adding raw units.

Everything here is a baseline: the weights are the club objective profile's,
the conversions are declared constants, and no learned model is consulted
unless the model registry resolves a released one.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any

from ..execution.executor import unsettled_twin
from ..execution.lifecycle import IntentFactory, LifecycleError, validate
from ..execution.verification import CONTINUE_BOUNDARY, CONTINUE_FROM_DATE, CONTINUE_FROM_TIME
from ..models.baselines import BASELINE_VERSION, Forecast, readiness_forecast, result_forecast, unavailable_forecast
from ..models.registry import ModelRegistry, ReleaseContext
from ..rules.authority import AuthorityMode, AuthorityProfile
from ..rules.capabilities import ACTION_REQUIREMENTS, Capability, CapabilityRegistry, CapabilityStatus
from ..rules.competitions import RulesProfileRegistry
from ..rules.deadlines import ContinueGate, LineupStatus, PendingAction, continue_gate, pending_actions
from ..rules.eligibility import NoEligibilityProvider
from ..state.identity import new_id
from ..state.records import CompetitionContext, Decision, DecisionSnapshot, PlayerState
from ..state.status import MissingCapabilityReport, Observed, ValueStatus
from ..state.store import StoreError
from ..state.units import Money, Period, parse_date
from ..state.visibility import InformationMode
from ..state.views import FinanceView, FixtureView, finance_view, squad_states, tactic_view, upcoming_fixtures
from . import finance as fin
from .lineup import FLAG_ELIGIBILITY_UNVERIFIED, LINEUP_SOLVER_VERSION, LineupPlan, LineupRequest, RoleSlot, eligibility_map, slots_from_tactic_view, solve
from .minutes import HORIZON_MAX_FIXTURES, MINUTES_PLANNER_VERSION, MinutesPlan, Restriction
from .objective import ClubObjectiveProfile, ObjectiveComponents, ObjectiveInputs, default_profile, score_components
from .recruitment import RECRUITMENT_VERSION, CandidatePackage, FinanceInputs, PackageEvaluation, RecruitmentEvaluator, SquadContext, SuccessionGap, TradeoffTable, package_tradeoffs, registration_feasibility, succession_gaps
from .roles import ROLE_WEIGHTS_VERSION

PLANNER_VERSION = "planner-baseline-0.1"

HORIZON_NEXT_DECISION = "next_decision"
HORIZON_NEXT_FIXTURES = "next_fixtures"
HORIZON_ROLLING_12_MONTHS = "rolling_12_months"
HORIZON_SUCCESSION = "succession_3_years"
HORIZONS: tuple[str, ...] = (HORIZON_NEXT_DECISION, HORIZON_NEXT_FIXTURES, HORIZON_ROLLING_12_MONTHS, HORIZON_SUCCESSION)

# Reviewable heuristic constants (versioned with PLANNER_VERSION).
LINEUP_POINTS_PER_OBJECTIVE_UNIT = 1.0     # explicit conversion: one lineup-objective unit over the horizon ~ one expected point (heuristic, not fitted)
YOUNG_PLAYER_AGE = 21                      # development index = share of planned minutes given to players at or under this age
RESULT_FORECAST_WINDOW = 6                 # matches in the rolling points-per-match baseline
MATCH_MINUTES = 90

STATUS_PROPOSED = "proposed"       # executable if validated: capability check passed and the plan is legal and verified
STATUS_ADVISORY = "advisory"       # useful advice; never executed from this record
STATUS_BLOCKED = "blocked"         # a prerequisite (capability, verification, authority) is missing; reasons say which
STATUS_INFEASIBLE = "infeasible"   # the constraints cannot all hold; reasons name the conflict

# Action families whose executable kinds are capability-gated (spec 3.2, 14): one report per family.
GATED_FAMILIES: dict[str, str] = {"selection": "submit.lineup", "tactics": "select_validated_tactic", "training": "set.training", "contracts": "commit.contract", "transfers": "commit.transfer_offer", "progression": "progress.continue", "inbox": "respond.inbox", "match": "match.substitute"}
EXECUTION_MODES = {AuthorityMode.SCOPED_EXECUTION, AuthorityMode.CLUB_AUTONOMY}
KIND_FAMILY: dict[str, str] = {"advise.lineup": "selection", "submit.lineup": "selection", "advise.minutes": "selection", "advise.finance": "board", "commit.transfer_offer": "transfers", "commit.contract": "contracts", "progress.continue": "progression", "respond.inbox": "inbox", "advise.succession": "scouting"}
AUTHORITY_SCOPES: dict[str, str] = {"submit.lineup": "selection.submit_lineup", "progress.continue": "progression.continue", "commit.transfer_offer": "transfers.offer", "commit.contract": "contracts.commit", "respond.inbox": "inbox.respond"}
# The verification plan each executable kind is judged by (spec 12.2). Continue and inbox answers are
# judged by their effect on the game - the in-game clock moved on, the message is no longer pending -
# because a changed screen and a click that returned prove nothing about the club's state.
VERIFICATION_PLANS: dict[str, str] = {"submit.lineup": "lineup_matches_selection", "progress.continue": "game_advanced_past_boundary", "commit.contract": "contract_accepted_with_obligations", "commit.transfer_offer": "contract_accepted_with_obligations", "respond.inbox": "inbox_message_answered"}


# ---------------------------------------------------------------------------
# providers and records
# ---------------------------------------------------------------------------

@dataclass
class Providers:
    """Optional sources the planner consults. Absent providers leave their outputs missing, never assumed.

    ``finance_policy`` may be omitted: when the authority profile carries
    ``min_cash_reserve`` a :class:`~fm_bot.planning.finance.RiskPolicy` is
    derived from it (labelled so); otherwise the reserve rule is unknown.
    ``recovery_per_day`` is the empirical readiness slope for the baseline
    readiness forecast; without it readiness forecasts are unavailable.
    """

    eligibility: Any = field(default_factory=NoEligibilityProvider)
    rules: RulesProfileRegistry | None = None
    promises: Any = None                       # fm_bot.interactions.promises.PromiseLedger
    finance_policy: fin.RiskPolicy | None = None
    scenarios: list[fin.Scenario] | None = None
    regulatory: Observed | None = None
    engine: fin.CashFlowEngine | None = None
    models: ModelRegistry | None = None
    language_model: Any = None                 # None: numeric planners continue, text tasks are reported unavailable
    inbox_text: Any = None
    candidates: list[CandidatePackage] = field(default_factory=list)
    restrictions: list[Restriction] = field(default_factory=list)
    recovery_per_day: float | None = None
    build: str | None = None


@dataclass
class ResourceClaims:
    """What a candidate would consume from the shared pools."""

    weekly_wage: Money | None = None
    guaranteed_fees: Money | None = None
    minutes: dict[int, int] = field(default_factory=dict)
    squad_places: int = 0
    deadline: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"weekly_wage": self.weekly_wage.to_json() if self.weekly_wage else None, "guaranteed_fees": self.guaranteed_fees.to_json() if self.guaranteed_fees else None, "minutes": {str(k): v for k, v in self.minutes.items()}, "squad_places": self.squad_places, "deadline": self.deadline}


@dataclass
class CandidateDecision:
    """One thing the club could decide, with the resources it claims and why it may not be executable."""

    candidate_id: str
    kind: str
    horizon: str
    description: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    claims: ResourceClaims = field(default_factory=ResourceClaims)
    reasons: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    unverified: bool = True
    submittable: bool = False
    targets: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    package: CandidatePackage | None = None

    @property
    def family(self) -> str:
        return KIND_FAMILY.get(self.kind, self.kind.split(".")[0])

    def to_json(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "kind": self.kind, "family": self.family, "horizon": self.horizon, "description": self.description, "status": self.status, "claims": self.claims.to_json(), "reasons": list(self.reasons), "requires": list(self.requires), "unverified": self.unverified, "submittable": self.submittable, "payload": self.payload, "package": self.package.to_json() if self.package else None}


@dataclass
class ConstraintRecord:
    name: str
    status: str                      # pass | fail | unknown
    reason: str
    binding: bool = False
    source: str = "planner"

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "reason": self.reason, "binding": self.binding, "source": self.source}


@dataclass
class ForecastAndConstraintReport:
    """Forecast intervals (explicitly unavailable when inputs are missing), every constraint, and the binding ones."""

    candidate_id: str
    forecasts: list[Forecast]
    constraints: list[ConstraintRecord]
    binding_constraints: list[str]
    components: ObjectiveComponents | None
    feasible: bool | None
    scenarios: list[str] = field(default_factory=list)
    model_versions: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    package_evaluation: PackageEvaluation | None = None
    version: str = PLANNER_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "forecasts": [f.to_json() for f in self.forecasts], "constraints": [c.to_json() for c in self.constraints], "binding_constraints": list(self.binding_constraints), "components": self.components.to_json() if self.components else None, "feasible": self.feasible, "scenarios": list(self.scenarios), "model_versions": dict(self.model_versions), "notes": list(self.notes), "package_evaluation": self.package_evaluation.to_json() if self.package_evaluation else None, "version": self.version}


@dataclass
class SharedPlan:
    """One plan for money, minutes, squad places and deadlines shared by every candidate."""

    money: dict[str, Any]
    minutes: dict[str, Any]
    squad_places: dict[str, Any]
    deadlines: list[dict[str, Any]]
    priorities: dict[str, Any]
    allocations: dict[str, dict[str, Any]]
    conflicts: list[str] = field(default_factory=list)
    eligibility: dict[str, Any] = field(default_factory=dict)
    version: str = PLANNER_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"money": self.money, "minutes": self.minutes, "squad_places": self.squad_places, "deadlines": list(self.deadlines), "priorities": self.priorities, "allocations": self.allocations, "conflicts": list(self.conflicts), "eligibility": dict(self.eligibility), "version": self.version}


@dataclass
class IntentOutcome:
    kind: str
    candidate_id: str
    created: bool
    action_id: str | None
    reason: str
    state: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PartialFinanceModel:
    """Phase 1 finance output: what is observed, what is committed, what is unknown (spec 16.2)."""

    status: str
    view: dict[str, Any]
    payroll: dict[str, Any] | None
    committed_projection: dict[str, Any] | None
    reserve_check: dict[str, Any] | None
    unknown_items: int
    unresolved: list[str]
    notes: list[str] = field(default_factory=list)
    version: str = PLANNER_VERSION

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PlanReport:
    snapshot_id: str
    game_date: str | None
    game_time: str | None
    status: str                                   # planned | blocked
    candidates: list[CandidateDecision]
    evaluations: dict[str, ForecastAndConstraintReport]
    shared_plan: SharedPlan | None
    decisions: list[Decision]
    capability_reports: dict[str, MissingCapabilityReport]
    intents: list[IntentOutcome]
    finance_model: PartialFinanceModel | None
    succession_gaps: list[SuccessionGap]
    continue_gate: ContinueGate | None
    tradeoffs: TradeoffTable | None
    summaries: list[str]
    model_versions: dict[str, str]
    reasons: list[str] = field(default_factory=list)
    version: str = PLANNER_VERSION

    def candidate(self, kind: str) -> CandidateDecision | None:
        return next((c for c in self.candidates if c.kind == kind), None)

    def to_json(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "game_date": self.game_date, "game_time": self.game_time, "status": self.status, "candidates": [c.to_json() for c in self.candidates], "evaluations": {k: v.to_json() for k, v in self.evaluations.items()}, "shared_plan": self.shared_plan.to_json() if self.shared_plan else None, "decisions": [d.to_json() for d in self.decisions], "capability_reports": {k: v.to_json() for k, v in self.capability_reports.items()}, "intents": [i.to_json() for i in self.intents], "finance_model": self.finance_model.to_json() if self.finance_model else None, "succession_gaps": [g.to_json() for g in self.succession_gaps], "continue_gate": self.continue_gate.to_json() if self.continue_gate else None, "tradeoffs": self.tradeoffs.to_json() if self.tradeoffs else None, "summaries": list(self.summaries), "model_versions": dict(self.model_versions), "reasons": list(self.reasons), "version": self.version}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def registry_from_snapshot(snapshot: DecisionSnapshot, *, build: str | None = None) -> CapabilityRegistry:
    """A capability registry from the snapshot's own capability lists (bridge-provided only; nothing bot-side)."""
    registry = CapabilityRegistry(build=build, connected=True)
    for name in snapshot.capabilities or []:
        registry.provide(name, "bridge")
    for name in snapshot.unresolved or []:
        registry.capabilities[name] = Capability(name, CapabilityStatus.UNSUPPORTED, "bridge", "reported unresolved by the bridge")
    return registry


def recent_points(snapshot: DecisionSnapshot) -> tuple[list[float], list[int]]:
    """Points per played match for our club (chronological) and the competitions they were played in."""
    payload = snapshot.routes.get("/fixtures") or {}
    club = snapshot.club_id
    rows = []
    for item in payload.get("fixtures", []) or []:
        if item.get("status") != "played" or item.get("home_score") is None or item.get("away_score") is None:
            continue
        home = (item.get("home") or {}).get("club_id") == club
        away = (item.get("away") or {}).get("club_id") == club
        if not (home or away):
            continue
        gf, ga = (item["home_score"], item["away_score"]) if home else (item["away_score"], item["home_score"])
        rows.append((item.get("date") or "", 3.0 if gf > ga else 1.0 if gf == ga else 0.0, item.get("competition_id")))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows], sorted({r[2] for r in rows if r[2] is not None})


def _fixture_label(fixture: FixtureView, club_id: int | None) -> str:
    venue = "h" if club_id is not None and fixture.is_home(club_id) else "a"
    opponent = fixture.opponent(club_id) if club_id is not None else f"{fixture.home_name} v {fixture.away_name}"
    return f"{opponent} ({venue}), {fixture.competition_name}, {fixture.date}"


def _days_between(earlier: str | None, later: str | None) -> int | None:
    if not earlier or not later:
        return None
    return (parse_date(later) - parse_date(earlier)).days


# ---------------------------------------------------------------------------
# planning context (built once per snapshot)
# ---------------------------------------------------------------------------

@dataclass
class _Context:
    snapshot: DecisionSnapshot
    registry: CapabilityRegistry
    players: list[PlayerState]
    fixtures: list[FixtureView]
    slots: Observed
    rules_contexts: list[CompetitionContext | None]
    eligibility: list[dict[int, Observed]]
    promised_minutes: dict[int, int]
    finance: FinanceView
    ledger: fin.CommitmentLedger | None
    policy: fin.RiskPolicy | None
    finance_inputs: FinanceInputs | None
    squad: SquadContext | None
    minutes_plan: MinutesPlan | None
    submit_lineup: LineupPlan | None
    pending: list[PendingAction]
    gate: ContinueGate | None
    points: list[float]
    leagues: list[int]
    evaluator: RecruitmentEvaluator | None
    current_eleven: list[int]
    lineup_status: LineupStatus


class Planner:
    """Builds, evaluates, reconciles and records candidate decisions for one snapshot at a time."""

    def __init__(self, *, objective_profile: ClubObjectiveProfile | None = None, authority: AuthorityProfile | None = None, capabilities: CapabilityRegistry | None = None, providers: Providers | None = None, store=None):
        self.profile = objective_profile or default_profile()
        self.authority = authority or AuthorityProfile()
        self.capabilities = capabilities
        self.providers = providers or Providers()
        self.store = store
        self._contexts: dict[str, _Context] = {}

    # ----- context -----
    @property
    def execution_requested(self) -> bool:
        return self.authority.mode in EXECUTION_MODES

    def context(self, snapshot: DecisionSnapshot) -> _Context:
        if snapshot.snapshot_id not in self._contexts:
            self._contexts[snapshot.snapshot_id] = self._build_context(snapshot)
        return self._contexts[snapshot.snapshot_id]

    def _policy(self) -> fin.RiskPolicy | None:
        if self.providers.finance_policy is not None:
            return self.providers.finance_policy
        reserve = self.authority.limits.min_cash_reserve
        if reserve is not None:
            return fin.RiskPolicy(reserve, label="derived_from_authority_min_cash_reserve")
        return None

    def _build_context(self, snapshot: DecisionSnapshot) -> _Context:
        p = self.providers
        registry = self.capabilities or registry_from_snapshot(snapshot, build=p.build)
        fixtures = upcoming_fixtures(snapshot, HORIZON_MAX_FIXTURES)
        first = fixtures[0].to_json() if fixtures else None
        players = squad_states(snapshot, eligibility=p.eligibility, fixture=first)
        slots = slots_from_tactic_view(tactic_view(snapshot))
        rules_contexts: list[CompetitionContext | None] = p.rules.contexts_for(fixtures) if p.rules is not None else [None] * len(fixtures)
        loans_supported = registry.supported("loan_contracts")
        eligibility = []
        for fixture in fixtures:
            states = players if fixture is fixtures[0] else squad_states(snapshot, eligibility=p.eligibility, fixture=fixture.to_json())
            eligibility.append(eligibility_map(states, fixture, snapshot_game_date=snapshot.game_date, snapshot_game_time=snapshot.game_time, club_id=snapshot.club_id, loan_contracts_supported=loans_supported))
        promised = p.promises.minutes_commitments(len(fixtures), [f.date for f in fixtures]) if p.promises is not None and fixtures else {}
        view = finance_view(snapshot)
        ledger = fin.CommitmentLedger.from_snapshot(snapshot, view) if "/squad" in snapshot.routes or "/club" in snapshot.routes else None
        policy = self._policy()
        finance_inputs = FinanceInputs(view, ledger, policy, p.scenarios, p.engine, p.regulatory, snapshot.game_date) if ledger is not None and policy is not None else None
        squad = SquadContext(players, slots.value, fixtures, eligibility, promised, list(p.restrictions), {}, snapshot.club_id, rules_contexts) if slots.available and fixtures else None
        evaluator = RecruitmentEvaluator(squad, finance_inputs) if squad is not None else None
        minutes_plan = evaluator.baseline if evaluator is not None else None
        submit = solve(LineupRequest(players, slots.value, fixtures[0], eligibility[0], "submit", competition_rules=rules_contexts[0])) if squad is not None and self.execution_requested else None
        pending = pending_actions(snapshot, p.inbox_text)
        current_eleven = [s.get("player_id") for s in tactic_view(snapshot).get("slots", []) if s.get("player_id") is not None]
        lineup_status = self._lineup_status(fixtures, minutes_plan, submit)
        gate = continue_gate(snapshot, pending, [c for c in rules_contexts if c is not None], lineup_status, authority_mode=self.authority.mode, capabilities=registry)
        points, leagues = recent_points(snapshot)
        return _Context(snapshot, registry, players, fixtures, slots, rules_contexts, eligibility, promised, view, ledger, policy, finance_inputs, squad, minutes_plan, submit, pending, gate, points, leagues, evaluator, current_eleven, lineup_status)

    @staticmethod
    def _lineup_status(fixtures: list[FixtureView], minutes_plan: MinutesPlan | None, submit: LineupPlan | None) -> LineupStatus:
        if not fixtures:
            return LineupStatus("not_required", reasons=["no scheduled fixture"])
        identity = fixtures[0].identity
        if submit is not None and submit.submittable:
            return LineupStatus("verified", identity)
        if minutes_plan is None or minutes_plan.immediate is None:
            return LineupStatus("missing", identity, reasons=["tactic slots unavailable; no eleven could be selected"])
        immediate = minutes_plan.immediate.lineup
        if immediate.status == "infeasible":
            return LineupStatus("infeasible", identity, reasons=[c.message for c in immediate.conflicts])
        unverified = [a.player_id for a in immediate.assignments if FLAG_ELIGIBILITY_UNVERIFIED in a.flags]
        if unverified:
            return LineupStatus("unverified", identity, unverified, [f"eligibility unverified for {len(unverified)} starter(s)"])
        return LineupStatus("verified", identity)

    # ----- propose -----
    def propose(self, snapshot: DecisionSnapshot) -> list[CandidateDecision]:
        """Candidate decisions for every horizon. An inconsistent snapshot yields one blocked candidate."""
        if not snapshot.valid:
            return [CandidateDecision("blocked:snapshot", "observe", HORIZON_NEXT_DECISION, "No plan: the decision snapshot is not consistent", STATUS_BLOCKED, reasons=[f"snapshot {snapshot.consistency.value}: " + "; ".join(snapshot.consistency_reasons)])]
        ctx = self.context(snapshot)
        candidates = [*self._lineup_candidates(ctx), self._continue_candidate(ctx), *self._inbox_candidates(ctx), self._minutes_candidate(ctx), self._finance_candidate(ctx), *self._recruitment_candidates(ctx), self._succession_candidate(ctx)]
        return candidates

    def _lineup_candidates(self, ctx: _Context) -> list[CandidateDecision]:
        club = ctx.snapshot.club_id
        if not ctx.fixtures:
            return [CandidateDecision("lineup:none", "advise.lineup", HORIZON_NEXT_DECISION, "No scheduled fixture in the observed calendar; no eleven to pick", STATUS_BLOCKED, reasons=["no upcoming fixture observed"])]
        fixture = ctx.fixtures[0]
        label = _fixture_label(fixture, club)
        if ctx.minutes_plan is None:
            return [CandidateDecision("lineup:no-tactic", "advise.lineup", HORIZON_NEXT_DECISION, f"Eleven v {label}: tactic slots unavailable", STATUS_BLOCKED, reasons=[f"tactic slots {ctx.slots.status.value}: {ctx.slots.reason}"], requires=ACTION_REQUIREMENTS["advise.lineup"])]
        immediate = ctx.minutes_plan.immediate.lineup
        unverified = [a.player_id for a in immediate.assignments if FLAG_ELIGIBILITY_UNVERIFIED in a.flags]
        status = STATUS_INFEASIBLE if immediate.status == "infeasible" else STATUS_ADVISORY
        reasons = [c.message for c in immediate.conflicts] if immediate.conflicts else []
        if unverified:
            reasons.append(f"eligibility unverified for {len(unverified)} starter(s): advisory only, not for submission")
        reasons.extend(immediate.verification_required)
        advisory = CandidateDecision("lineup:advisory", "advise.lineup", HORIZON_NEXT_DECISION, f"Advisory eleven v {label} ({immediate.status})", status, {"lineup": immediate.to_json(), "fixture": fixture.to_json()}, ResourceClaims(minutes={pid: MATCH_MINUTES for pid in immediate.player_ids}), reasons, ACTION_REQUIREMENTS["advise.lineup"], bool(unverified), False, {"routes": ["/tactics", "/squad"], "player_ids": immediate.player_ids}, lineup_parameters(immediate))
        result = [advisory]
        if self.execution_requested:
            result.append(self._submit_candidate(ctx, fixture, label))
        return result

    def _submit_candidate(self, ctx: _Context, fixture: FixtureView, label: str) -> CandidateDecision:
        plan_ = ctx.submit_lineup
        report = ctx.registry.check("submit.lineup")
        reasons: list[str] = []
        if plan_ is None or not plan_.submittable:
            reasons.append(f"submit-mode selection is {plan_.status if plan_ else 'unavailable'}: " + ("; ".join(c.message for c in plan_.conflicts) if plan_ and plan_.conflicts else "eligibility must be verified for every starter before submission"))
        if report.blocked:
            reasons.append("missing capabilities: " + ", ".join(report.missing))
        status = STATUS_PROPOSED if not reasons else STATUS_BLOCKED
        payload = {"lineup": plan_.to_json() if plan_ else None, "fixture": fixture.to_json()}
        ids = plan_.player_ids if plan_ else []
        return CandidateDecision("lineup:submit", "submit.lineup", HORIZON_NEXT_DECISION, f"Submit eleven v {label}", status, payload, ResourceClaims(minutes={pid: MATCH_MINUTES for pid in ids}), reasons, ACTION_REQUIREMENTS["submit.lineup"], not (plan_ is not None and plan_.submittable), plan_ is not None and plan_.submittable, {"routes": ["/tactics", "/squad"], "player_ids": ids}, lineup_parameters(plan_))

    def _continue_candidate(self, ctx: _Context) -> CandidateDecision:
        """Continue, carrying the in-game moment it moves on from and the boundary it expects (spec 12.2, 12.4, CAL 01).

        Those are exactly what ``game_advanced_past_boundary`` reads later:
        Continue's effect is that the calendar moved, judged from the in-game
        clock of a fresh snapshot against the moment recorded here. A screen
        that changed proves nothing about the calendar, so no target screen
        is named.
        """
        gate = ctx.gate
        reasons = list(gate.blockers)
        if gate.missing_capabilities.blocked:
            reasons.append("missing capabilities: " + ", ".join(gate.missing_capabilities.missing))
        if not self.execution_requested:
            reasons.append(f"authority mode {self.authority.mode.value}: Continue is advice only")
        elif not self.authority.limits.allow_continue:
            reasons.append("profile does not allow pressing Continue")
        boundary = gate.next_boundary.description if gate.next_boundary else "unknown boundary"
        status = STATUS_PROPOSED if not reasons else STATUS_BLOCKED
        deadline = gate.next_boundary.date if gate.next_boundary else None
        parameters = {CONTINUE_BOUNDARY: gate.next_boundary.to_json() if gate.next_boundary else None, CONTINUE_FROM_DATE: ctx.snapshot.game_date, CONTINUE_FROM_TIME: ctx.snapshot.game_time}
        return CandidateDecision("continue", "progress.continue", HORIZON_NEXT_DECISION, f"Advance the calendar to the next boundary: {boundary}", status, {"gate": gate.to_json()}, ResourceClaims(deadline=deadline), reasons, ACTION_REQUIREMENTS["progress.continue"], False, False, {"routes": [r for r in ("/inbox", "/fixtures") if r in ctx.snapshot.routes]}, parameters)

    def _inbox_candidates(self, ctx: _Context) -> list[CandidateDecision]:
        result = []
        for action in ctx.pending:
            if action.resolved or not action.blocks_continue:
                continue
            report = ctx.registry.check("respond.inbox")
            reasons = [f"{action.classification}: {action.description}"]
            if action.requires_capability:
                reasons.append(f"needs capability {action.requires_capability}")
            if report.blocked:
                reasons.append("missing capabilities: " + ", ".join(report.missing))
            if self.providers.language_model is None:
                reasons.append("no language model configured: text decisions are not delegated by default")
            result.append(CandidateDecision(f"inbox:{action.action_id}", "respond.inbox", HORIZON_NEXT_DECISION, f"Resolve inbox item {action.message_id}: {action.event_type or 'unknown type'}", STATUS_BLOCKED, {"pending_action": action.to_json()}, ResourceClaims(deadline=action.deadline_date), reasons, ACTION_REQUIREMENTS["respond.inbox"], True, False, {"routes": ["/inbox"]}, {"message_id": action.message_id}))
        return result

    def _minutes_candidate(self, ctx: _Context) -> CandidateDecision:
        mp = ctx.minutes_plan
        if mp is None:
            return CandidateDecision("minutes", "advise.minutes", HORIZON_NEXT_FIXTURES, "Minutes plan unavailable", STATUS_BLOCKED, reasons=["no fixtures or tactic slots to plan over"], requires=ACTION_REQUIREMENTS["advise.minutes"])
        reasons = list(mp.violations) + list(mp.horizon.get("notes", []))
        status = STATUS_INFEASIBLE if any(f.lineup.status == "infeasible" for f in mp.fixtures) else STATUS_ADVISORY
        unmet = [str(pid) for pid, v in mp.promises.items() if v["status"] == "unmet"]
        description = f"Minutes plan over the next {len(mp.fixtures)} fixture(s); promised minutes {'all planned' if not unmet else 'unmet for ' + ', '.join(unmet)}"
        return CandidateDecision("minutes", "advise.minutes", HORIZON_NEXT_FIXTURES, description, status, {"minutes_plan": mp.to_json()}, ResourceClaims(minutes=dict(mp.cumulative_minutes)), reasons, ACTION_REQUIREMENTS["advise.minutes"], True, False)

    def _finance_candidate(self, ctx: _Context) -> CandidateDecision:
        model = self.finance_model(ctx.snapshot)
        status = STATUS_ADVISORY if model.committed_projection is not None else STATUS_BLOCKED
        return CandidateDecision("finance", "advise.finance", HORIZON_ROLLING_12_MONTHS, f"Partial finance model over 12 months ({model.status})", status, {"finance_model": model.to_json()}, ResourceClaims(), list(model.notes), ACTION_REQUIREMENTS["advise.finance"], True, False)

    @staticmethod
    def finance_window(ctx: _Context) -> Observed:
        """The rolling projection window as ``Observed[(start, end)]``.

        In-game time is the only clock for game commitments: without a game
        date in the snapshot there is no window, so guaranteed fees over the
        horizon are not projected and the corresponding constraint stays
        unknown. No epoch or wall-clock date ever stands in for it.
        """
        if ctx.finance_inputs is not None:
            return Observed.available_value(ctx.finance_inputs.window(), "planner", what="finance_window")
        if ctx.snapshot.game_date:
            start = fin.as_date(ctx.snapshot.game_date)
            return Observed.available_value((start, fin.add_months(start, fin.PROJECTION_MONTHS)), "planner", what="finance_window")
        return Observed.unavailable(ValueStatus.MISSING, "finance_window", "snapshot reports no game date; the rolling projection window cannot be placed in game time", "planner")

    def _recruitment_candidates(self, ctx: _Context) -> list[CandidateDecision]:
        result = []
        window = self.finance_window(ctx)
        for package in self.providers.candidates:
            kind = "commit.contract" if package.kind == "renewal" else "commit.transfer_offer"
            if not package.registration.available and ctx.rules_contexts and ctx.rules_contexts[0] is not None:
                # the caller's package is left untouched; the planner's copy carries the rules-derived answer
                package = replace(package, registration=registration_feasibility(ctx.rules_contexts[0], len(ctx.players), ctx.snapshot.game_date))
            fees = package.guaranteed_fees(*window.value) if window.available else None
            claims = ResourceClaims(package.weekly_wage(), fees, squad_places=0 if package.kind == "renewal" else 1)
            reasons = ["negotiation must produce a confirmed executable offer before any commitment (spec 8.3)"]
            if not window.available:
                reasons.append(f"finance window {window.status.value}: {window.reason}; guaranteed fees not projected")
            if ctx.evaluator is None:
                reasons.append("sporting contribution cannot be computed without tactic slots and fixtures")
            description = f"{package.kind.replace('_', ' ').title()}: {package.player.name} at {claims.weekly_wage}" + (f" for {claims.guaranteed_fees} guaranteed" if claims.guaranteed_fees and not claims.guaranteed_fees.is_zero else "")
            result.append(CandidateDecision(f"recruit:{package.candidate_id}", kind, HORIZON_ROLLING_12_MONTHS, description, STATUS_ADVISORY, {"package": package.to_json(), "finance_window": _window_json(window)}, claims, reasons, ACTION_REQUIREMENTS[kind], True, False, {"routes": [r for r in ("/finances", "/squad") if r in ctx.snapshot.routes]}, {"weekly_wage": claims.weekly_wage.to_json(), "total_fee": claims.guaranteed_fees.to_json() if claims.guaranteed_fees else None, "conditional_total": package.conditional_total().to_json()}, package))
        return result

    def _succession_candidate(self, ctx: _Context) -> CandidateDecision:
        gaps = self.succession(ctx.snapshot)
        if not ctx.slots.available:
            return CandidateDecision("succession", "advise.succession", HORIZON_SUCCESSION, "Succession gaps unavailable: tactic slots not decoded", STATUS_BLOCKED, reasons=[ctx.slots.reason or "no slots"])
        return CandidateDecision("succession", "advise.succession", HORIZON_SUCCESSION, f"Succession gap list over {len(gaps)} position(s) within three seasons", STATUS_ADVISORY, {"gaps": [g.to_json() for g in gaps]}, ResourceClaims(), [g.reason for g in gaps], [], True, False)

    # ----- finance model and succession -----
    def finance_model(self, snapshot: DecisionSnapshot) -> PartialFinanceModel:
        """The Phase 1 'partial finance model': observed aggregates, contract ledger, committed-only projection, unknowns."""
        ctx = self.context(snapshot)
        view = ctx.finance
        view_json = {k: _observed_json(getattr(view, k)) for k in ("balance", "transfer_budget", "wage_budget_weekly", "payroll_spending_weekly")}
        unresolved = [u for u in (snapshot.unresolved or []) if u in ("contract_clauses", "debts", "finance_breakdowns", "scouting_budget", "transfer_target_terms")]
        notes = [f"bridge does not decode: {', '.join(unresolved)}" if unresolved else "no finance-related capability reported unresolved"]
        # The ledger judges which commitments are active by in-game date only; without one nothing is evaluated (spec 5.2).
        payroll = ctx.ledger.weekly_payroll(snapshot.game_date).to_json() if ctx.ledger is not None and snapshot.game_date else None
        if ctx.ledger is not None and not snapshot.game_date:
            notes.append("no game date in the snapshot: active contract lines cannot be determined, payroll ledger not evaluated")
        if payroll is not None and payroll.get("residual", {}).get("status") == "available":
            notes.append(f"payroll aggregate minus contract lines leaves an unexplained residual of {Money.from_json(payroll['residual']['value'])}")
        projection = reserve = None
        unknown = 0
        if ctx.ledger is not None and view.balance.available and snapshot.game_date:
            engine = self.providers.engine or fin.CashFlowEngine()
            proj = engine.project(view.balance.value, snapshot.game_date, ctx.ledger, [fin.Scenario("committed_only", 1)])
            path = proj.paths[0]
            unknown = proj.unknown_count
            projection = {"scenario": "committed_only", "start_balance": str(proj.start_balance), "min_cash": str(path.min_cash), "min_cash_date": path.min_cash_date.isoformat() if isinstance(path.min_cash_date, dt.date) else str(path.min_cash_date), "end_cash": str(path.end_cash), "end_date": proj.end_date.isoformat(), "unknown_items": unknown, "note": "committed obligations only: no forecast receipts, no assumed sales or prize money"}
            if ctx.policy is not None:
                risk = fin.evaluate_risk(proj, ctx.policy)
                reserve = fin.check_cash_reserve(risk).to_json()
                reserve["cvar_shortfall"] = str(risk.cvar_shortfall)
                reserve["policy"] = ctx.policy.label
            else:
                notes.append("no cash reserve policy configured (finance_policy provider or authority min_cash_reserve): reserve rule unknown")
        else:
            notes.append(f"balance {view.balance.status.value}: no projection possible" if not view.balance.available else "no contract ledger: no projection possible")
        return PartialFinanceModel("partial", view_json, payroll, projection, reserve, unknown, unresolved, notes)

    def succession(self, snapshot: DecisionSnapshot) -> list[SuccessionGap]:
        ctx = self.context(snapshot)
        if not ctx.slots.available or not snapshot.game_date:
            return []
        return succession_gaps(ctx.players, ctx.slots.value, snapshot.game_date, club_id=snapshot.club_id)

    # ----- evaluate -----
    def evaluate(self, candidate: CandidateDecision, scenarios: list[fin.Scenario] | None = None, *, snapshot: DecisionSnapshot | None = None) -> ForecastAndConstraintReport:
        """Forecasts, constraints and objective components for one candidate.

        ``scenarios`` are finance scenarios for recruitment candidates (the
        providers' scenarios when omitted). Forecast intervals are the
        baselines' heuristic intervals; unavailable inputs leave a forecast
        unavailable with its reason rather than a number.
        """
        snapshot = snapshot or self._snapshot_for(candidate)
        if snapshot is None or not snapshot.valid:
            return ForecastAndConstraintReport(candidate.candidate_id, [], [ConstraintRecord("snapshot", "fail", "no consistent snapshot", True)], ["snapshot"], None, False)
        ctx = self.context(snapshot)
        if candidate.package is not None:
            return self._evaluate_package(ctx, candidate, scenarios)
        if candidate.kind in ("advise.lineup", "submit.lineup"):
            return self._evaluate_lineup(ctx, candidate)
        if candidate.kind == "advise.minutes":
            return self._evaluate_minutes(ctx, candidate)
        if candidate.kind == "advise.finance":
            return self._evaluate_finance(ctx, candidate)
        if candidate.kind == "progress.continue":
            constraints = [ConstraintRecord(f"blocker:{i}", "fail", b, True, "continue_gate") for i, b in enumerate(ctx.gate.blockers)]
            constraints.extend(ConstraintRecord(f"capability:{name}", "unknown", reason, False, "capability_registry") for name, reason in ctx.gate.missing_capabilities.reasons.items())
            return ForecastAndConstraintReport(candidate.candidate_id, [], constraints, [c.name for c in constraints if c.binding], None, ctx.gate.allowed, notes=["no forecast: pressing Continue changes nothing the baselines forecast"])
        return ForecastAndConstraintReport(candidate.candidate_id, [], [ConstraintRecord(r, "unknown", r, False) for r in candidate.reasons] if candidate.status == STATUS_BLOCKED else [], [], None, None if candidate.status == STATUS_BLOCKED else True, notes=[f"{candidate.kind}: no quantitative forecast in this baseline"])

    def _snapshot_for(self, candidate: CandidateDecision) -> DecisionSnapshot | None:
        """The snapshot a candidate was proposed from, when this planner has seen exactly one (else pass it explicitly)."""
        if len(self._contexts) != 1:
            return None
        return next(iter(self._contexts.values())).snapshot

    def _result_forecasts(self, ctx: _Context, fixtures: list[FixtureView]) -> tuple[list[Forecast], dict[str, str]]:
        build = self.providers.build or ctx.registry.build
        scope = {"leagues": list(ctx.leagues), "clubs": [ctx.snapshot.club_id], "builds": [build] if build else []}
        model_id = "result_forecast"
        versions = {model_id: BASELINE_VERSION}
        if self.providers.models is not None:
            resolution = self.providers.models.resolve(model_id, ReleaseContext(build or "unknown", InformationMode(ctx.snapshot.information_mode)))
            versions[model_id] = resolution.resolved_id or BASELINE_VERSION
            versions[f"{model_id}.resolution"] = resolution.reason
        forecasts = [result_forecast(ctx.points, {"date": f.date, "league": f.competition_id, "club": ctx.snapshot.club_id, "build": build}, scope, window=RESULT_FORECAST_WINDOW) for f in fixtures]
        return forecasts, versions

    def _sporting(self, forecasts: list[Forecast], lineup_change: float | None) -> Observed:
        available = [f for f in forecasts if f.available]
        if not forecasts or len(available) != len(forecasts):
            missing = [f.reason or "unavailable" for f in forecasts if not f.available] or ["no fixtures in the horizon"]
            return Observed.unavailable(ValueStatus.MISSING, "sporting_value", "; ".join(sorted(set(missing))), "baselines")
        total = sum(f.point for f in available)
        if lineup_change:
            total += LINEUP_POINTS_PER_OBJECTIVE_UNIT * lineup_change
        return Observed.available_value(total, "baselines", what="sporting_value")

    def _risk(self, ctx: _Context, risk: fin.RiskReport | None) -> Observed:
        if risk is not None:
            return Observed.available_value(risk.cvar_shortfall, "finance", what="downside_loss")
        model = self.finance_model(ctx.snapshot)
        if model.reserve_check is not None:
            return Observed.available_value(Money.from_json(_money_json(model.reserve_check["cvar_shortfall"])), "finance", what="downside_loss")
        return Observed.unavailable(ValueStatus.MISSING, "downside_loss", "no reserve policy or no balance: downside loss not estimated", "finance")

    def _development(self, ctx: _Context, plans: list[LineupPlan]) -> Observed:
        ages = {p.player_id: p.age for p in ctx.players}
        starters = [pid for pl in plans for pid in pl.player_ids]
        if not starters:
            return Observed.unavailable(ValueStatus.MISSING, "development_value", "no planned starters", "planner")
        if any(ages.get(pid) is None for pid in starters):
            return Observed.unavailable(ValueStatus.MISSING, "development_value", "age unobserved for a planned starter", "planner")
        young = sum(1 for pid in starters if ages[pid] <= YOUNG_PLAYER_AGE)
        return Observed.available_value(young / len(starters), "planner", what="development_value")

    def _continuity(self, ctx: _Context, plan_: LineupPlan) -> tuple[Observed, Observed]:
        if not ctx.current_eleven or not plan_.player_ids:
            return Observed.unavailable(ValueStatus.MISSING, "continuity", "current selection or proposed eleven unavailable", "planner"), Observed.unavailable(ValueStatus.MISSING, "plan_changes", "current selection unavailable", "planner")
        overlap = len(set(ctx.current_eleven) & set(plan_.player_ids))
        return Observed.available_value(overlap / len(plan_.player_ids), "planner", what="continuity"), Observed.available_value(len(plan_.player_ids) - overlap, "planner", what="plan_changes")

    def _rules_constraints(self, ctx: _Context, index: int) -> list[ConstraintRecord]:
        context = ctx.rules_contexts[index] if index < len(ctx.rules_contexts) else None
        if context is None or context.squad_rules.get("status") == "missing":
            return [ConstraintRecord("competition_rules", "unknown", "no rules profile for this competition (capability competition_rules)", False, "rules")]
        return [ConstraintRecord("competition_rules", "pass" if context.substitution_rules.get("status") == "available" else "unknown", f"rules profile {context.source} v{context.rules_version} ({context.substitution_rules.get('status')})", False, "rules")]

    def _evaluate_lineup(self, ctx: _Context, candidate: CandidateDecision) -> ForecastAndConstraintReport:
        plan_ = ctx.submit_lineup if candidate.kind == "submit.lineup" and ctx.submit_lineup is not None else (ctx.minutes_plan.immediate.lineup if ctx.minutes_plan else None)
        if plan_ is None:
            return ForecastAndConstraintReport(candidate.candidate_id, [], [ConstraintRecord("tactic_slots", "fail", ctx.slots.reason or "unavailable", True)], ["tactic_slots"], None, False)
        forecasts, versions = self._result_forecasts(ctx, ctx.fixtures[:1])
        forecasts.extend(self._readiness_forecasts(ctx, plan_))
        constraints = [ConstraintRecord("legal_eleven", "pass" if plan_.status != "infeasible" else "fail", f"selection status {plan_.status}", plan_.status == "infeasible", "lineup")]
        unverified = [a.player_id for a in plan_.assignments if FLAG_ELIGIBILITY_UNVERIFIED in a.flags]
        constraints.append(ConstraintRecord("eligibility_verified", "pass" if not unverified and plan_.status == "legal" else ("fail" if candidate.kind == "submit.lineup" and (unverified or not plan_.submittable) else "unknown"), f"{len(unverified)} starter(s) unverified" if unverified else "every starter verified eligible" if plan_.status == "legal" else "no eleven", candidate.kind == "submit.lineup" and bool(unverified), "eligibility"))
        constraints.extend(self._rules_constraints(ctx, 0))
        for text in plan_.binding_constraints:
            constraints.append(ConstraintRecord("selection_binding", "pass", text, True, "lineup"))
        continuity, changes = self._continuity(ctx, plan_)
        inputs = ObjectiveInputs(self._sporting(forecasts[:1], None), self._development(ctx, [plan_]), continuity, self._risk(ctx, None), changes)
        components = score_components(self.profile, inputs)
        binding = [c.name for c in constraints if c.binding]
        feasible = False if any(c.status == "fail" for c in constraints) else (None if any(c.status == "unknown" for c in constraints) else True)
        versions.update({"lineup": LINEUP_SOLVER_VERSION, "roles": ROLE_WEIGHTS_VERSION})
        return ForecastAndConstraintReport(candidate.candidate_id, forecasts, constraints, binding, components, feasible, ["baseline: points per match rolling average"], versions, [f"sporting conversion: {LINEUP_POINTS_PER_OBJECTIVE_UNIT} expected points per lineup-objective unit (heuristic {PLANNER_VERSION})"])

    def _readiness_forecasts(self, ctx: _Context, plan_: LineupPlan) -> list[Forecast]:
        days = _days_between(ctx.snapshot.game_date, ctx.fixtures[0].date) if ctx.fixtures else None
        slope = self.providers.recovery_per_day
        states = {p.player_id: p for p in ctx.players}
        forecasts = []
        for pid in plan_.player_ids[:11]:
            state = states.get(pid)
            target = f"validated_readiness_at_next_fixture:{pid}"
            if state is None or slope is None or days is None:
                forecasts.append(unavailable_forecast("recovery_baseline", "no recovery curve baseline supplied (Providers.recovery_per_day)" if slope is None else "player or fixture date unavailable", target=target))
                continue
            condition = Observed.available_value(state.condition, "bridge", what="condition") if state.readiness_status == "current" and state.condition is not None else Observed.unavailable(ValueStatus.STALE if state.readiness_status == "stale" else ValueStatus.MISSING, "condition", f"readiness {state.readiness_status}", "bridge")
            forecasts.append(readiness_forecast(condition, max(days, 0), slope, target=target))
        return forecasts

    def _evaluate_minutes(self, ctx: _Context, candidate: CandidateDecision) -> ForecastAndConstraintReport:
        mp = ctx.minutes_plan
        if mp is None:
            return ForecastAndConstraintReport(candidate.candidate_id, [], [ConstraintRecord("horizon", "fail", "no plan", True)], ["horizon"], None, False)
        forecasts, versions = self._result_forecasts(ctx, [f.fixture for f in mp.fixtures])
        constraints = [ConstraintRecord(f"promise:{pid}", "pass" if v["status"] == "planned" else "fail", f"promised {v['promised']} minutes, planned {v['planned']}", v["status"] != "planned", "promises") for pid, v in mp.promises.items()]
        constraints.extend(ConstraintRecord(f"fixture:{f.index}", "pass" if f.lineup.status != "infeasible" else "fail", f"{f.fixture.date}: {f.lineup.status}", f.lineup.status == "infeasible", "minutes") for f in mp.fixtures)
        for i in range(len(mp.fixtures)):
            constraints.extend(ConstraintRecord(f"{c.name}:{i}", c.status, c.reason, c.binding, c.source) for c in self._rules_constraints(ctx, i))
        plans = [f.lineup for f in mp.fixtures]
        rotation = sum(len(set(a.player_ids) - set(b.player_ids)) for a, b in zip(plans, plans[1:]))
        continuity, _ = self._continuity(ctx, plans[0])
        inputs = ObjectiveInputs(self._sporting(forecasts, None), self._development(ctx, plans), continuity, self._risk(ctx, None), Observed.available_value(rotation, "planner", what="plan_changes"))
        components = score_components(self.profile, inputs)
        binding = [c.name for c in constraints if c.binding]
        feasible = False if any(c.status == "fail" for c in constraints) else (None if any(c.status == "unknown" for c in constraints) else True)
        versions.update({"minutes": MINUTES_PLANNER_VERSION, "lineup": LINEUP_SOLVER_VERSION})
        return ForecastAndConstraintReport(candidate.candidate_id, forecasts, constraints, binding, components, feasible, ["baseline: points per match rolling average"], versions, ["plan_changes counts starters rotated between consecutive planned elevens"])

    def _evaluate_finance(self, ctx: _Context, candidate: CandidateDecision) -> ForecastAndConstraintReport:
        model = self.finance_model(ctx.snapshot)
        constraints = []
        if model.reserve_check is not None:
            constraints.append(ConstraintRecord("cash_reserve", model.reserve_check["status"], model.reserve_check["reason"], model.reserve_check["status"] == "fail", "finance"))
        else:
            constraints.append(ConstraintRecord("cash_reserve", "unknown", "; ".join(model.notes), False, "finance"))
        for name in ("balance", "transfer_budget", "wage_budget_weekly", "payroll_spending_weekly"):
            status = model.view[name]["status"]
            constraints.append(ConstraintRecord(f"observed:{name}", "pass" if status == "available" else "unknown", f"{name} {status}", False, "bridge:/finances"))
        feasible = False if any(c.status == "fail" for c in constraints) else (None if any(c.status == "unknown" for c in constraints) else True)
        return ForecastAndConstraintReport(candidate.candidate_id, [], constraints, [c.name for c in constraints if c.binding], None, feasible, ["committed_only"], {"finance": fin.FINANCE_POLICY_VERSION}, ["no forecast receipts: the partial model projects committed obligations only"])

    def _evaluate_package(self, ctx: _Context, candidate: CandidateDecision, scenarios: list[fin.Scenario] | None) -> ForecastAndConstraintReport:
        package = candidate.package
        assert package is not None
        window = self.finance_window(ctx)
        window_constraints = [] if window.available else [ConstraintRecord("finance_window", "unknown", f"finance window {window.status.value}: {window.reason}; guaranteed fees over the horizon not projected", False, "planner")]
        if ctx.evaluator is None:
            return ForecastAndConstraintReport(candidate.candidate_id, [], [ConstraintRecord("squad_context", "fail", "no tactic slots or fixtures to re-solve against", True), *window_constraints], ["squad_context"], None, False)
        evaluator = ctx.evaluator
        if scenarios is not None and ctx.finance_inputs is not None:
            evaluator = RecruitmentEvaluator(ctx.squad, FinanceInputs(ctx.finance, ctx.ledger, ctx.policy, scenarios, self.providers.engine, self.providers.regulatory, ctx.snapshot.game_date), baseline=ctx.evaluator.baseline)
        evaluation = evaluator.evaluate(package)
        contribution, feasibility = evaluation.contribution, evaluation.feasibility
        constraints = [ConstraintRecord(c.name, c.status.value, c.reason, c.binding, "finance") for c in feasibility.constraints]
        constraints.extend(window_constraints)
        constraints.append(_observed_constraint("registration_feasible", package.registration, "rules"))
        constraints.append(_observed_constraint("availability", package.availability, "scouting"))
        constraints.append(ConstraintRecord("acceptance", "pass" if package.acceptance == "plausible" else ("fail" if package.acceptance == "unlikely" else "unknown"), f"acceptance {package.acceptance}: {package.acceptance_basis}", False, "declared"))
        if self.providers.promises is not None:
            for conflict in self.providers.promises.check_before(candidate.kind, {"position": package.target_position, "player_id": package.player_id if package.kind == "renewal" else None}):
                constraints.append(ConstraintRecord(f"promise:{conflict.promise_id}", "fail" if conflict.blocking else "unknown" if conflict.severity == "warning" else "pass", conflict.reason, conflict.blocking, "promises"))
        if contribution.available:
            for row in contribution.displaced:
                if row["player_id"] in ctx.promised_minutes:
                    constraints.append(ConstraintRecord(f"promised_minutes:{row['player_id']}", "unknown", f"{row['name']} loses {row['starts_lost']} planned start(s) but holds a playing-time promise", False, "minutes"))
        forecasts, versions = self._result_forecasts(ctx, ctx.fixtures)
        lineup_change = contribution.change("horizon_lineup_objective_sum") if contribution.available else None
        sporting = self._sporting(forecasts, lineup_change) if contribution.available else Observed.unavailable(ValueStatus.MISSING, "sporting_value", contribution.reason, "recruitment")
        plans = [f.lineup for f in ctx.minutes_plan.fixtures] if ctx.minutes_plan else []
        continuity, _ = self._continuity(ctx, plans[0]) if plans else (Observed.unavailable(ValueStatus.MISSING, "continuity", "no plan", "planner"),) * 2
        changes = Observed.available_value(sum(r["starts_lost"] for r in contribution.displaced), "recruitment", what="plan_changes") if contribution.available else Observed.unavailable(ValueStatus.MISSING, "plan_changes", contribution.reason, "recruitment")
        components = score_components(self.profile, ObjectiveInputs(sporting, self._development(ctx, plans), continuity, self._risk(ctx, feasibility.risk), changes))
        binding = [c.name for c in constraints if c.binding]
        feasible = False if any(c.status == "fail" for c in constraints) else (None if any(c.status == "unknown" for c in constraints) else True)
        versions.update({"recruitment": RECRUITMENT_VERSION, "finance": fin.FINANCE_POLICY_VERSION, "lineup": LINEUP_SOLVER_VERSION, "minutes": MINUTES_PLANNER_VERSION})
        names = [s.name for s in (scenarios or self.providers.scenarios or [])] or ["committed_only"]
        notes = [f"sporting conversion: {LINEUP_POINTS_PER_OBJECTIVE_UNIT} expected points per lineup-objective unit (heuristic)", "valuation is a model marginal value, not a market price"]
        if not contribution.available:
            notes.append(f"contribution unavailable: {contribution.reason}")
        return ForecastAndConstraintReport(candidate.candidate_id, forecasts, constraints, binding, components, feasible, names, versions, notes, evaluation)

    # ----- reconcile -----
    def reconcile(self, candidates: list[CandidateDecision], evaluations: dict[str, ForecastAndConstraintReport], *, snapshot: DecisionSnapshot | None = None) -> SharedPlan:
        """One shared plan: wage headroom and fee budget across recruits and renewals, minutes across promises and lineups, squad places, deadlines and competition priorities."""
        snapshot = snapshot or self._snapshot_for(candidates[0] if candidates else CandidateDecision("", "", "", "", ""))
        ctx = self.context(snapshot) if snapshot is not None and snapshot.valid else None
        allocations: dict[str, dict[str, Any]] = {}
        conflicts: list[str] = []
        money = self._reconcile_money(ctx, candidates, evaluations, allocations, conflicts)
        minutes = self._reconcile_minutes(ctx, candidates, evaluations, conflicts)
        places = self._reconcile_places(ctx, candidates, allocations, conflicts)
        deadlines = self._deadlines(ctx)
        priorities = {f.identity: {"competition_id": f.competition_id, "priority": self.profile.priority(f.competition_id), "note": None if self.profile.priority(f.competition_id) is not None else "priority not set in the objective profile"} for f in (ctx.fixtures if ctx else [])}
        eligibility = self._reconcile_eligibility(ctx, candidates, conflicts)
        return SharedPlan(money, minutes, places, deadlines, priorities, allocations, conflicts, eligibility)

    @staticmethod
    def _reconcile_eligibility(ctx: _Context | None, candidates: list[CandidateDecision], conflicts: list[str]) -> dict[str, Any]:
        """Eligibility for the immediate fixture is one shared fact: every candidate that fields a player depends on it.

        Counts come from the verified-eligibility map (``True`` verified,
        ``False`` ineligible, anything else unverified); nothing unverified
        is assumed clear. Candidates whose eleven includes an unverified or
        ineligible player are listed so the operator sees what a verification
        pass would unblock.
        """
        if ctx is None or not ctx.fixtures:
            return {"status": "unavailable", "reason": "no fixture to verify eligibility for"}
        current = ctx.eligibility[0] if ctx.eligibility else {}
        verified = sorted(pid for pid, o in current.items() if o.available and o.value is True)
        ineligible = sorted(pid for pid, o in current.items() if o.available and o.value is False)
        unverified = sorted(p.player_id for p in ctx.players if p.player_id not in verified and p.player_id not in ineligible)
        depending = []
        for candidate in candidates:
            fielded = candidate.claims.minutes.keys() if candidate.horizon == HORIZON_NEXT_DECISION else ()
            unresolved = sorted(pid for pid in fielded if pid not in verified)
            if unresolved:
                depending.append({"candidate_id": candidate.candidate_id, "kind": candidate.kind, "unverified_or_ineligible": unresolved})
        fielded_ineligible = sorted({pid for row in depending for pid in row["unverified_or_ineligible"] if pid in ineligible})
        if fielded_ineligible:
            conflicts.append(f"players observed ineligible are fielded in a next-decision candidate: {fielded_ineligible}")
        return {"fixture": ctx.fixtures[0].identity, "verified": verified, "ineligible": ineligible, "unverified": unverified, "candidates_depending_on_unverified": depending, "lineup_status": ctx.lineup_status.status, "note": "unverified is not eligible: a submit needs every starter verified by a verifying source at snapshot time"}

    def _reconcile_money(self, ctx: _Context | None, candidates: list[CandidateDecision], evaluations: dict[str, ForecastAndConstraintReport], allocations: dict[str, dict[str, Any]], conflicts: list[str]) -> dict[str, Any]:
        headroom = ctx.finance.headroom_weekly() if ctx else Observed.unavailable(ValueStatus.MISSING, "wage_headroom_weekly", "no snapshot")
        budget = ctx.finance.transfer_budget if ctx else Observed.unavailable(ValueStatus.MISSING, "transfer_budget", "no snapshot")
        wage_left = headroom.value if headroom.available else None
        fee_left = budget.value if budget.available else None
        order = sorted([c for c in candidates if c.package is not None], key=lambda c: (_feasible_rank(evaluations.get(c.candidate_id)), -(_gain(evaluations.get(c.candidate_id)) or 0.0)))
        for candidate in order:
            evaluation = evaluations.get(candidate.candidate_id)
            reasons: list[str] = []
            if evaluation is not None and evaluation.feasible is False:
                allocations[candidate.candidate_id] = {"status": "infeasible", "reasons": [f"binding: {', '.join(evaluation.binding_constraints)}"]}
                continue
            wage, fee = candidate.claims.weekly_wage, candidate.claims.guaranteed_fees
            if wage is not None and wage_left is not None:
                if wage > wage_left:
                    reasons.append(f"weekly wage {wage} exceeds the remaining shared headroom {wage_left}")
                else:
                    wage_left = wage_left - wage
            elif wage is not None:
                reasons.append(f"wage headroom {headroom.status.value}: allocation unknown")
            if fee is not None and fee_left is not None:
                if fee > fee_left:
                    reasons.append(f"guaranteed fees {fee} exceed the remaining shared transfer budget {fee_left}")
                else:
                    fee_left = fee_left - fee
            elif fee is not None:
                reasons.append(f"transfer budget {budget.status.value}: allocation unknown")
            status = "allocated" if not reasons else ("deferred" if all("exceeds" in r or "exceed" in r for r in reasons) else "unknown")
            if status == "deferred":
                conflicts.append(f"{candidate.description}: {'; '.join(reasons)}")
            allocations[candidate.candidate_id] = {"status": status, "reasons": reasons or ["fits the shared wage headroom and transfer budget after earlier allocations"]}
        return {"wage_headroom_weekly": headroom.to_json() if not headroom.available else str(headroom.value), "transfer_budget": budget.to_json() if not budget.available else str(budget.value), "wage_headroom_remaining": str(wage_left) if wage_left is not None else None, "transfer_budget_remaining": str(fee_left) if fee_left is not None else None, "allocation_order": [c.candidate_id for c in order], "note": "renewals and recruits draw on the same weekly headroom; feasible packages with the larger plan gain are allocated first (heuristic order, not a ranking of merit)"}

    def _reconcile_minutes(self, ctx: _Context | None, candidates: list[CandidateDecision], evaluations: dict[str, ForecastAndConstraintReport], conflicts: list[str]) -> dict[str, Any]:
        if ctx is None or ctx.minutes_plan is None:
            return {"status": "unavailable", "reason": "no minutes plan"}
        mp = ctx.minutes_plan
        promised = dict(ctx.promised_minutes)
        unmet = {str(pid): v for pid, v in mp.promises.items() if v["status"] == "unmet"}
        for key, v in unmet.items():
            conflicts.append(f"promised {v['promised']} minutes to player {key}; the plan finds {v['planned']}")
        displaced_promises = []
        for candidate in candidates:
            evaluation = evaluations.get(candidate.candidate_id)
            if evaluation is None or evaluation.package_evaluation is None:
                continue
            for row in evaluation.package_evaluation.contribution.displaced:
                if row["player_id"] in promised:
                    displaced_promises.append({"candidate_id": candidate.candidate_id, "player_id": row["player_id"], "starts_lost": row["starts_lost"]})
        return {"fixtures": len(mp.fixtures), "planned_minutes": {str(k): v for k, v in mp.cumulative_minutes.items()}, "promised_minutes": {str(k): v for k, v in promised.items()}, "unmet_promises": unmet, "recruits_displacing_promised_players": displaced_promises, "trade_offs": list(mp.trade_offs)}

    def _reconcile_places(self, ctx: _Context | None, candidates: list[CandidateDecision], allocations: dict[str, dict[str, Any]], conflicts: list[str]) -> dict[str, Any]:
        if ctx is None:
            return {"status": "unavailable"}
        context = ctx.rules_contexts[0] if ctx.rules_contexts else None
        limit = None
        if context is not None and context.squad_rules.get("status") != "missing":
            raw = context.squad_rules.get("squad_size_limit")
            limit = raw.get("value") if isinstance(raw, dict) and raw.get("status") == "available" else (raw if isinstance(raw, int) else None)
        wanted = sum(c.claims.squad_places for c in candidates if c.package is not None and allocations.get(c.candidate_id, {}).get("status") == "allocated")
        current = len(ctx.players)
        if limit is None:
            status = "unknown: squad size limit not observed (capability competition_rules)"
        elif current + wanted > limit:
            status = f"over: {current} registered + {wanted} allocated recruit(s) exceeds the limit {limit}"
            conflicts.append(status)
        else:
            status = f"within limit {limit}"
        return {"current_squad": current, "recruits_allocated": wanted, "limit": limit, "status": status}

    def _deadlines(self, ctx: _Context | None) -> list[dict[str, Any]]:
        if ctx is None:
            return []
        items = [{"kind": "pending_action", "date": a.deadline_date, "description": a.description, "blocks_continue": a.blocks_continue, "source": a.source} for a in ctx.pending if not a.resolved]
        for context in ctx.rules_contexts:
            if context is None:
                continue
            for entry in context.deadlines:
                items.append({"kind": entry.get("kind", "deadline"), "date": entry.get("date"), "description": entry.get("description"), "competition_id": context.competition_id, "source": f"rules_profile:{context.source}"})
        if ctx.gate and ctx.gate.next_boundary:
            items.append({"kind": f"boundary:{ctx.gate.next_boundary.kind}", "date": ctx.gate.next_boundary.date, "description": ctx.gate.next_boundary.description, "source": ctx.gate.next_boundary.source})
        return items

    # ----- record and act -----
    def capability_reports(self, snapshot: DecisionSnapshot) -> dict[str, MissingCapabilityReport]:
        """One missing-capability report per gated action family (only families needing a missing subsystem are blocked)."""
        registry = self.context(snapshot).registry if snapshot.valid else (self.capabilities or registry_from_snapshot(snapshot))
        return {family: registry.check(kind) for family, kind in GATED_FAMILIES.items()}

    def decisions(self, snapshot: DecisionSnapshot, candidates: list[CandidateDecision], evaluations: dict[str, ForecastAndConstraintReport], shared: SharedPlan, tradeoffs: TradeoffTable | None) -> list[Decision]:
        """One Decision record per horizon, stored when a store is configured."""
        records = []
        versions = self.model_versions(evaluations)
        for horizon in HORIZONS:
            group = [c for c in candidates if c.horizon == horizon]
            if not group:
                continue
            selected, reasons = self._select(horizon, group, evaluations, shared)
            constraints = [dict(c.to_json(), candidate_id=cid) for cid, ev in evaluations.items() for c in ev.constraints if any(g.candidate_id == cid for g in group)]
            forecasts = [dict(f.to_json(), candidate_id=cid) for cid, ev in evaluations.items() for f in ev.forecasts if any(g.candidate_id == cid for g in group)]
            components = {cid: ev.components.to_json() for cid, ev in evaluations.items() if ev.components is not None and any(g.candidate_id == cid for g in group)}
            if horizon == HORIZON_ROLLING_12_MONTHS:
                components["tradeoffs"] = tradeoffs.to_json() if tradeoffs else None
                components["shared_plan"] = shared.to_json()
            decision = Decision(new_id("dec"), self.profile.version, snapshot.snapshot_id, [_compact(c) for c in group], constraints, forecasts, selected, reasons, components, snapshot.information_mode, versions, kind=f"plan.{horizon}")
            self._persist(snapshot, decision, reasons)
            records.append(decision)
        return records

    @staticmethod
    def _select(horizon: str, group: list[CandidateDecision], evaluations: dict[str, ForecastAndConstraintReport], shared: SharedPlan) -> tuple[dict[str, Any] | None, list[str]]:
        if horizon == HORIZON_NEXT_DECISION:
            submit = next((c for c in group if c.kind == "submit.lineup" and c.status == STATUS_PROPOSED), None)
            advisory = next((c for c in group if c.kind == "advise.lineup"), None)
            chosen = submit or advisory
            if chosen is None:
                return None, ["no lineup candidate"]
            reasons = [f"{chosen.kind} {chosen.status}: {chosen.description}"] + chosen.reasons
            return _compact(chosen), reasons
        if horizon == HORIZON_NEXT_FIXTURES:
            chosen = group[0]
            return _compact(chosen), [f"{chosen.status}: {chosen.description}"] + chosen.reasons
        if horizon == HORIZON_ROLLING_12_MONTHS:
            reasons = ["no package selected: recruitment exposes tradeoffs and requires a negotiated, confirmed offer (spec 7.2, 8.3)"]
            for c in group:
                if c.package is not None:
                    alloc = shared.allocations.get(c.candidate_id, {})
                    ev = evaluations.get(c.candidate_id)
                    reasons.append(f"{c.description}: feasibility {ev.feasible if ev else 'not evaluated'}, allocation {alloc.get('status', 'n/a')}")
            return None, reasons
        return None, ["gap list only: succession needs are reported, not planned (spec 4.2)"] + [c.description for c in group]

    def _persist(self, snapshot: DecisionSnapshot, decision: Decision, reasons: list[str]) -> None:
        if self.store is None:
            return
        try:
            if self.store.get_snapshot(snapshot.snapshot_id) is None:
                self.store.insert_snapshot(snapshot)
            self.store.insert_decision(decision)
        except StoreError as exc:
            reasons.append(f"decision not persisted: {exc}")

    @staticmethod
    def model_versions(evaluations: dict[str, ForecastAndConstraintReport]) -> dict[str, str]:
        versions: dict[str, str] = {"planner": PLANNER_VERSION}
        for ev in evaluations.values():
            versions.update(ev.model_versions)
        return versions

    def intents(self, snapshot: DecisionSnapshot, candidates: list[CandidateDecision], decisions: list[Decision]) -> list[IntentOutcome]:
        """Build intents only for proposed candidates whose capability check passes; record why otherwise.

        A ``submit.lineup`` intent needs a submittable, verified eleven; an
        advisory or unverified lineup is never promoted (spec 7.3). The
        intent is then validated against the authority profile through the
        lifecycle so the record shows VALIDATED or OUTSIDE_SCOPE.
        """
        ctx = self.context(snapshot)
        factory = IntentFactory(self.store)
        outcomes = []
        decision_by_horizon = {d.kind.removeprefix("plan."): d.decision_id for d in decisions}
        for candidate in candidates:
            if candidate.kind not in AUTHORITY_SCOPES:
                continue
            if candidate.status != STATUS_PROPOSED:
                outcomes.append(IntentOutcome(candidate.kind, candidate.candidate_id, False, None, f"{candidate.status}: " + ("; ".join(candidate.reasons) or "not proposed for execution")))
                continue
            report = ctx.registry.check(candidate.kind)
            if report.blocked:
                outcomes.append(IntentOutcome(candidate.kind, candidate.candidate_id, False, None, "capability check failed: " + ", ".join(f"{m} ({report.reasons[m]})" for m in report.missing)))
                continue
            if candidate.kind == "submit.lineup" and (candidate.unverified or not candidate.submittable):
                outcomes.append(IntentOutcome(candidate.kind, candidate.candidate_id, False, None, "advisory or unverified lineup is never submitted"))
                continue
            twin = unsettled_twin(self.store, candidate.kind, candidate.targets, branch_id=snapshot.branch_id) if self.store is not None else None
            if twin is not None:
                # Duplicate-effect guard (spec 12.3, ACT 02): the earlier intent's effect is not established; a new one could double it.
                outcomes.append(IntentOutcome(candidate.kind, candidate.candidate_id, False, None, f"duplicate-effect guard: intent {twin.action_id} for the same targets is still {twin.state.value}; reconcile it before a new intent is minted"))
                continue
            try:
                intent = factory.create(candidate.kind, AUTHORITY_SCOPES[candidate.kind], snapshot, candidate.targets, candidate.parameters, verification=VERIFICATION_PLANS[candidate.kind], decision_id=decision_by_horizon.get(candidate.horizon))
            except LifecycleError as exc:
                outcomes.append(IntentOutcome(candidate.kind, candidate.candidate_id, False, None, f"intent not created: {exc}"))
                continue
            result = validate(intent, ctx.registry, self.authority, snapshot, self.store)
            outcomes.append(IntentOutcome(candidate.kind, candidate.candidate_id, True, intent.action_id, "; ".join(result.reasons) or result.state.value, result.state.value))
        return outcomes

    # ----- one pass -----
    def plan(self, snapshot: DecisionSnapshot, scenarios: list[fin.Scenario] | None = None) -> PlanReport:
        """Propose, evaluate, reconcile, record and (where allowed) prepare intents for one snapshot."""
        candidates = self.propose(snapshot)
        if not snapshot.valid:
            return PlanReport(snapshot.snapshot_id, snapshot.game_date, snapshot.game_time, "blocked", candidates, {}, None, [], self.capability_reports(snapshot), [], None, [], None, None, [candidates[0].description + ": " + "; ".join(candidates[0].reasons)], {"planner": PLANNER_VERSION}, candidates[0].reasons)
        evaluations = {c.candidate_id: self.evaluate(c, scenarios, snapshot=snapshot) for c in candidates}
        shared = self.reconcile(candidates, evaluations, snapshot=snapshot)
        packages = [ev.package_evaluation for ev in evaluations.values() if ev.package_evaluation is not None]
        ctx = self.context(snapshot)
        tradeoffs = package_tradeoffs(packages, window=ctx.finance_inputs.window() if ctx.finance_inputs else None) if packages else None
        decisions = self.decisions(snapshot, candidates, evaluations, shared, tradeoffs)
        intents = self.intents(snapshot, candidates, decisions)
        reports = self.capability_reports(snapshot)
        model = self.finance_model(snapshot)
        gaps = self.succession(snapshot)
        summaries = self.summaries(snapshot, candidates, evaluations, shared, model, gaps, intents)
        return PlanReport(snapshot.snapshot_id, snapshot.game_date, snapshot.game_time, "planned", candidates, evaluations, shared, decisions, reports, intents, model, gaps, ctx.gate, tradeoffs, summaries, self.model_versions(evaluations))

    def summaries(self, snapshot: DecisionSnapshot, candidates: list[CandidateDecision], evaluations: dict[str, ForecastAndConstraintReport], shared: SharedPlan, model: PartialFinanceModel, gaps: list[SuccessionGap], intents: list[IntentOutcome]) -> list[str]:
        """Football-language lines for the operator; every line says what is verified and what is not."""
        ctx = self.context(snapshot)
        lines = []
        lineup = next((c for c in candidates if c.kind == "advise.lineup"), None)
        if lineup is not None:
            lines.append(f"Next match: {lineup.description}. " + ("Not for submission: " + lineup.reasons[0] if lineup.reasons else "Every starter verified eligible."))
        submit = next((c for c in candidates if c.kind == "submit.lineup"), None)
        if submit is not None:
            lines.append(f"Team sheet submission: {submit.status}" + (" - " + "; ".join(submit.reasons) if submit.reasons else " - ready for the single UI writer"))
        minutes = next((c for c in candidates if c.kind == "advise.minutes"), None)
        if minutes is not None:
            lines.append(minutes.description + (". Issues: " + "; ".join(minutes.reasons) if minutes.reasons else "."))
        if model.committed_projection is not None:
            proj = model.committed_projection
            lines.append(f"Finances: cash {proj['start_balance']} today; on committed obligations alone the low point is {proj['min_cash']} on {proj['min_cash_date']}; " + (f"reserve rule {model.reserve_check['status']} ({model.reserve_check['policy']})" if model.reserve_check else "no reserve rule configured") + f"; {model.unknown_items} unknown item(s).")
        else:
            lines.append("Finances: " + "; ".join(model.notes))
        recruits = [c for c in candidates if c.package is not None]
        if recruits:
            parts = []
            for c in recruits:
                ev = evaluations.get(c.candidate_id)
                alloc = shared.allocations.get(c.candidate_id, {}).get("status", "n/a")
                feas = "feasible" if ev and ev.feasible is True else ("infeasible: " + ", ".join(ev.binding_constraints) if ev and ev.feasible is False else "feasibility unknown")
                gain = ev.package_evaluation.contribution.change("horizon_lineup_objective_sum") if ev and ev.package_evaluation and ev.package_evaluation.contribution.available else None
                parts.append(f"{c.package.player.name} ({c.package.kind}) {feas}, shared allocation {alloc}" + (f", lineup gain {gain:+.3f} model units over the horizon" if gain is not None else ", sporting contribution unavailable"))
            lines.append("Recruitment tradeoffs (no ranking): " + "; ".join(parts) + f". Wage headroom {shared.money['wage_headroom_weekly']}.")
        if gaps:
            lines.append("Succession gaps within three seasons at: " + ", ".join(sorted({g.position for g in gaps})) + " (gap list only).")
        cont = next((c for c in candidates if c.kind == "progress.continue"), None)
        if cont is not None:
            lines.append(f"Continue: {cont.status}" + (" - " + "; ".join(cont.reasons) if cont.reasons else " - gate open"))
        blocked = [f"{i.kind}: {i.reason}" for i in intents if not i.created]
        created = [f"{i.kind} {i.state}" for i in intents if i.created]
        if created or blocked:
            lines.append("Actions: " + "; ".join(created + blocked))
        return lines


# ---------------------------------------------------------------------------
# module-level interfaces (spec 5.4) and convenience
# ---------------------------------------------------------------------------

def _compact(candidate: CandidateDecision) -> dict[str, Any]:
    data = candidate.to_json()
    data.pop("payload", None)
    return data


def lineup_parameters(plan_: LineupPlan | None) -> dict[str, Any]:
    """The one lineup parameter contract shared with the UI workflow and the verifier (spec 12.2).

    ``player_ids`` is the eleven in slot order; ``roles`` maps each slot's
    position code (``GK``, ``DCR``, ``STCL`` ...) to the role name assigned
    there, exactly what ``set_lineup`` sends and ``lineup_matches_selection``
    reads back. A plan with no assignments yields an empty eleven and no roles.
    """
    if plan_ is None:
        return {"player_ids": [], "roles": {}}
    return {"player_ids": list(plan_.player_ids), "roles": {a.position: a.role for a in plan_.assignments}}


def _window_json(window: Observed) -> dict[str, Any]:
    data = window.to_json()
    if window.available:
        data["value"] = [d.isoformat() if isinstance(d, dt.date) else str(d) for d in window.value]
    return data


def _money_json(text: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    currency, _, rest = text.partition(" ")
    amount = rest.split("/")[0].replace(",", "")
    pounds, _, pence = amount.partition(".")
    return {"minor": int(pounds) * 100 + int((pence or "0").ljust(2, "0")[:2]), "currency": currency, "period": Period.ONCE.value}


def _observed_json(observed: Observed) -> dict[str, Any]:
    """``Observed.to_json()`` with a Money value rendered as Money JSON (exact minor units, currency, period)."""
    data = observed.to_json()
    if isinstance(data.get("value"), Money):
        data["value"] = data["value"].to_json()
    return data


def _observed_constraint(name: str, observed: Observed, source: str) -> ConstraintRecord:
    if observed.available:
        return ConstraintRecord(name, "pass" if observed.value is True else "fail", observed.reason or f"{name} observed {observed.value}", observed.value is not True, source)
    return ConstraintRecord(name, "unknown", f"{name} {observed.status.value}: {observed.reason}", False, source)


def _feasible_rank(evaluation: ForecastAndConstraintReport | None) -> int:
    if evaluation is None:
        return 2
    return {True: 0, None: 1, False: 2}[evaluation.feasible]


def _gain(evaluation: ForecastAndConstraintReport | None) -> float | None:
    if evaluation is None or evaluation.package_evaluation is None:
        return None
    return evaluation.package_evaluation.contribution.change("horizon_plan_value")


def propose(snapshot: DecisionSnapshot, objective_profile: ClubObjectiveProfile | None = None, authority: AuthorityProfile | None = None, capabilities: CapabilityRegistry | None = None, providers: Providers | None = None, *, store=None) -> list[CandidateDecision]:
    """``propose(snapshot, objective, limits) -> CandidateDecision[]`` (spec 5.4)."""
    return Planner(objective_profile=objective_profile, authority=authority, capabilities=capabilities, providers=providers, store=store).propose(snapshot)


def evaluate(candidate: CandidateDecision, scenarios: list[fin.Scenario] | None, *, planner: Planner, snapshot: DecisionSnapshot) -> ForecastAndConstraintReport:
    """``evaluate(candidate, scenarios) -> ForecastAndConstraintReport`` (spec 5.4) against the planner that proposed it."""
    return planner.evaluate(candidate, scenarios, snapshot=snapshot)


def plan_once(snapshot: DecisionSnapshot, *, store=None, objective_profile: ClubObjectiveProfile | None = None, authority: AuthorityProfile | None = None, capabilities: CapabilityRegistry | None = None, providers: Providers | None = None, scenarios: list[fin.Scenario] | None = None) -> PlanReport:
    """Everything from one snapshot with default providers (no eligibility source, no rules, no language model).

    With defaults every lineup is advisory and unverified, the Continue gate
    is blocked by the missing bot-side capabilities, and recruitment packages
    (if any are passed in ``providers.candidates``) are judged sportingly but
    their finance feasibility is unknown unless a reserve policy exists.
    """
    return Planner(objective_profile=objective_profile, authority=authority, capabilities=capabilities, providers=providers, store=store).plan(snapshot, scenarios)
