"""Recruitment candidate packages and portfolio effects (spec 7.2, 7.3, 8.3, 11.1, 17.1).

A recruit is judged by what he *adds to the squad we already have and the
plan we already hold*, not by an isolated grade. Everything here re-solves
the existing selection and minutes planners with and without the candidate
and reports the difference:

* :func:`marginal_contribution` - change in the lineup and minutes plan
  (objective units), role coverage depth per tactic slot, players displaced,
  and *versatility value*: the expected reduction in performance loss across
  availability scenarios. A player covers only one simultaneous assignment,
  so each scenario is a full re-solve of the eleven rather than a sum of
  per-position insurance values.
* :func:`shadow_value_of_squad_place` - the value of a squad place or of
  the cover for a scarce role, obtained by re-solving with and without that
  resource. It is labelled ``model marginal value, not a market price``.
* :func:`package_tradeoffs` - a table of tradeoffs across packages: sporting
  change, coverage, versatility, weekly wage, guaranteed fees, feasibility
  and acceptance. It deliberately carries no single ranking.
* :class:`RecruitmentEvaluator` - the recompute hook: when negotiation
  terms change, the package is re-versioned and its finance feasibility is
  re-checked while the sporting side is reused.

Baseline versus experiment
--------------------------
Everything in this module is a baseline built on the reviewable heuristics
of :mod:`fm_bot.planning.roles`, :mod:`fm_bot.planning.lineup` and
:mod:`fm_bot.planning.minutes`. No fee, wage or resale value is predicted:
a :class:`DisplayedValuation` is a model quantity in lineup-objective units
whose ``executable_buying_price`` and ``guaranteed_resale_income`` fields are
explicitly unavailable. The plausible-acceptance label is a declaration made
by whoever supplies the package (scout, negotiation state machine, operator);
this module never infers it.

Money conventions follow :mod:`fm_bot.planning.finance`: weekly wages are
``wages``/``WEEKLY`` commitments, fees are ``transfer_fee``/``ONCE`` (with
instalments as separate dated commitments), conditional items carry a
trigger and are never taken at face value.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Iterable

from ..state.records import Certainty, CompetitionContext, FinancialCommitment, MovementKind, PlayerState
from ..state.status import MissingCapabilityReport, Observed, ValueStatus
from ..state.units import Money, Period, parse_date, sum_money
from ..state.views import FinanceView, FixtureView
from . import finance as fin
from .lineup import LineupPlan, LineupRequest, RoleSlot, solve
from .minutes import INFEASIBLE_FIXTURE_PENALTY, MinutesPlan, MinutesRequest, Restriction, plan, plan_value
from .roles import UNFAMILIAR_LABEL, base_position, explain_role_score

RECRUITMENT_VERSION = "recruitment-baseline-0.1"

# Labels required by spec 7.2. They travel with every valuation this module emits.
VALUATION_LABEL = "model marginal value, not a market price"
VALUATION_UNIT = "lineup_objective_units"
WHAT_IF_MODE = "advisory"          # what-if re-solves never run in submit mode: a candidate is not yet ours to field

# Acceptance labels are declared by the package supplier (spec 7.2 "plausible acceptance"), never inferred here.
ACCEPTANCE_PLAUSIBLE = "plausible"
ACCEPTANCE_UNLIKELY = "unlikely"
ACCEPTANCE_UNKNOWN = "unknown"
ACCEPTANCE_LABELS: tuple[str, ...] = (ACCEPTANCE_PLAUSIBLE, ACCEPTANCE_UNLIKELY, ACCEPTANCE_UNKNOWN)

PACKAGE_KINDS: tuple[str, ...] = ("buy", "loan_in", "free_transfer", "renewal")

# Heuristic constants (reviewable). A player counts as cover for a slot when his
# role score is available and he is not unfamiliar with the slot's position.
COVERAGE_MIN_DEPTH = 2             # slots with fewer covering players than this are reported as thin
SUCCESSION_YEARS = 3               # succession gaps are listed over this horizon (spec 4: 3-year succession)
SUCCESSION_AGE_THRESHOLD = 32      # a player at or past this age at the horizon counts as ageing cover
TRADEOFF_DIMENSIONS: tuple[str, ...] = ("immediate_lineup_objective", "horizon_plan_value", "versatility_value", "slots_newly_covered", "weekly_wage", "guaranteed_fees", "conditional_total", "feasible", "binding_constraint", "acceptance", "registration", "availability")


# ---------------------------------------------------------------------------
# candidate packages
# ---------------------------------------------------------------------------

@dataclass
class DisplayedValuation:
    """A model quantity shown to the operator. Not a price, not resale income.

    ``executable_buying_price`` and ``guaranteed_resale_income`` are
    unavailable by construction: this module has no market model and the
    specification forbids presenting a valuation as either.
    """

    model_value: float
    unit: str = VALUATION_UNIT
    label: str = VALUATION_LABEL
    method: str = "resolve_with_and_without"
    executable_buying_price: Observed = field(default_factory=lambda: Observed.unavailable(ValueStatus.UNSUPPORTED, "executable_buying_price", "a model marginal value is not a buying price; only a negotiated offer is", "recruitment"))
    guaranteed_resale_income: Observed = field(default_factory=lambda: Observed.unavailable(ValueStatus.UNSUPPORTED, "guaranteed_resale_income", "no resale income is guaranteed by a model valuation", "recruitment"))
    version: str = RECRUITMENT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"model_value": self.model_value, "unit": self.unit, "label": self.label, "method": self.method, "executable_buying_price": self.executable_buying_price.to_json(), "guaranteed_resale_income": self.guaranteed_resale_income.to_json(), "version": self.version}


@dataclass
class CandidatePackage:
    """A recruit (or renewal) together with the exact terms proposed for him.

    ``player`` is the candidate's state as observed (a scouted profile may
    carry masked attributes, in which case his contribution is unavailable
    rather than guessed). ``terms`` are the proposed obligations in ledger
    conventions. ``acceptance`` is a declared label; ``registration`` and
    ``availability`` are :class:`Observed` booleans supplied by the rules and
    scouting sources, missing until someone observes them.
    """

    candidate_id: str
    player: PlayerState
    kind: str
    terms: list[FinancialCommitment]
    acceptance: str = ACCEPTANCE_UNKNOWN
    acceptance_basis: str = "not declared"
    registration: Observed = field(default_factory=lambda: Observed.unavailable(ValueStatus.MISSING, "registration_feasible", "no competition rules profile consulted", "none"))
    availability: Observed = field(default_factory=lambda: Observed.unavailable(ValueStatus.MISSING, "availability", "no availability observation (transfer status, contract state) recorded", "none"))
    target_position: str | None = None
    source: str = "operator"
    terms_version: int = 1
    counterparty: str | None = None
    notes: list[str] = field(default_factory=list)
    version: str = RECRUITMENT_VERSION

    def __post_init__(self):
        if self.kind not in PACKAGE_KINDS:
            raise ValueError(f"package kind must be one of {PACKAGE_KINDS}, got {self.kind!r}")
        if self.acceptance not in ACCEPTANCE_LABELS:
            raise ValueError(f"acceptance label must be one of {ACCEPTANCE_LABELS}, got {self.acceptance!r}")
        currencies = {c.amount.currency for c in self.terms}
        if len(currencies) > 1:
            raise ValueError(f"package terms mix currencies {sorted(currencies)}")

    @property
    def player_id(self) -> int:
        return self.player.player_id

    @property
    def currency(self) -> str:
        return next((c.amount.currency for c in self.terms), "GBP")

    def weekly_wage(self) -> Money:
        """Committed weekly wages in the package (loan contributions included)."""
        return sum_money((c.amount for c in self.terms if c.category in fin.WAGE_BUDGET_CATEGORIES and c.recurrence is Period.WEEKLY and c.kind is MovementKind.PAYMENT and c.certainty is Certainty.OBSERVED_COMMITTED), self.currency, Period.WEEKLY)

    def guaranteed_fees(self, start: str | dt.date, end: str | dt.date) -> Money:
        """Guaranteed one-off club payments falling inside the window (calendar-expanded)."""
        return fin.guaranteed_total((c for c in self.terms if c.category in fin.TRANSFER_BUDGET_CATEGORIES), start, end, currency=self.currency)

    def conditional_total(self) -> Money:
        return sum_money((c.amount.as_once() for c in self.terms if c.certainty is Certainty.CONDITIONAL and c.kind is MovementKind.PAYMENT and c.recurrence is Period.ONCE), self.currency, Period.ONCE)

    def with_terms(self, terms: list[FinancialCommitment], *, source: str | None = None, acceptance: str | None = None, acceptance_basis: str | None = None) -> "CandidatePackage":
        """A new version of the package with changed negotiation terms (the recompute hook's input)."""
        return CandidatePackage(self.candidate_id, self.player, self.kind, list(terms), acceptance or self.acceptance, acceptance_basis or self.acceptance_basis, self.registration, self.availability, self.target_position, source or self.source, self.terms_version + 1, self.counterparty, list(self.notes))

    def to_json(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "player_id": self.player_id, "player_name": self.player.name, "kind": self.kind, "terms": [c.to_json() for c in self.terms], "acceptance": self.acceptance, "acceptance_basis": self.acceptance_basis, "registration": self.registration.to_json(), "availability": self.availability.to_json(), "target_position": self.target_position, "source": self.source, "terms_version": self.terms_version, "counterparty": self.counterparty, "weekly_wage": str(self.weekly_wage()), "notes": list(self.notes), "version": self.version}


def package_terms(counterparty: str, *, weekly_wage: Money, wage_start: str, wage_end: str, source: str, fee: Money | None = None, fee_due: str | None = None, instalments: list[tuple[str, Money]] | None = None, signing_on_fee: Money | None = None, agent_fee: Money | None = None, conditional: Iterable[tuple[str, Money, str, str]] = (), prefix: str | None = None) -> list[FinancialCommitment]:
    """Build proposed terms in ledger conventions.

    ``fee`` is paid in full on ``fee_due`` unless ``instalments`` are given
    (they must sum exactly to the fee). ``conditional`` entries are
    ``(label, amount, trigger, due_date)`` and become conditional commitments
    that the cash engine resolves per scenario, never at face value.
    """
    if weekly_wage.period is not Period.WEEKLY:
        raise ValueError("weekly_wage must carry the weekly period")
    prefix = prefix or f"package:{counterparty}"
    terms = [FinancialCommitment(f"{prefix}:wages", counterparty, MovementKind.PAYMENT, weekly_wage, wage_start, Period.WEEKLY, wage_end, None, "club", Certainty.OBSERVED_COMMITTED, source, 1, "wages")]
    if fee is not None:
        if instalments:
            terms.extend(fin.instalment_commitments(counterparty, fee, instalments, source=source, commitment_prefix=f"{prefix}:fee"))
        else:
            if fee_due is None:
                raise ValueError("a fee needs a due date or an instalment schedule")
            terms.append(FinancialCommitment(f"{prefix}:fee", counterparty, MovementKind.PAYMENT, fee.as_once(), fee_due, Period.ONCE, None, None, "club", Certainty.OBSERVED_COMMITTED, source, 1, "transfer_fee"))
    if signing_on_fee is not None:
        terms.append(FinancialCommitment(f"{prefix}:signing_on", counterparty, MovementKind.PAYMENT, signing_on_fee.as_once(), fee_due or wage_start, Period.ONCE, None, None, "club", Certainty.OBSERVED_COMMITTED, source, 1, "signing_on_fee"))
    if agent_fee is not None:
        terms.append(FinancialCommitment(f"{prefix}:agent", counterparty, MovementKind.PAYMENT, agent_fee.as_once(), fee_due or wage_start, Period.ONCE, None, None, "club", Certainty.OBSERVED_COMMITTED, source, 1, "agent_fee"))
    for label, amount, trigger, due in conditional:
        terms.append(fin.conditional_commitment(counterparty, amount.as_once(), trigger, category="bonus", source=source, due_date=due, commitment_id=f"{prefix}:conditional:{label}"))
    return terms


def registration_feasibility(context: CompetitionContext | None, squad_count: int, game_date: str | None) -> Observed:
    """Whether one more player can be registered, from an observed rules profile; missing without one.

    Uses ``squad_size_limit`` and ``registration_windows`` from the
    competition context when they are available. An open question stays
    open: no limit observed means the answer is missing, not ``True``.
    """
    if context is None or context.squad_rules.get("status") == "missing":
        return Observed.unavailable(ValueStatus.MISSING, "registration_feasible", "no competition rules profile for this competition (capability competition_rules)", "none")
    limit = _rule_value(context.squad_rules.get("squad_size_limit"))
    windows = _rule_value(context.squad_rules.get("registration_windows"))
    if limit is None:
        return Observed.unavailable(ValueStatus.MISSING, "registration_feasible", "squad size limit not in the rules profile", context.source)
    if squad_count + 1 > int(limit):
        return Observed(False, ValueStatus.AVAILABLE, context.source, None, None, f"squad of {squad_count} is at the registered limit {limit}", "registration_feasible")
    if windows is not None and game_date is not None:
        open_now = any(w.get("opens") and w.get("closes") and w["opens"] <= game_date <= w["closes"] for w in windows if isinstance(w, dict))
        if not open_now:
            return Observed(False, ValueStatus.AVAILABLE, context.source, None, None, f"no registration window open on {game_date}", "registration_feasible")
    return Observed.available_value(True, context.source, what="registration_feasible")


def _rule_value(raw: Any) -> Any:
    if isinstance(raw, dict) and "status" in raw:
        return raw.get("value") if raw.get("status") == ValueStatus.AVAILABLE.value else None
    return raw


# ---------------------------------------------------------------------------
# squad context: everything a what-if re-solve needs
# ---------------------------------------------------------------------------

@dataclass
class SquadContext:
    """The squad, tactic slots and fixture horizon that every what-if re-solve shares.

    ``eligibility`` is one ``{player_id: Observed[bool]}`` map per fixture
    (as :func:`fm_bot.planning.lineup.eligibility_map` produces); when
    omitted every player is missing, which the advisory what-if mode allows
    and labels. A candidate not yet at the club always has missing
    eligibility: nobody has verified him for our fixtures.
    """

    players: list[PlayerState]
    slots: list[RoleSlot]
    fixtures: list[FixtureView]
    eligibility: list[dict[int, Observed]] | None = None
    promised_minutes: dict[int, int] = field(default_factory=dict)
    restrictions: list[Restriction] = field(default_factory=list)
    workload_caps: dict[int, int] = field(default_factory=dict)
    club_id: int | None = None
    competition_rules: list[CompetitionContext | None] | None = None
    time_budget_seconds: float = 2.0

    def eligibility_for(self, index: int) -> dict[int, Observed]:
        if self.eligibility is None or index >= len(self.eligibility):
            return {}
        return dict(self.eligibility[index])

    def request(self, *, extra_players: Iterable[PlayerState] = (), without: Iterable[int] = ()) -> MinutesRequest:
        """A minutes request over the horizon with players added and/or removed (what-if, advisory)."""
        excluded = set(without)
        players = [p for p in self.players if p.player_id not in excluded] + [p for p in extra_players if p.player_id not in excluded]
        eligibility = [self.eligibility_for(i) for i in range(len(self.fixtures))]
        return MinutesRequest(players, list(self.fixtures), list(self.slots), eligibility, list(self.restrictions), dict(self.promised_minutes), None, dict(self.workload_caps), self.club_id, WHAT_IF_MODE, list(self.competition_rules) if self.competition_rules is not None else None, time_budget_seconds=self.time_budget_seconds)

    def lineup_request(self, *, extra_players: Iterable[PlayerState] = (), without: Iterable[int] = ()) -> LineupRequest:
        """A single-fixture request for the immediate fixture (what-if, advisory)."""
        excluded = set(without)
        players = [p for p in self.players if p.player_id not in excluded] + [p for p in extra_players if p.player_id not in excluded]
        fixture = self.fixtures[0] if self.fixtures else None
        rules = self.competition_rules[0] if self.competition_rules else None
        eligibility = self.eligibility_for(0)
        for r in self.restrictions:
            if fixture is not None and r.applies(fixture, self.club_id):
                eligibility[r.player_id] = Observed(False, ValueStatus.AVAILABLE, r.source, None, None, r.describe(), "verified_eligible")
        return LineupRequest(players, list(self.slots), fixture, eligibility, WHAT_IF_MODE, competition_rules=rules, time_budget_seconds=self.time_budget_seconds)


def lineup_objective(plan_: LineupPlan) -> float:
    """Objective value of a single-fixture plan; an infeasible plan counts the planner's infeasibility penalty."""
    return plan_.objective_value if plan_.objective_value is not None else -INFEASIBLE_FIXTURE_PENALTY


def horizon_objective(minutes_plan: MinutesPlan) -> float:
    return sum(lineup_objective(f.lineup) for f in minutes_plan.fixtures)


# ---------------------------------------------------------------------------
# role coverage
# ---------------------------------------------------------------------------

@dataclass
class RoleCoverage:
    slot: int
    position: str
    role: str | None
    depth_without: int
    depth_with: int
    best_without: float | None
    best_with: float | None

    @property
    def change(self) -> int:
        return self.depth_with - self.depth_without

    def to_json(self) -> dict[str, Any]:
        return {"slot": self.slot, "position": self.position, "role": self.role, "depth_without": self.depth_without, "depth_with": self.depth_with, "change": self.change, "best_without": self.best_without, "best_with": self.best_with}


def covering_players(players: Iterable[PlayerState], slot: RoleSlot) -> list[tuple[int, float]]:
    """Players who can cover ``slot``: role score available and position not unfamiliar. ``(player_id, score)``."""
    found = []
    for player in players:
        detail = explain_role_score(player, slot.role, slot.position)
        if detail.score.available and detail.familiarity != UNFAMILIAR_LABEL:
            found.append((player.player_id, detail.score.value))
    return found


def role_coverage(players: list[PlayerState], slots: list[RoleSlot], candidate: PlayerState | None) -> list[RoleCoverage]:
    """Cover depth per slot with and without the candidate."""
    result = []
    for slot in slots:
        without = covering_players(players, slot)
        with_ = without + covering_players([candidate], slot) if candidate is not None else without
        result.append(RoleCoverage(slot.slot, slot.position, slot.role, len(without), len(with_), max((s for _, s in without), default=None), max((s for _, s in with_), default=None)))
    return result


def coverage_summary(coverage: list[RoleCoverage]) -> dict[str, Any]:
    return {"newly_covered": [c.slot for c in coverage if c.depth_without == 0 and c.depth_with > 0], "deepened": [c.slot for c in coverage if c.depth_with > c.depth_without], "thin_after": [c.slot for c in coverage if c.depth_with < COVERAGE_MIN_DEPTH], "unchanged": sum(1 for c in coverage if c.change == 0), "min_depth": COVERAGE_MIN_DEPTH}


# ---------------------------------------------------------------------------
# versatility value across availability scenarios
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AvailabilityScenario:
    """A named absence: these players cannot be fielded in the immediate fixture."""

    name: str
    unavailable_player_ids: tuple[int, ...]
    weight: Fraction = Fraction(1)


def default_availability_scenarios(baseline: LineupPlan) -> list[AvailabilityScenario]:
    """One scenario per current starter, equally weighted (HEURISTIC: no injury model is used)."""
    starters = baseline.player_ids
    if not starters:
        return []
    weight = Fraction(1, len(starters))
    return [AvailabilityScenario(f"starter {pid} unavailable", (pid,), weight) for pid in starters]


@dataclass
class VersatilityValue:
    """Expected reduction in performance loss across availability scenarios (spec 7.2).

    ``loss`` in each scenario is the drop of the immediate lineup objective
    against that squad's own full-availability plan. The candidate fills at
    most one slot per scenario because each scenario is a full re-solve.
    """

    value: float
    scenarios: list[dict[str, Any]]
    unit: str = VALUATION_UNIT
    note: str = "one simultaneous assignment per scenario; each scenario is a full re-solve of the eleven"
    weights_normalised: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "scenarios": [dict(s) for s in self.scenarios], "unit": self.unit, "note": self.note, "weights_normalised": self.weights_normalised}


