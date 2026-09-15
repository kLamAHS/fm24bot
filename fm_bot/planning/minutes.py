"""Fixture-aware minutes planning over the next 4-6 fixtures (spec 7.1, 7.3, 10.1; BOT 008).

The lineup for the *immediate* fixture is the action; lineups for later
fixtures are revisable plans. This module builds both from the exact
single-fixture solver in :mod:`fm_bot.planning.lineup`, solving fixtures
**sequentially** with accumulated-minutes penalties, promised-minutes
bonuses, recovery caps and known restrictions, then running a bounded
improvement pass for unmet playing-time promises.

This is a **heuristic** for the joint multi-fixture problem, not an exact
solution of it: each fixture's eleven is exact given the caps and
adjustments in force at that point, but the sequence is greedy and the
improvement pass is bounded (:data:`MAX_IMPROVEMENT_ITERATIONS`). Exact joint
optimisation (spec 7.1: constraint programming) is future work.

Honesty rules
-------------
* Every assignment in a future fixture carries ``plan_not_confirmed_fit``.
  A plan never implies a player is confirmed fit for a future date; his
  eligibility there is scenario-dependent and is re-verified before any
  submission (spec 7.1).
* The immediate fixture uses the request's mode (``advisory`` or
  ``submit``); future fixtures are always advisory.
* Recovery caps come only from the caller's recovery model. When it cannot
  answer, no cap is applied and the fixture plan records
  ``recovery_unavailable`` for that player; nothing is assumed.
* Planned minutes are what the plan asks for (a start is planned as a full
  match); they are not observed minutes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..state.records import CompetitionContext, PlayerState
from ..state.status import Observed, ValueStatus
from ..state.units import parse_date
from ..state.views import FixtureView
from .lineup import DEFAULT_MATCH_MINUTES, DEFAULT_TIME_BUDGET_SECONDS, LineupPlan, LineupRequest, RoleSlot, solve
from .roles import explain_role_score

MINUTES_PLANNER_VERSION = "minutes-sequential-v1"
HORIZON_MIN_FIXTURES = 4
HORIZON_MAX_FIXTURES = 6

# Heuristic weights (reviewable). Score units are role-score units (0..1).
ACCUMULATED_MINUTES_PENALTY = 0.06     # per full match already planned earlier in the horizon
PROMISE_BONUS = 0.15                   # maximum bonus for a player whose promised minutes are still unmet
PROMISE_SHORTFALL_WEIGHT = 0.5         # plan value lost per match-equivalent of unmet promised minutes
INFEASIBLE_FIXTURE_PENALTY = 10.0      # plan value lost per fixture without a legal eleven
MAX_IMPROVEMENT_ITERATIONS = 12

FLAG_PLAN_NOT_CONFIRMED_FIT = "plan_not_confirmed_fit"
FLAG_FUTURE_ELIGIBILITY_SCENARIO = "future_eligibility_scenario"
FLAG_RECOVERY_UNAVAILABLE = "recovery_unavailable"
FLAG_PROMISE_IMPROVEMENT = "forced_by_promise_improvement"

RESTRICTION_KINDS: tuple[str, ...] = ("suspension", "loan_parent_club", "registration", "other")

# (player_id, days_since_last_planned_appearance or None, minutes_in_that_appearance or None) -> Observed[int] permitted minutes
RecoveryModel = Callable[[int, int | None, int | None], Observed]


@dataclass(frozen=True)
class Restriction:
    """A known, dated selection restriction (observed, not guessed).

    ``suspension`` applies to fixtures between ``from_date`` and ``to_date``
    (inclusive; ``None`` means open-ended) and, when ``competition_id`` is
    set, only in that competition. ``loan_parent_club`` applies when the
    opponent is ``opponent_club_id`` (the parent club). ``registration``
    applies to the named competition.
    """

    player_id: int
    kind: str
    source: str
    reason: str = ""
    from_date: str | None = None
    to_date: str | None = None
    competition_id: int | None = None
    opponent_club_id: int | None = None

    def __post_init__(self):
        if self.kind not in RESTRICTION_KINDS:
            raise ValueError(f"unknown restriction kind {self.kind!r}")

    def applies(self, fixture: FixtureView, club_id: int | None) -> bool:
        if self.competition_id is not None and fixture.competition_id != self.competition_id:
            return False
        if self.kind == "loan_parent_club":
            if club_id is None or self.opponent_club_id is None:
                return False
            opponent = fixture.away_club_id if fixture.home_club_id == club_id else fixture.home_club_id
            return opponent == self.opponent_club_id
        if self.from_date and fixture.date < self.from_date:
            return False
        if self.to_date and fixture.date > self.to_date:
            return False
        return True

    def describe(self) -> str:
        return f"{self.kind}: {self.reason or 'observed'} ({self.source})"

    def to_json(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "kind": self.kind, "source": self.source, "reason": self.reason, "from_date": self.from_date, "to_date": self.to_date, "competition_id": self.competition_id, "opponent_club_id": self.opponent_club_id}


@dataclass
class MinutesRequest:
    """Inputs for the horizon plan.

    ``slots`` is one slot list used for every fixture or one list per
    fixture. ``eligibility`` is one ``{player_id: Observed[bool]}`` map per
    fixture: the first is the verified current reading, later ones are
    scenarios. ``promised_minutes`` must be consumed inside the horizon.
    ``workload_caps`` bound total planned minutes per player over the
    horizon. ``last_appearance`` (``{player_id: (date, minutes)}``) seeds
    the recovery model before the first fixture.
    """

    players: list[PlayerState]
    fixtures: list[FixtureView]
    slots: list[RoleSlot] | list[list[RoleSlot]]
    eligibility: list[dict[int, Observed]]
    restrictions: list[Restriction] = field(default_factory=list)
    promised_minutes: dict[int, int] = field(default_factory=dict)
    recovery_model: RecoveryModel | None = None
    workload_caps: dict[int, int] = field(default_factory=dict)
    club_id: int | None = None
    mode: str = "advisory"
    competition_rules: list[CompetitionContext | None] | None = None
    bench_size: Observed | None = None
    substitutes_allowed: Observed | None = None
    last_appearance: dict[int, tuple[str, int]] = field(default_factory=dict)
    match_minutes: int = DEFAULT_MATCH_MINUTES
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS

    def __post_init__(self):
        if len(self.eligibility) != len(self.fixtures):
            raise ValueError("one eligibility map per fixture is required")
        if self.competition_rules is not None and len(self.competition_rules) != len(self.fixtures):
            raise ValueError("one competition context (or None) per fixture is required")

    def slots_for(self, index: int) -> list[RoleSlot]:
        if self.slots and isinstance(self.slots[0], RoleSlot):
            return list(self.slots)  # type: ignore[arg-type]
        per_fixture = self.slots  # type: ignore[assignment]
        if len(per_fixture) != len(self.fixtures):
            raise ValueError("one slot list per fixture is required when slots vary")
        return list(per_fixture[index])  # type: ignore[arg-type]


@dataclass
class FixturePlan:
    index: int
    fixture: FixtureView
    kind: str                                  # action (immediate) | plan (future, revisable)
    lineup: LineupPlan
    planned_minutes: dict[int, int]
    caps: dict[int, int]
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    recovery_unavailable: list[int] = field(default_factory=list)
    restricted: dict[int, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "fixture": self.fixture.to_json(), "kind": self.kind, "lineup": self.lineup.to_json(), "planned_minutes": {str(k): v for k, v in self.planned_minutes.items()}, "caps": {str(k): v for k, v in self.caps.items()}, "flags": list(self.flags), "notes": list(self.notes), "recovery_unavailable": list(self.recovery_unavailable), "restricted": {str(k): v for k, v in self.restricted.items()}}


@dataclass
class MinutesPlan:
    fixtures: list[FixturePlan]
    cumulative_minutes: dict[int, int]
    violations: list[str]
    promises: dict[int, dict[str, Any]]
    trade_offs: list[str]
    horizon: dict[str, Any]
    improvement: dict[str, Any]
    method: str = "sequential_heuristic"
    version: str = MINUTES_PLANNER_VERSION

    @property
    def immediate(self) -> FixturePlan | None:
        return self.fixtures[0] if self.fixtures else None

    def to_json(self) -> dict[str, Any]:
        return {"fixtures": [f.to_json() for f in self.fixtures], "cumulative_minutes": {str(k): v for k, v in self.cumulative_minutes.items()}, "violations": list(self.violations), "promises": {str(k): dict(v) for k, v in self.promises.items()}, "trade_offs": list(self.trade_offs), "horizon": dict(self.horizon), "improvement": dict(self.improvement), "method": self.method, "version": self.version}


# ---------------------------------------------------------------------------
# per-fixture preparation
# ---------------------------------------------------------------------------

def _days_between(earlier: str, later: str) -> int:
    return (parse_date(later) - parse_date(earlier)).days


def _apply_restrictions(request: MinutesRequest, index: int) -> tuple[dict[int, Observed], dict[int, str]]:
    fixture = request.fixtures[index]
    eligibility = dict(request.eligibility[index])
    restricted: dict[int, str] = {}
    for restriction in request.restrictions:
        if restriction.applies(fixture, request.club_id):
            restricted[restriction.player_id] = restriction.describe()
            eligibility[restriction.player_id] = Observed(False, ValueStatus.AVAILABLE, restriction.source, None, None, restriction.describe(), "verified_eligible")
    return eligibility, restricted


def _caps(request: MinutesRequest, index: int, cumulative: dict[int, int], last: dict[int, tuple[str, int]]) -> tuple[dict[int, int], list[int]]:
    """Permitted minutes per player for fixture ``index`` from recovery and workload; players the model could not assess."""
    fixture = request.fixtures[index]
    caps: dict[int, int] = {}
    unavailable: list[int] = []
    for player in request.players:
        pid = player.player_id
        limits: list[int] = []
        if request.recovery_model is not None:
            previous = last.get(pid)
            days = _days_between(previous[0], fixture.date) if previous else None
            minutes = previous[1] if previous else None
            permitted = request.recovery_model(pid, days, minutes)
            if permitted.available:
                limits.append(int(permitted.value))
            else:
                unavailable.append(pid)
        if pid in request.workload_caps:
            limits.append(max(0, request.workload_caps[pid] - cumulative.get(pid, 0)))
        if limits:
            caps[pid] = min(limits)
    return caps, unavailable


def _adjustments(request: MinutesRequest, index: int, cumulative: dict[int, int]) -> dict[int, float]:
    remaining_fixtures = len(request.fixtures) - index
    adjustments: dict[int, float] = {}
    for player in request.players:
        pid = player.player_id
        value = -ACCUMULATED_MINUTES_PENALTY * cumulative.get(pid, 0) / request.match_minutes
        shortfall = request.promised_minutes.get(pid, 0) - cumulative.get(pid, 0)
        if shortfall > 0:
            value += PROMISE_BONUS * min(1.0, shortfall / (request.match_minutes * remaining_fixtures))
        if value:
            adjustments[pid] = value
    return adjustments


def _solve_fixture(request: MinutesRequest, index: int, cumulative: dict[int, int], last: dict[int, tuple[str, int]], forced: dict[int, int]) -> FixturePlan:
    fixture = request.fixtures[index]
    eligibility, restricted = _apply_restrictions(request, index)
    caps, recovery_unavailable = _caps(request, index, cumulative, last)
    immediate = index == 0
    kw: dict[str, Any] = {}
    if request.bench_size is not None:
        kw["bench_size"] = request.bench_size
    if request.substitutes_allowed is not None:
        kw["substitutes_allowed"] = request.substitutes_allowed
    if request.competition_rules is not None:
        kw["competition_rules"] = request.competition_rules[index]
    lineup_request = LineupRequest(request.players, request.slots_for(index), fixture, eligibility, request.mode if immediate else "advisory", forced_assignments=dict(forced), minutes_cap=caps or None, score_adjustments=_adjustments(request, index, cumulative), match_minutes=request.match_minutes, time_budget_seconds=request.time_budget_seconds, **kw)
    lineup = solve(lineup_request)
    flags: list[str] = []
    if not immediate:
        flags.extend([FLAG_PLAN_NOT_CONFIRMED_FIT, FLAG_FUTURE_ELIGIBILITY_SCENARIO])
        for assignment in lineup.assignments:
            assignment.flags.append(FLAG_PLAN_NOT_CONFIRMED_FIT)
        for entry in lineup.bench:
            entry.flags.append(FLAG_PLAN_NOT_CONFIRMED_FIT)
    if recovery_unavailable:
        flags.append(FLAG_RECOVERY_UNAVAILABLE)
    for slot, pid in forced.items():
        assignment = next((a for a in lineup.assignments if a.slot == slot and a.player_id == pid), None)
        if assignment is not None:
            assignment.flags.append(FLAG_PROMISE_IMPROVEMENT)
    planned = {a.player_id: request.match_minutes for a in lineup.assignments}
    notes = [f"{pid}: {reason}" for pid, reason in restricted.items()]
    if recovery_unavailable:
        notes.append(f"recovery model could not assess {len(recovery_unavailable)} player(s); no cap applied and none claimed")
    return FixturePlan(index, fixture, "action" if immediate else "plan", lineup, planned, caps, flags, notes, recovery_unavailable, restricted)


def _solve_sequence(request: MinutesRequest, forced: dict[int, dict[int, int]]) -> tuple[list[FixturePlan], dict[int, int]]:
    cumulative: dict[int, int] = {}
    last: dict[int, tuple[str, int]] = dict(request.last_appearance)
    plans: list[FixturePlan] = []
    for index in range(len(request.fixtures)):
        plan = _solve_fixture(request, index, cumulative, last, forced.get(index, {}))
        for pid, minutes in plan.planned_minutes.items():
            cumulative[pid] = cumulative.get(pid, 0) + minutes
            last[pid] = (plan.fixture.date, minutes)
        plans.append(plan)
    return plans, cumulative


# ---------------------------------------------------------------------------
# evaluation and improvement
# ---------------------------------------------------------------------------

def _shortfalls(request: MinutesRequest, cumulative: dict[int, int]) -> dict[int, int]:
    return {pid: promised - cumulative.get(pid, 0) for pid, promised in request.promised_minutes.items() if promised - cumulative.get(pid, 0) > 0}


def plan_value(request: MinutesRequest, plans: list[FixturePlan], cumulative: dict[int, int]) -> float:
    """Heuristic plan value: summed lineup objectives minus promise shortfalls and infeasible fixtures."""
    value = 0.0
    for plan in plans:
        value += plan.lineup.objective_value if plan.lineup.objective_value is not None else -INFEASIBLE_FIXTURE_PENALTY
    value -= PROMISE_SHORTFALL_WEIGHT * sum(_shortfalls(request, cumulative).values()) / request.match_minutes
    return value


def _best_slot_for(plan: FixturePlan, player: PlayerState, request: MinutesRequest) -> int | None:
    """The slot in a fixture where a benched player scores best, if he could be admitted there."""
    candidates = []
    for slot in request.slots_for(plan.index):
        detail = explain_role_score(player, slot.role, slot.position)
        if detail.score.available:
            candidates.append((detail.score.value, slot.slot))
    return max(candidates)[1] if candidates else None


def _improve(request: MinutesRequest, plans: list[FixturePlan], cumulative: dict[int, int]) -> tuple[list[FixturePlan], dict[int, int], dict[str, Any]]:
    """Bounded local search: force players with unmet promises into fixtures where that raises the plan value."""
    forced: dict[int, dict[int, int]] = {}
    best_value = plan_value(request, plans, cumulative)
    iterations = 0
    accepted = 0
    players_by_id = {p.player_id: p for p in request.players}
    while iterations < MAX_IMPROVEMENT_ITERATIONS:
        shortfalls = _shortfalls(request, cumulative)
        if not shortfalls:
            break
        improved = False
        for pid in sorted(shortfalls, key=shortfalls.get, reverse=True):
            for plan in reversed(plans):
                iterations += 1
                if iterations > MAX_IMPROVEMENT_ITERATIONS:
                    break
                if pid in plan.planned_minutes or pid in plan.restricted or pid in plan.lineup.exclusions:
                    continue
                slot = _best_slot_for(plan, players_by_id[pid], request)
                if slot is None or slot in forced.get(plan.index, {}):
                    continue
                trial = {i: dict(f) for i, f in forced.items()}
                trial.setdefault(plan.index, {})[slot] = pid
                trial_plans, trial_cumulative = _solve_sequence(request, trial)
                value = plan_value(request, trial_plans, trial_cumulative)
                if value > best_value + 1e-9:
                    forced, plans, cumulative, best_value = trial, trial_plans, trial_cumulative, value
                    accepted += 1
                    improved = True
                    break
            if improved or iterations > MAX_IMPROVEMENT_ITERATIONS:
                break
        if not improved:
            break
    return plans, cumulative, {"method": "bounded_forced_reassignment", "iterations": iterations, "accepted": accepted, "max_iterations": MAX_IMPROVEMENT_ITERATIONS, "final_value": round(best_value, 9), "forced": {str(i): {str(s): p for s, p in f.items()} for i, f in forced.items()}}


def _violations_and_trade_offs(request: MinutesRequest, plans: list[FixturePlan], cumulative: dict[int, int]) -> tuple[list[str], dict[int, dict[str, Any]], list[str]]:
    violations: list[str] = []
    trade_offs: list[str] = []
    names = {p.player_id: p.name for p in request.players}
    for plan in plans:
        if plan.lineup.status == "infeasible":
            violations.append(f"fixture {plan.index} ({plan.fixture.date} v {plan.fixture.opponent(request.club_id) if request.club_id is not None else plan.fixture.away_name}): no legal eleven - {'; '.join(c.message for c in plan.lineup.conflicts)}")
        capped = [pid for pid, cap in plan.caps.items() if cap < request.match_minutes and pid in plan.lineup.exclusions]
        if capped:
            trade_offs.append(f"fixture {plan.index}: {len(capped)} player(s) rested by recovery/workload caps: {[names.get(p, p) for p in capped]}")
        if plan.recovery_unavailable:
            trade_offs.append(f"fixture {plan.index}: recovery not assessed for {len(plan.recovery_unavailable)} player(s); no cap applied")
    promises: dict[int, dict[str, Any]] = {}
    for pid, promised in request.promised_minutes.items():
        planned = cumulative.get(pid, 0)
        status = "planned" if planned >= promised else "unmet"
        promises[pid] = {"promised": promised, "planned": planned, "status": status}
        if status == "unmet":
            violations.append(f"promised {promised} minutes to {names.get(pid, pid)} but only {planned} planned in the horizon")
    for pid, cap in request.workload_caps.items():
        if cumulative.get(pid, 0) > cap:
            violations.append(f"workload cap {cap} exceeded for {names.get(pid, pid)}: {cumulative[pid]} planned")
    starts = {pid: sum(1 for p in plans if pid in p.planned_minutes) for pid in cumulative}
    heavy = [names.get(pid, pid) for pid, n in starts.items() if n == len(plans) and len(plans) >= HORIZON_MIN_FIXTURES]
    if heavy:
        trade_offs.append(f"{len(heavy)} player(s) planned to start every fixture despite the accumulated-minutes penalty: {heavy}")
    return violations, promises, trade_offs


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def plan(request: MinutesRequest) -> MinutesPlan:
    """Plan the next fixtures' elevens and minutes (heuristic; immediate fixture is the action).

    Fixtures beyond :data:`HORIZON_MAX_FIXTURES` are dropped from the plan and
    reported; a horizon shorter than :data:`HORIZON_MIN_FIXTURES` is planned
    and reported as short. Promised minutes are consumed where the bounded
    improvement pass can do so; any shortfall is a listed violation.
    """
    horizon: dict[str, Any] = {"requested": len(request.fixtures), "planned": min(len(request.fixtures), HORIZON_MAX_FIXTURES), "min": HORIZON_MIN_FIXTURES, "max": HORIZON_MAX_FIXTURES, "notes": []}
    if not request.fixtures:
        return MinutesPlan([], {}, ["no fixtures supplied for the horizon"], {}, [], horizon, {"iterations": 0, "accepted": 0})
    if len(request.fixtures) > HORIZON_MAX_FIXTURES:
        horizon["notes"].append(f"horizon truncated to {HORIZON_MAX_FIXTURES} fixtures; {len(request.fixtures) - HORIZON_MAX_FIXTURES} later fixture(s) not planned")
        request = _truncate(request, HORIZON_MAX_FIXTURES)
    if len(request.fixtures) < HORIZON_MIN_FIXTURES:
        horizon["notes"].append(f"horizon shorter than {HORIZON_MIN_FIXTURES} fixtures; accumulated-minutes trade-offs are less informative")
    plans, cumulative = _solve_sequence(request, {})
    plans, cumulative, improvement = _improve(request, plans, cumulative)
    violations, promises, trade_offs = _violations_and_trade_offs(request, plans, cumulative)
    return MinutesPlan(plans, cumulative, violations, promises, trade_offs, horizon, improvement)


def _truncate(request: MinutesRequest, count: int) -> MinutesRequest:
    slots = request.slots if (request.slots and isinstance(request.slots[0], RoleSlot)) else list(request.slots[:count])
    return MinutesRequest(request.players, list(request.fixtures[:count]), slots, list(request.eligibility[:count]), request.restrictions, request.promised_minutes, request.recovery_model, request.workload_caps, request.club_id, request.mode, list(request.competition_rules[:count]) if request.competition_rules is not None else None, request.bench_size, request.substitutes_allowed, request.last_appearance, request.match_minutes, request.time_budget_seconds)