def versatility_value(context: SquadContext, candidate: PlayerState, scenarios: list[AvailabilityScenario] | None = None) -> VersatilityValue:
    """Weighted mean over scenarios of ``loss_without_candidate - loss_with_candidate``."""
    base_without = solve(context.lineup_request())
    base_with = solve(context.lineup_request(extra_players=[candidate]))
    scenarios = scenarios if scenarios is not None else default_availability_scenarios(base_without)
    if not scenarios:
        return VersatilityValue(0.0, [], note="no availability scenarios: no starters to remove")
    total_weight = sum((s.weight for s in scenarios), Fraction(0))
    if total_weight <= 0:
        raise ValueError("availability scenario weights must sum to a positive value")
    rows: list[dict[str, Any]] = []
    value = 0.0
    for scenario in scenarios:
        absent = list(scenario.unavailable_player_ids)
        loss_without = lineup_objective(base_without) - lineup_objective(solve(context.lineup_request(without=absent)))
        loss_with = lineup_objective(base_with) - lineup_objective(solve(context.lineup_request(extra_players=[candidate], without=absent)))
        weight = scenario.weight / total_weight
        reduction = loss_without - loss_with
        value += float(weight) * reduction
        rows.append({"name": scenario.name, "unavailable": absent, "weight": str(weight), "loss_without": round(loss_without, 6), "loss_with": round(loss_with, 6), "reduction": round(reduction, 6)})
    return VersatilityValue(round(value, 9), rows)


# ---------------------------------------------------------------------------
# marginal contribution
# ---------------------------------------------------------------------------

@dataclass
class ComponentChange:
    name: str
    without: float
    with_: float
    unit: str

    @property
    def change(self) -> float:
        return self.with_ - self.without

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "without": self.without, "with": self.with_, "change": round(self.change, 9), "unit": self.unit}


@dataclass
class MarginalContribution:
    """What the candidate changes in the plan. Unavailable (never zero) when his fit cannot be scored."""

    candidate_id: str
    player_id: int
    status: str                                   # available | unavailable
    reason: str | None
    components: dict[str, ComponentChange] = field(default_factory=dict)
    coverage: list[RoleCoverage] = field(default_factory=list)
    coverage_summary: dict[str, Any] = field(default_factory=dict)
    versatility: VersatilityValue | None = None
    displaced: list[dict[str, Any]] = field(default_factory=list)
    candidate_slots: list[dict[str, Any]] = field(default_factory=list)
    valuation: DisplayedValuation | None = None
    unverified: bool = True
    method: str = "resolve_with_and_without"
    version: str = RECRUITMENT_VERSION

    @property
    def available(self) -> bool:
        return self.status == "available"

    def change(self, name: str) -> float | None:
        item = self.components.get(name)
        return item.change if item else None

    def to_json(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "player_id": self.player_id, "status": self.status, "reason": self.reason, "components": {k: v.to_json() for k, v in self.components.items()}, "coverage": [c.to_json() for c in self.coverage], "coverage_summary": dict(self.coverage_summary), "versatility": self.versatility.to_json() if self.versatility else None, "displaced": [dict(d) for d in self.displaced], "candidate_slots": [dict(s) for s in self.candidate_slots], "valuation": self.valuation.to_json() if self.valuation else None, "unverified": self.unverified, "method": self.method, "version": self.version}


def _fit_scoreable(candidate: PlayerState, slots: list[RoleSlot]) -> str | None:
    """Reason the candidate cannot be scored anywhere in the tactic (masked attributes, unknown roles), else None."""
    reasons = []
    for slot in slots:
        detail = explain_role_score(candidate, slot.role, slot.position)
        if detail.score.available:
            return None
        reasons.append(f"slot {slot.slot}: {detail.score.reason}")
    return "candidate cannot be scored in any tactic slot: " + "; ".join(reasons[:3])


def _starts(minutes_plan: MinutesPlan) -> dict[int, int]:
    counts: dict[int, int] = {}
    for fixture in minutes_plan.fixtures:
        for pid in fixture.planned_minutes:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def marginal_contribution(candidate: CandidatePackage, context: SquadContext, *, baseline: MinutesPlan | None = None, availability_scenarios: list[AvailabilityScenario] | None = None) -> MarginalContribution:
    """Re-solve the lineup and minutes plan with and without the candidate.

    ``baseline`` is the current plan without the candidate (re-solved when
    not supplied so that both sides use the same request). Every re-solve is
    advisory: the candidate is not at the club, so nothing here is a
    submittable selection and ``unverified`` is always true.
    """
    player = candidate.player
    if not context.fixtures:
        return MarginalContribution(candidate.candidate_id, player.player_id, "unavailable", "no fixtures in the horizon: nothing to re-solve")
    if any(p.player_id == player.player_id for p in context.players):
        return MarginalContribution(candidate.candidate_id, player.player_id, "unavailable", "candidate is already in the squad; use shadow_value_of_squad_place for a retention question")
    blocked = _fit_scoreable(player, context.slots)
    if blocked:
        return MarginalContribution(candidate.candidate_id, player.player_id, "unavailable", blocked, coverage=role_coverage(context.players, context.slots, None))
    without = baseline if baseline is not None else plan(context.request())
    with_ = plan(context.request(extra_players=[player]))
    request_without = context.request()
    request_with = context.request(extra_players=[player])
    starts_without, starts_with = _starts(without), _starts(with_)
    components = {
        "immediate_lineup_objective": ComponentChange("immediate_lineup_objective", lineup_objective(without.fixtures[0].lineup), lineup_objective(with_.fixtures[0].lineup), VALUATION_UNIT),
        "horizon_lineup_objective_sum": ComponentChange("horizon_lineup_objective_sum", horizon_objective(without), horizon_objective(with_), VALUATION_UNIT),
        "horizon_plan_value": ComponentChange("horizon_plan_value", plan_value(request_without, without.fixtures, without.cumulative_minutes), plan_value(request_with, with_.fixtures, with_.cumulative_minutes), "plan_value_units"),
        "infeasible_fixtures": ComponentChange("infeasible_fixtures", float(sum(1 for f in without.fixtures if f.lineup.status == "infeasible")), float(sum(1 for f in with_.fixtures if f.lineup.status == "infeasible")), "fixtures"),
        "candidate_starts": ComponentChange("candidate_starts", 0.0, float(starts_with.get(player.player_id, 0)), "starts"),
        "candidate_planned_minutes": ComponentChange("candidate_planned_minutes", 0.0, float(with_.cumulative_minutes.get(player.player_id, 0)), "minutes"),
        "promise_shortfalls": ComponentChange("promise_shortfalls", float(sum(1 for v in without.promises.values() if v["status"] == "unmet")), float(sum(1 for v in with_.promises.values() if v["status"] == "unmet")), "promises"),
    }
    names = {p.player_id: p.name for p in context.players}
    displaced = [{"player_id": pid, "name": names.get(pid, str(pid)), "starts_lost": starts_without.get(pid, 0) - starts_with.get(pid, 0)} for pid in sorted(starts_without) if starts_with.get(pid, 0) < starts_without.get(pid, 0)]
    candidate_slots = [{"fixture_index": f.index, "date": f.fixture.date, "slot": a.slot, "position": a.position, "role": a.role, "score": a.score} for f in with_.fixtures for a in f.lineup.assignments if a.player_id == player.player_id]
    coverage = role_coverage(context.players, context.slots, player)
    versatility = versatility_value(context, player, availability_scenarios)
    valuation = DisplayedValuation(round(components["horizon_lineup_objective_sum"].change, 9))
    return MarginalContribution(candidate.candidate_id, player.player_id, "available", None, components, coverage, coverage_summary(coverage), versatility, displaced, candidate_slots, valuation)


# ---------------------------------------------------------------------------
# shadow value of a squad place or scarce role
# ---------------------------------------------------------------------------

RESOURCE_SQUAD_PLACE = "squad_place"     # the place one current player occupies
RESOURCE_ROLE_COVER = "role_cover"       # the cover behind the starter in one tactic slot


@dataclass
class ShadowValue:
    """Marginal value of a resource under the model, obtained by re-solving with and without it."""

    resource: str
    reference: int
    value: Observed
    with_value: float | None
    without_value: float | None
    label: str = VALUATION_LABEL
    unit: str = VALUATION_UNIT
    method: str = "resolve_with_and_without"
    detail: dict[str, Any] = field(default_factory=dict)
    version: str = RECRUITMENT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"resource": self.resource, "reference": self.reference, "value": self.value.to_json(), "with_value": self.with_value, "without_value": self.without_value, "label": self.label, "unit": self.unit, "method": self.method, "detail": dict(self.detail), "version": self.version}


def shadow_value_of_squad_place(context: SquadContext, resource: str, reference: int, *, baseline: MinutesPlan | None = None) -> ShadowValue:
    """Value of a squad place (``reference`` = player id) or of the cover for a slot (``reference`` = slot index).

    A squad place is valued by the horizon plan without that player; role
    cover by the immediate fixture without the slot's current starter. The
    result is a model marginal value, not a market price, and is
    unavailable when the resource cannot be identified.
    """
    if not context.fixtures:
        return ShadowValue(resource, reference, Observed.unavailable(ValueStatus.MISSING, "shadow_value", "no fixtures in the horizon", "recruitment"), None, None)
    if resource == RESOURCE_SQUAD_PLACE:
        if not any(p.player_id == reference for p in context.players):
            return ShadowValue(resource, reference, Observed.unavailable(ValueStatus.MISSING, "shadow_value", f"player {reference} is not in the squad", "recruitment"), None, None)
        with_plan = baseline if baseline is not None else plan(context.request())
        without_plan = plan(context.request(without=[reference]))
        with_value, without_value = horizon_objective(with_plan), horizon_objective(without_plan)
        detail = {"starts_lost": _starts(with_plan).get(reference, 0), "fixtures": len(with_plan.fixtures)}
    elif resource == RESOURCE_ROLE_COVER:
        immediate = solve(context.lineup_request())
        starter = next((a.player_id for a in immediate.assignments if a.slot == reference), None)
        if starter is None:
            return ShadowValue(resource, reference, Observed.unavailable(ValueStatus.MISSING, "shadow_value", f"no starter assigned to slot {reference} in the immediate fixture", "recruitment"), None, None)
        without = solve(context.lineup_request(without=[starter]))
        with_value, without_value = lineup_objective(immediate), lineup_objective(without)
        replacement = next((a.player_id for a in without.assignments if a.slot == reference), None)
        detail = {"starter": starter, "replacement": replacement, "replacement_status": without.status}
    else:
        raise ValueError(f"unknown resource {resource!r}; use {RESOURCE_SQUAD_PLACE} or {RESOURCE_ROLE_COVER}")
    value = Observed.available_value(round(with_value - without_value, 9), "recruitment", what="shadow_value")
    return ShadowValue(resource, reference, value, with_value, without_value, detail=detail)


# ---------------------------------------------------------------------------
# finance evaluation of a package
# ---------------------------------------------------------------------------

@dataclass
class FinanceInputs:
    """Everything the finance planner needs to test a package (spec 8.1, 8.2)."""

    finance_view: FinanceView
    ledger: fin.CommitmentLedger
    policy: fin.RiskPolicy
    scenarios: list[fin.Scenario] | None = None
    engine: fin.CashFlowEngine | None = None
    regulatory: Observed | None = None
    start_date: str | None = None

    def window(self) -> tuple[dt.date, dt.date]:
        engine = self.engine or fin.CashFlowEngine()
        start = fin.as_date(self.start_date or self.ledger.as_of or self.finance_view.as_of or "1970-01-01")
        return engine.horizon(start)


def package_feasibility(package: CandidatePackage, inputs: FinanceInputs) -> fin.FeasibilityReport:
    """Test the package's exact terms against every financial constraint separately (FIN 02)."""
    return fin.check_package(list(package.terms), inputs.finance_view, inputs.ledger, inputs.policy, inputs.scenarios, start_date=inputs.start_date, engine=inputs.engine, regulatory=inputs.regulatory)


@dataclass
class PackageEvaluation:
    package: CandidatePackage
    contribution: MarginalContribution
    feasibility: fin.FeasibilityReport
    evaluated_terms_version: int

    def to_json(self) -> dict[str, Any]:
        return {"package": self.package.to_json(), "contribution": self.contribution.to_json(), "feasibility": self.feasibility.to_json(), "evaluated_terms_version": self.evaluated_terms_version}


class RecruitmentEvaluator:
    """Evaluates packages against one squad context and one finance state; recomputes when terms change."""

    def __init__(self, context: SquadContext, finance_inputs: FinanceInputs | None, *, baseline: MinutesPlan | None = None, availability_scenarios: list[AvailabilityScenario] | None = None):
        self.context = context
        self.finance_inputs = finance_inputs
        self.baseline = baseline if baseline is not None else (plan(context.request()) if context.fixtures else None)
        self.availability_scenarios = availability_scenarios
        self._contributions: dict[tuple[str, int], MarginalContribution] = {}

    def contribution(self, package: CandidatePackage) -> MarginalContribution:
        key = (package.candidate_id, package.player_id)
        if key not in self._contributions:
            self._contributions[key] = marginal_contribution(package, self.context, baseline=self.baseline, availability_scenarios=self.availability_scenarios)
        return self._contributions[key]

    def feasibility(self, package: CandidatePackage) -> fin.FeasibilityReport:
        if self.finance_inputs is None:
            blocked = MissingCapabilityReport("recruitment.package_feasibility")
            blocked.add("club_finances", "no finance inputs supplied to the evaluator")
            return fin.FeasibilityReport([fin.ConstraintResult(name, fin.ConstraintStatus.UNKNOWN, "no finance inputs") for name in ("cash_reserve", "transfer_budget", "wage_budget_headroom_weekly", "regulatory_limits")], None, None, None, None, blocked)
        return package_feasibility(package, self.finance_inputs)

    def evaluate(self, package: CandidatePackage) -> PackageEvaluation:
        return PackageEvaluation(package, self.contribution(package), self.feasibility(package), package.terms_version)

    def on_terms_changed(self, package: CandidatePackage, new_terms: list[FinancialCommitment], *, source: str | None = None) -> PackageEvaluation:
        """Recompute hook (spec 7.2): new negotiation terms re-version the package and re-run finance; the sporting side is reused."""
        return self.evaluate(package.with_terms(new_terms, source=source))

    def from_negotiation(self, package: CandidatePackage, negotiation: Any) -> PackageEvaluation:
        """Re-evaluate from a :class:`~fm_bot.planning.negotiation.NegotiationStateMachine`'s latest planning commitments."""
        return self.on_terms_changed(package, list(negotiation.planning_commitments()), source=f"negotiation:{getattr(negotiation, 'negotiation_id', '?')}")


# ---------------------------------------------------------------------------
# tradeoffs across packages (no single ranking)
# ---------------------------------------------------------------------------

@dataclass
class TradeoffRow:
    candidate_id: str
    player_name: str
    kind: str
    values: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "player_name": self.player_name, "kind": self.kind, "values": dict(self.values), "notes": list(self.notes)}


@dataclass
class TradeoffTable:
    """Side-by-side tradeoffs. ``ranking`` is deliberately ``None``: the operator sees the dimensions, not a verdict."""

    rows: list[TradeoffRow]
    dimensions: tuple[str, ...] = TRADEOFF_DIMENSIONS
    undominated: list[str] = field(default_factory=list)
    ranking: None = None
    note: str = "tradeoffs, not a ranking: sporting gain, cover, wage, fees, feasibility and acceptance are shown separately"
    version: str = RECRUITMENT_VERSION

    def row(self, candidate_id: str) -> TradeoffRow:
        for r in self.rows:
            if r.candidate_id == candidate_id:
                return r
        raise KeyError(candidate_id)

    def to_json(self) -> dict[str, Any]:
        return {"rows": [r.to_json() for r in self.rows], "dimensions": list(self.dimensions), "undominated": list(self.undominated), "ranking": None, "note": self.note, "version": self.version}


def _dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """``a`` dominates ``b`` when it is at least as good on gain, wage and fees and strictly better on one (feasibility excluded)."""
    gain_a, gain_b = a.get("horizon_plan_value") or 0.0, b.get("horizon_plan_value") or 0.0
    wage_a, wage_b = a["_wage_minor"], b["_wage_minor"]
    fee_a, fee_b = a["_fees_minor"], b["_fees_minor"]
    at_least = gain_a >= gain_b and wage_a <= wage_b and fee_a <= fee_b
    strictly = gain_a > gain_b or wage_a < wage_b or fee_a < fee_b
    return at_least and strictly


def package_tradeoffs(evaluations: Iterable[PackageEvaluation], *, window: tuple[str | dt.date, str | dt.date] | None = None) -> TradeoffTable:
    """Lay the packages side by side (spec 7.2: expose tradeoffs rather than one unexplained ranking).

    ``undominated`` lists packages not dominated on (plan value gain, weekly
    wage, guaranteed fees) by another package whose feasibility is not
    ``False``; an infeasible package cannot dominate anything.
    """
    rows: list[TradeoffRow] = []
    raw: dict[str, dict[str, Any]] = {}
    for ev in evaluations:
        pkg, con, fea = ev.package, ev.contribution, ev.feasibility
        start, end = window if window is not None else (pkg.terms[0].due_date if pkg.terms else "1970-01-01", "9999-12-31")
        wage = pkg.weekly_wage()
        fees = pkg.guaranteed_fees(start, end)
        values: dict[str, Any] = {
            "immediate_lineup_objective": con.change("immediate_lineup_objective"),
            "horizon_plan_value": con.change("horizon_plan_value"),
            "versatility_value": con.versatility.value if con.versatility else None,
            "slots_newly_covered": list(con.coverage_summary.get("newly_covered", [])),
            "weekly_wage": str(wage), "guaranteed_fees": str(fees), "conditional_total": str(pkg.conditional_total()),
            "feasible": fea.feasible, "binding_constraint": fea.binding,
            "acceptance": pkg.acceptance, "registration": pkg.registration.value if pkg.registration.available else pkg.registration.status.value,
            "availability": pkg.availability.value if pkg.availability.available else pkg.availability.status.value,
            "contribution_status": con.status,
        }
        notes = []
        if not con.available:
            notes.append(f"sporting contribution unavailable: {con.reason}")
        if fea.feasible is None:
            notes.append("finance feasibility unknown: " + ", ".join(fea.unknown or ["no finance inputs"]))
        elif fea.feasible is False:
            notes.append(f"infeasible under the finance policy: {fea.binding}")
        if pkg.acceptance != ACCEPTANCE_PLAUSIBLE:
            notes.append(f"acceptance {pkg.acceptance} ({pkg.acceptance_basis})")
        rows.append(TradeoffRow(pkg.candidate_id, pkg.player.name, pkg.kind, values, notes))
        raw[pkg.candidate_id] = {**values, "_wage_minor": wage.minor, "_fees_minor": fees.minor}
    undominated = []
    for cid, values in raw.items():
        if values["feasible"] is False or values["contribution_status"] != "available":
            continue
        dominated = any(other != cid and o["feasible"] is not False and o["contribution_status"] == "available" and _dominates(o, values) for other, o in raw.items())
        if not dominated:
            undominated.append(cid)
    return TradeoffTable(rows, undominated=undominated)


# ---------------------------------------------------------------------------
# succession gaps (3-year horizon, gap list only)
# ---------------------------------------------------------------------------

@dataclass
class SuccessionGap:
    position: str
    slot: int
    horizon_date: str
    current_cover: list[int]
    expiring: list[int]
    ageing: list[int]
    depth_after: int
    reason: str
    version: str = RECRUITMENT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"position": self.position, "slot": self.slot, "horizon_date": self.horizon_date, "current_cover": list(self.current_cover), "expiring": list(self.expiring), "ageing": list(self.ageing), "depth_after": self.depth_after, "reason": self.reason, "version": self.version}


def _contract_end(player: PlayerState, club_id: int | None) -> str | None:
    for contract in player.employment or []:
        if contract.get("kind") == "employment" and (club_id is None or contract.get("club_id") == club_id):
            return contract.get("end_date")
    return None


def succession_gaps(players: list[PlayerState], slots: list[RoleSlot], game_date: str, *, club_id: int | None = None, years: int = SUCCESSION_YEARS, min_depth: int = COVERAGE_MIN_DEPTH, age_threshold: int = SUCCESSION_AGE_THRESHOLD) -> list[SuccessionGap]:
    """Slots whose cover falls below ``min_depth`` within ``years`` through expiring contracts or age.

    This is a gap list, not a plan: it says where succession is needed and
    why, using observed contract end dates and ages only. Players with no
    observed contract end or age are neither counted as leaving nor as
    staying; they are listed in the reason.
    """
    today = parse_date(game_date)
    horizon = today.replace(year=today.year + years).isoformat()
    gaps: list[SuccessionGap] = []
    seen_positions: set[str] = set()
    for slot in slots:
        base = base_position(slot.position) or slot.position   # DCR/DCL are one succession question: DC
        if base in seen_positions:
            continue
        seen_positions.add(base)
        cover = [pid for pid, _ in covering_players(players, slot)]
        by_id = {p.player_id: p for p in players}
        expiring = [pid for pid in cover if (end := _contract_end(by_id[pid], club_id)) is not None and end <= horizon]
        ageing = [pid for pid in cover if by_id[pid].age is not None and by_id[pid].age + years >= age_threshold]
        unknown = [pid for pid in cover if _contract_end(by_id[pid], club_id) is None or by_id[pid].age is None]
        remaining = [pid for pid in cover if pid not in expiring and pid not in ageing]
        if len(remaining) < min_depth:
            reason = f"{base}: {len(cover)} cover now, {len(expiring)} contract(s) end by {horizon}, {len(ageing)} at or past {age_threshold} by then; {len(remaining)} remain against a floor of {min_depth}"
            if unknown:
                reason += f"; contract end or age unobserved for {unknown}"
            gaps.append(SuccessionGap(base, slot.slot, horizon, cover, expiring, ageing, len(remaining), reason))
    return gaps
